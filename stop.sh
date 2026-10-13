#!/usr/bin/env bash
# 停止 HapiTest（等价于 ./start.sh stop）
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$ROOT_DIR/start.sh" stop
