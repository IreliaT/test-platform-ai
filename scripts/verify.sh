#!/usr/bin/env bash
# 一键验证：单元测试 + 端到端自检 + 体检，并产出可归档的证据文件。
#
# 用法：
#   ./scripts/verify.sh                 # 全部执行，报告写入 docs/reports/
#   ./scripts/verify.sh --quick         # 跳过端到端自检
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

REPORT_DIR="${HAPITEST_REPORT_DIR:-$ROOT_DIR/docs/reports}"
PYTHON_BIN="${HAPITEST_PYTHON:-python3}"
QUICK=0
[ "${1:-}" = "--quick" ] && QUICK=1

mkdir -p "$REPORT_DIR"
failed=0

echo "== 1/3 单元与接口测试 =="
if "$PYTHON_BIN" -m unittest discover -s tests -t . 2>&1 | tee "$REPORT_DIR/unittest.log" | tail -5; then
  echo "单元测试：通过"
else
  echo "单元测试：失败（详见 $REPORT_DIR/unittest.log）"
  failed=1
fi

if [ "$QUICK" -eq 0 ]; then
  echo
  echo "== 2/3 端到端自检（本地 mock，无外网）=="
  if "$PYTHON_BIN" -m hapitest verify --json "$REPORT_DIR/selfcheck.json" >"$REPORT_DIR/selfcheck.log" 2>&1; then
    "$PYTHON_BIN" - "$REPORT_DIR/selfcheck.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print("自检：%s（%s/%s 项通过，耗时 %sms）" % (data["status"], data["passed"], data["total"], data["duration_ms"]))
for item in data["checks"]:
    if item["status"] != "passed":
        print("  未通过：%s - %s" % (item["name"], item["detail"]))
PY
  else
    echo "端到端自检：失败（详见 $REPORT_DIR/selfcheck.log）"
    failed=1
  fi
else
  echo
  echo "== 2/3 端到端自检：已跳过（--quick）=="
fi

echo
echo "== 3/3 体检 =="
"$PYTHON_BIN" -m hapitest doctor | tee "$REPORT_DIR/doctor.json" | "$PYTHON_BIN" -c "
import json,sys
data = json.load(sys.stdin)
print('体检：%s' % data['status'])
for action in data.get('next_actions', []):
    print('  建议：%s' % action)
"

echo
if [ "$failed" -eq 0 ]; then
  echo "验证通过。报告目录：$REPORT_DIR"
else
  echo "验证未通过，请查看 $REPORT_DIR 下的日志。" >&2
fi
exit "$failed"
