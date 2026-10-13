#!/usr/bin/env bash
# HapiTest 启动脚本：start | stop | restart | status
#
# 设计约定：
#   - 只读 .env.local / .env 的纯文本键值，不执行任何 shell 表达式；
#   - pid 文件放在 .run/ 下，停止前校验进程归属，绝不误杀他人进程；
#   - 端口被占用时拒绝启动并给出提示，不抢端口。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

RUN_DIR="$ROOT_DIR/.run"
PID_FILE="$RUN_DIR/hapitest.pid"
LOG_FILE="$RUN_DIR/hapitest.log"
PYTHON_BIN="${HAPITEST_PYTHON:-python3}"

read_env_value() {
  local key="$1" default="${2:-}" file value
  for file in "$ROOT_DIR/.env.local" "$ROOT_DIR/.env"; do
    [ -f "$file" ] || continue
    value="$(grep -E "^[[:space:]]*${key}[[:space:]]*=" "$file" | tail -n 1 | sed -E "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//" || true)"
    if [ -n "${value:-}" ]; then
      value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
      printf '%s' "$value"
      return 0
    fi
  done
  printf '%s' "$default"
}

HOST="$(read_env_value HAPITEST_HOST 127.0.0.1)"
PORT="$(read_env_value HAPITEST_PORT 8801)"
DATA_DIR="$(read_env_value HAPITEST_DATA_DIR "")"

pid_alive() {
  local pid="$1"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  # 校验命令行确实属于本平台，避免 pid 复用导致误杀
  ps -p "$pid" -o command= 2>/dev/null | grep -q "hapitest" || return 1
  return 0
}

current_pid() {
  [ -f "$PID_FILE" ] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  pid_alive "$pid" || return 1
  printf '%s' "$pid"
}

port_busy() {
  "$PYTHON_BIN" - "$HOST" "$PORT" <<'PY'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(0.5)
    sys.exit(0 if sock.connect_ex((host, port)) == 0 else 1)
PY
}

start() {
  if pid="$(current_pid)"; then
    echo "已在运行（pid=${pid}）：http://$HOST:$PORT/"
    return 0
  fi
  if port_busy; then
    echo "端口 $PORT 已被其它进程占用，请修改 .env.local 中的 HAPITEST_PORT 后重试" >&2
    return 1
  fi
  mkdir -p "$RUN_DIR"
  args=(serve)
  [ -n "$DATA_DIR" ] && args+=(--data-dir "$DATA_DIR")
  # 额外参数透传，例如 `./start.sh start --demo` 首次创建演示项目
  if [ "$#" -gt 0 ]; then args+=("$@"); fi
  nohup "$PYTHON_BIN" -m hapitest "${args[@]}" --host "$HOST" --port "$PORT" >"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  sleep 1
  if pid="$(current_pid)"; then
    echo "已启动（pid=${pid}）：http://$HOST:$PORT/"
    echo "日志：$LOG_FILE"
    echo "数据目录：${DATA_DIR:-$ROOT_DIR/data}"
  else
    echo "启动失败，请查看日志：$LOG_FILE" >&2
    tail -n 20 "$LOG_FILE" >&2 || true
    return 1
  fi
}

stop() {
  if ! pid="$(current_pid)"; then
    echo "未在运行"
    rm -f "$PID_FILE"
    return 0
  fi
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 30); do
    pid_alive "$pid" || break
    sleep 0.2
  done
  if pid_alive "$pid"; then
    echo "进程未退出，发送 SIGKILL（pid=${pid}）"
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  echo "已停止"
}

status() {
  if pid="$(current_pid)"; then
    echo "运行中（pid=${pid}）：http://$HOST:$PORT/"
    "$PYTHON_BIN" - "$HOST" "$PORT" <<'PY' || true
import json, sys, urllib.request
host, port = sys.argv[1], sys.argv[2]
try:
    with urllib.request.urlopen("http://%s:%s/api/v1/health" % (host, port), timeout=2) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data", {})
    print("健康检查：%s %s，项目数 %s" % (data.get("name"), data.get("version"), data.get("projects")))
except Exception as exc:
    print("健康检查失败：%s" % exc)
PY
  else
    echo "未在运行"
    return 1
  fi
}

case "${1:-start}" in
  start) shift || true; start "$@" ;;
  stop) stop ;;
  restart) shift || true; stop; start "$@" ;;
  status) status ;;
  *) echo "用法: $0 {start|stop|restart|status} [额外参数，例如 --demo]" >&2; exit 2 ;;
esac
