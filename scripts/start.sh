#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ] || [ ! -f frontend/dist/index.html ]; then
  echo '尚未安装或未构建界面，请先运行 bash scripts/setup.sh' >&2
  exit 1
fi
export SKILL_COLLECT_DATA="${SKILL_COLLECT_DATA:-$ROOT/data}"
export SKILL_COLLECT_PORT="${SKILL_COLLECT_PORT:-8765}"
"$PYTHON" - <<'PY'
import os,socket,sys
try:
    port=int(os.environ['SKILL_COLLECT_PORT'])
    if not 1024 <= port <= 65535: raise ValueError()
except ValueError:
    sys.exit('SKILL_COLLECT_PORT 必须是 1024—65535 的整数')
PY
"$PYTHON" scripts/doctor.py --check-port "$SKILL_COLLECT_PORT"
mkdir -p "$SKILL_COLLECT_DATA/logs"
LOG="$SKILL_COLLECT_DATA/logs/server.log"
printf '工作台：http://127.0.0.1:%s\n数据：%s\n日志：%s\n按 Ctrl+C 停止服务。\n' "$SKILL_COLLECT_PORT" "$SKILL_COLLECT_DATA" "$LOG"
"$PYTHON" -u -m uvicorn backend.main:app --host 127.0.0.1 --port "$SKILL_COLLECT_PORT" --no-access-log 2>&1 | tee -a "$LOG"
