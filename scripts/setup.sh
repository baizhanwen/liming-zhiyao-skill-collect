#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if ! command -v node >/dev/null || ! command -v npm >/dev/null; then
  echo '缺少 Node.js/npm。请从 https://nodejs.org/ 安装 Node.js 24 LTS，再重试。' >&2
  exit 1
fi
node -e 'const [a,b]=process.versions.node.split(".").map(Number);if(!((a===20&&b>=19)||(a===22&&b>=12)||a>=23)){console.error("Vite 8 需要 Node.js 20.19+、22.12+ 或 24 LTS：https://nodejs.org/");process.exit(1)}'
PYTHON=""
for candidate in "$ROOT/.venv/bin/python" python3.12 python3.11 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
    PYTHON="$("$candidate" -c 'import sys; print(sys.executable)')"
    break
  fi
done
if [ -z "$PYTHON" ] && command -v uv >/dev/null; then
  PYTHON="$(uv python find --no-python-downloads '>=3.11' 2>/dev/null || true)"
fi
if [ -z "$PYTHON" ]; then
  echo '缺少 Python 3.11+。请从 https://www.python.org/downloads/ 安装后重试；不会自动安装系统运行时。' >&2
  exit 1
fi
if command -v uv >/dev/null; then
  uv sync --frozen --no-dev --no-python-downloads --python "$PYTHON"
else
  if [ ! -x .venv/bin/python ]; then "$PYTHON" -m venv .venv; fi
  .venv/bin/python -m pip install --requirement requirements.txt
fi
npm --prefix frontend ci --no-audit --no-fund
npm --prefix frontend run build
.venv/bin/python -c 'from backend.store import Store; s=Store(); print("数据库已初始化：", s.path)'
echo '安装完成。运行 bash scripts/start.sh；无需模型密钥即可进入工作台。'
