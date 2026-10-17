#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONUTF8=1
export MUSE_PORT="${MUSE_PORT:-8000}"
[[ -x .venv/bin/python ]] || { echo 'Run uv sync --locked first.'; exit 1; }
[[ -f .env ]] || { echo 'Copy .env.example to .env and configure it first.'; exit 1; }
exec .venv/bin/python -m uvicorn muse.app:app --host 127.0.0.1 --port "$MUSE_PORT" --loop asyncio
