#!/usr/bin/env bash
set -euo pipefail

# Resolve project root safely even when launched from Finder or paths with spaces.
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
DATA_DIR="${AISYS_DATA:-$SCRIPT_DIR/data}"
HOST="${AISYS_HOST:-127.0.0.1}"
EXPLICIT_PORT="${AISYS_PORT:-}"
NO_BROWSER="${AISYS_NO_BROWSER:-0}"
VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
LOG_DIR="$DATA_DIR/logs"
LOG_FILE="$LOG_DIR/launcher.log"

pause_if_interactive() {
  if [ -t 0 ] && [ "${AISYS_NO_PAUSE:-0}" != "1" ]; then
    echo ""
    read -r -p "按回车键关闭窗口..." _ || true
  fi
}

fail() {
  echo ""
  echo "[AIsys 错误] $1" >&2
  if [ -n "${2:-}" ]; then
    echo "[建议修复] $2" >&2
  fi
  pause_if_interactive
  exit 1
}

open_url() {
  local url="$1"
  if [ "$NO_BROWSER" = "1" ]; then
    echo "[AIsys] 已跳过自动打开浏览器（AISYS_NO_BROWSER=1）：$url"
    return 0
  fi
  if command -v open >/dev/null 2>&1; then
    if open "$url" >/dev/null 2>&1; then
      echo "[AIsys] 已在默认浏览器打开：$url"
      return 0
    fi
  fi
  echo "[AIsys 提示] 无法自动拉起浏览器，请手动在浏览器访问：$url"
  return 0
}

echo "=========================================="
echo "  AIsys 本地双模型工作台 · 一键启动"
echo "=========================================="
echo "项目目录：${SCRIPT_DIR}"

# 1. Ensure Python virtual environment exists.
if [ ! -x "$VENV_PY" ]; then
  echo "[AIsys] 未检测到虚拟环境 .venv，正在检查系统 Python 3.12+..."
  SYS_PY=""
  for candidate in python3.14 python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
        SYS_PY="$(command -v "$candidate")"
        break
      fi
    fi
  done
  if [ -z "$SYS_PY" ]; then
    fail "未找到 Python 3.12 或更高版本。" "请安装 Python 3.12+ 后重试（例如 brew install python@3.12）。"
  fi
  echo "[AIsys] 使用 ${SYS_PY} 创建虚拟环境：${VENV_DIR}"
  "$SYS_PY" -m venv "$VENV_DIR" || fail "创建虚拟环境失败。" "请手动执行：${SYS_PY} -m venv \"${VENV_DIR}\""
fi

# 2. Verify required dependencies once (never reinstall unnecessarily).
MISSING_DEPS="$("$VENV_PY" - <<'PY' 2>/dev/null || true
missing = []
for mod in ("aisys", "fastapi", "uvicorn", "langgraph", "yaml", "pydantic"):
    try:
        __import__(mod)
    except Exception:
        missing.append(mod)
print(",".join(missing))
PY
)"

if [ -n "$MISSING_DEPS" ]; then
  echo "[AIsys] 检测到缺失依赖：${MISSING_DEPS}，正在安装本地包..."
  if ! "$VENV_PY" -m pip install -e "$SCRIPT_DIR"; then
    fail "依赖安装失败（缺失：${MISSING_DEPS}）。" "请检查网络后手动运行：\"${VENV_PY}\" -m pip install -e \"${SCRIPT_DIR}\""
  fi
fi

mkdir -p "$LOG_DIR"
chmod 700 "$DATA_DIR" "$LOG_DIR" 2>/dev/null || true

# 3. Check existing verified AIsys instance and resolve safe port without killing foreign apps.
PROBE_RESULT="$("$VENV_PY" - "$DATA_DIR" "$HOST" "$EXPLICIT_PORT" <<'PY'
import json
import os
import socket
import sys
import urllib.request
from pathlib import Path
import yaml

data_dir = Path(sys.argv[1]).resolve()
host = sys.argv[2]
explicit_port = sys.argv[3].strip()

