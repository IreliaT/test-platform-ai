"""运行历史对比。

对比两次运行的用例级结果，用于回答"这次失败是不是新引入的"。
注意：契约/定义变化本身不是结论，只是关联证据——``same_fingerprint`` 为 False 时
说明两次运行的用例定义已经不同，差异需要人工确认。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .models import CaseResult, RunResult


def _index(results: List[CaseResult]) -> Dict[str, CaseResult]:
    mapping: Dict[str, CaseResult] = {}
    for result in results:
        key = str(result.case_id) if result.case_id is not None else "name:%s" % result.case_name
        mapping[key] = result
    return mapping


def _assertion_map(result: Optional[CaseResult]) -> Dict[str, str]:
    if result is None:
        return {}
    mapping: Dict[str, str] = {}
    for assertion in result.assertions:
        label = assertion.describe or "%s %s" % (assertion.type, assertion.operator)
        mapping[label] = "passed" if assertion.passed else "failed"
    return mapping


def diff_runs(before: RunResult, after: RunResult) -> Dict[str, Any]:
    """返回 HTTP_API.md 中约定的固定结构。"""
    old = _index(before.case_results)
    new = _index(after.case_results)

    changes: List[Dict[str, Any]] = []
    added = removed = status_changed = assertion_changed = newly_failed = newly_passed = 0

    for key, result in new.items():
        previous = old.get(key)
        if previous is None:
            added += 1
            changes.append(
                {
                    "kind": "added",
                    "case_id": result.case_id,
                    "case_name": result.case_name,
                    "before": None,
                    "after": {"status": result.status, "assertions": len(result.assertions)},
                    "assertions": [],
                }
            )
            continue
        if previous.status != result.status:
            status_changed += 1
            if result.status in ("failed", "error", "incomplete") and previous.status == "passed":
                newly_failed += 1
            if result.status == "passed" and previous.status in ("failed", "error", "incomplete"):
                newly_passed += 1
        old_assertions = _assertion_map(previous)
        new_assertions = _assertion_map(result)
        assertion_diffs = [
            {"describe": label, "before": old_assertions.get(label, "absent"), "after": new_assertions[label]}
            for label in new_assertions
            if old_assertions.get(label, "absent") != new_assertions[label]
        ]
        if assertion_diffs:
            assertion_changed += 1
        if previous.status != result.status or assertion_diffs:
            changes.append(
                {
                    "kind": "status_changed" if previous.status != result.status else "assertion_changed",
                    "case_id": result.case_id,
                    "case_name": result.case_name,
                    "before": {"status": previous.status, "assertions": len(previous.assertions)},
                    "after": {"status": result.status, "assertions": len(result.assertions)},
                    "assertions": assertion_diffs,
                }
            )

    for key, previous in old.items():
        if key in new:
            continue
        removed += 1
        changes.append(
            {
                "kind": "removed",
                "case_id": previous.case_id,
                "case_name": previous.case_name,
                "before": {"status": previous.status, "assertions": len(previous.assertions)},
                "after": None,
                "assertions": [],
            }
        )

    changes.sort(key=lambda item: (item["kind"], str(item["case_name"])))
    return {
        "run_id": after.id,
        "base_run_id": before.id,
        "same_fingerprint": bool(before.fingerprint and before.fingerprint == after.fingerprint),
        "summary": {
            "cases": len(new),
            "changed": status_changed + assertion_changed,
            "added": added,
            "removed": removed,
            "newly_failed": newly_failed,
            "newly_passed": newly_passed,
            "status_changed": status_changed,
            "assertion_changed": assertion_changed,
        },
        "changes": changes,
    }
