#!/usr/bin/env bash
# 从 Apifox 同步接口到 HapiTest（走 MCP，无需手工导出文件）。
#
# 用法：
#   ./scripts/sync_apifox.sh <HapiTest项目ID> <Apifox项目ID> [--import] [额外 import 参数]
#
# 例：
#   ./scripts/sync_apifox.sh hapi-air 8882175                          # 只预览（主分支）
#   ./scripts/sync_apifox.sh hapi-air 8882175 --import                 # 预览并入库（merge）
#   ./scripts/sync_apifox.sh hapi-air 8882175 --branch 123456 --import # 指定迭代分支
#   ./scripts/sync_apifox.sh hapi-air 8882175 --import --refresh-cases # 契约更新后刷新冒烟用例
#   ./scripts/sync_apifox.sh hapi-air 8882175 --import --mode replace
#
# 前置：访问令牌放在项目根目录的 .env.local（已被 .gitignore 排除）：
#   APIFOX_ACCESS_TOKEN=xxxxx
# 令牌只从环境变量/.env.local 读取，绝不作为命令行参数传入（避免进 shell 历史）。
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${HAPITEST_PYTHON:-python3}"
TOKEN_ENV="APIFOX_ACCESS_TOKEN"

if [ "$#" -lt 2 ]; then
  echo "用法: $0 <HapiTest项目ID> <Apifox项目ID> [--import] [额外参数]" >&2
  exit 2
fi

PROJECT_ID="$1"; shift
APIFOX_PROJECT_ID="$1"; shift

DO_IMPORT=0
BRANCH_ID=""
EXTRA_ARGS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --import) DO_IMPORT=1; shift ;;
    --branch) BRANCH_ID="${2:-}"; shift 2 ;;
    *) EXTRA_ARGS+=("$1"); shift ;;
  esac
done
MCP_BRANCH_ARGS=()
[ -n "$BRANCH_ID" ] && MCP_BRANCH_ARGS=(--mcp-branch-id "$BRANCH_ID")

# 1) 令牌检查（只看是否存在，不打印内容）
TOKEN_VALUE="${!TOKEN_ENV:-}"
if [ -z "$TOKEN_VALUE" ] && [ -f "$ROOT_DIR/.env.local" ]; then
  TOKEN_VALUE="$(grep -E "^${TOKEN_ENV}=.+" "$ROOT_DIR/.env.local" | head -1 | cut -d= -f2- | tr -d '"'"'"'\r')"
fi
if [ -z "$TOKEN_VALUE" ]; then
  cat >&2 <<EOF
缺少 Apifox 访问令牌。
  1. 打开 Apifox → 右上角头像 → 账号设置 → API 访问令牌 → 新建
  2. 写入本地配置（不要写进命令行，也不要提交）：
       printf '${TOKEN_ENV}=%s\n' '<你的令牌>' >> .env.local
  3. 重新执行本脚本
EOF
  exit 3
fi

# 2) 前置检查：Node 与 npx
if ! command -v npx >/dev/null 2>&1; then
  echo "未找到 npx：MCP 方式需要 Node.js 18+；也可以改用导出文件导入（--file）。" >&2
  exit 3
fi

echo "== 1/2 预览：Apifox 项目 $APIFOX_PROJECT_ID → HapiTest 项目 $PROJECT_ID =="
"$PYTHON_BIN" -m hapitest import \
  --project "$PROJECT_ID" \
  --mcp-project "$APIFOX_PROJECT_ID" \
  "${MCP_BRANCH_ARGS[@]+"${MCP_BRANCH_ARGS[@]}"}" \
  --preview "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
preview_status=$?
if [ "$preview_status" -ne 0 ]; then
  echo "预览失败（退出码 $preview_status），未做任何写入。" >&2
  exit "$preview_status"
fi

if [ "$DO_IMPORT" -ne 1 ]; then
  echo
  echo "以上是预览结果，未写入数据库。确认无误后加 --import 重跑即可入库。"
  exit 0
fi

echo
echo "== 2/2 入库 =="
"$PYTHON_BIN" -m hapitest import \
  --project "$PROJECT_ID" \
  --mcp-project "$APIFOX_PROJECT_ID" \
  "${MCP_BRANCH_ARGS[@]+"${MCP_BRANCH_ARGS[@]}"}" \
  "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
import_status=$?
if [ "$import_status" -eq 0 ]; then
  echo
  echo "同步完成。查看结果："
  echo "  控制台  http://127.0.0.1:8801/#/p/$PROJECT_ID/endpoints"
  echo "  跑一遍  python3 -m hapitest run --project $PROJECT_ID --all"
fi
exit "$import_status"