def check_aisys_health(h: str, p: int, expected_instance: str | None = None):
    try:
        with urllib.request.urlopen(f"http://{h}:{p}/api/health", timeout=1.5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("app") != "aisys":
            return None
        if expected_instance and payload.get("instance_id") != expected_instance:
            return None
        if Path(payload.get("data_dir", "")).resolve() != data_dir:
            return None
        return payload
    except Exception:
        return None

def port_open(h: str, p: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((h, p)) == 0

# Step A: Check server.json for an already-running verified instance.
server_file = data_dir / "server.json"
if server_file.is_file():
    try:
        info = json.loads(server_file.read_text(encoding="utf-8"))
        pid = int(info["pid"])
        os.kill(pid, 0)
        live = check_aisys_health(info["host"], int(info["port"]), info.get("instance_id"))
        if live:
            print(json.dumps({
                "action": "already_running",
                "url": f"http://{info['host']}:{info['port']}",
                "pid": pid,
            }))
            raise SystemExit(0)
    except Exception:
        pass

# Step B: Determine target port from AISYS_PORT or server.yaml or default 8765.
target_port = 8765
if explicit_port:
    target_port = int(explicit_port)
else:
    cfg_file = data_dir / "config" / "server.yaml"
    if cfg_file.is_file():
        try:
            cfg = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
            if isinstance(cfg.get("port"), int) and 1024 <= cfg["port"] <= 65535:
                target_port = cfg["port"]
        except Exception:
            pass

# Step C: Verify target port is not already our AIsys or occupied by a foreign process.
if port_open(host, target_port):
    live = check_aisys_health(host, target_port)
    if live:
        print(json.dumps({
            "action": "already_running",
            "url": f"http://{host}:{target_port}",
            "pid": live.get("pid"),
        }))
        raise SystemExit(0)
    if explicit_port:
        print(json.dumps({
            "action": "port_conflict",
            "port": target_port,
        }))
        raise SystemExit(0)
    # Foreign process occupies default port: NEVER kill it; pick first free fallback port.
    for cand in range(target_port + 1, min(target_port + 25, 65535)):
        if not port_open(host, cand):
            print(json.dumps({
                "action": "start_fallback",
                "original_port": target_port,
                "port": cand,
            }))
            raise SystemExit(0)
    print(json.dumps({"action": "port_conflict", "port": target_port}))
    raise SystemExit(0)

print(json.dumps({"action": "start", "port": target_port}))
PY
)"

ACTION="$("$VENV_PY" -c 'import json,sys; print(json.loads(sys.argv[1])["action"])' "$PROBE_RESULT")"

if [ "$ACTION" = "already_running" ]; then
  URL="$("$VENV_PY" -c 'import json,sys; print(json.loads(sys.argv[1])["url"])' "$PROBE_RESULT")"
  PID="$("$VENV_PY" -c 'import json,sys; print(json.loads(sys.argv[1]).get("pid","?"))' "$PROBE_RESULT")"
  echo "[AIsys] 已验证本地 AIsys 服务正在运行（PID ${PID}）：${URL}"
  open_url "$URL"
  exit 0
fi

if [ "$ACTION" = "port_conflict" ]; then
  BAD_PORT="$("$VENV_PY" -c 'import json,sys; print(json.loads(sys.argv[1])["port"])' "$PROBE_RESULT")"
  fail "端口 ${BAD_PORT} 已被其他非 AIsys 程序占用（未干扰该程序）。" "请使用其他端口启动：AISYS_PORT=8766 \"${SCRIPT_DIR}/AIsys.command\""
fi

CHOSEN_PORT="$("$VENV_PY" -c 'import json,sys; print(json.loads(sys.argv[1])["port"])' "$PROBE_RESULT")"
if [ "$ACTION" = "start_fallback" ]; then
  ORIG_PORT="$("$VENV_PY" -c 'import json,sys; print(json.loads(sys.argv[1])["original_port"])' "$PROBE_RESULT")"
  echo "[AIsys 提示] 默认端口 ${ORIG_PORT} 已被其他程序占用（未终止该进程），改用空闲端口 ${CHOSEN_PORT} 启动。"
fi

URL="http://${HOST}:${CHOSEN_PORT}"
echo "[AIsys] 正在后台启动本地 Web 服务（${URL}）..."
echo "[AIsys] 启动日志：${LOG_FILE}"

{
  echo ""
  echo "=== AIsys launch $(date '+%Y-%m-%d %H:%M:%S') (${URL}) ==="
} >> "$LOG_FILE"

# 4. Launch detached so closing the Finder Terminal or browser does NOT kill running tasks.
nohup "$VENV_PY" -m aisys --data "$DATA_DIR" serve \
  --host "$HOST" --port "$CHOSEN_PORT" --project-root "$SCRIPT_DIR" \
  >> "$LOG_FILE" 2>&1 </dev/null &
SERVER_PID=$!

# 5. Wait for verified health check before opening browser.
READY="$("$VENV_PY" - "$HOST" "$CHOSEN_PORT" "$SERVER_PID" "$DATA_DIR" <<'PY'
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

host, port, pid, data_dir = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4]).resolve()
deadline = time.monotonic() + 15.0
while time.monotonic() < deadline:
    try:
        os.kill(pid, 0)
    except OSError:
        print("exited")
        raise SystemExit(0)
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=1.0) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        if info.get("app") == "aisys" and int(info.get("pid", -1)) == pid and Path(info.get("data_dir", "")).resolve() == data_dir:
            print("ready")
            raise SystemExit(0)
    except Exception:
        pass
    time.sleep(0.25)
print("timeout")
PY
)"

if [ "$READY" != "ready" ]; then
  echo "[AIsys] 最近启动日志：" >&2
  tail -n 30 "$LOG_FILE" >&2 || true
  fail "AIsys 后端启动未通过健康检查（状态：${READY}）。" "请查看日志 ${LOG_FILE} 排查原因。"
fi

echo "[AIsys] 后端健康检查通过（PID ${SERVER_PID}）。"
open_url "$URL"
echo ""
echo "服务已在后台运行，关闭本终端窗口或浏览器不会中断已启动的任务。"
echo "如需停止服务，请在网页左下角点击「停止服务」，或运行：\"${VENV_PY}\" -m aisys --data \"${DATA_DIR}\" stop"
exit 0
