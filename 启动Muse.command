#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

URL="http://127.0.0.1:8000/"
fail() {
  printf '\n%s\n' "$1"
  read -r -p '按回车关闭窗口…' || true
  exit 1
}

if curl --silent --max-time 2 "$URL" | /usr/bin/grep -q 'Muse'; then
  echo 'Muse 已在运行，本次只打开工作台，不会重启服务。'
  echo '更新代码后请先在原服务终端按 Control+C 停止，再启动；旧服务仍占用 8000 时不会加载新代码。'
  open "$URL"
  exit 0
fi

if lsof -tiTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
  fail '端口 8000 已被其他程序占用，请先关闭占用程序。'
fi

[[ -x .venv/bin/python ]] || fail '缺少 Python 环境。请在项目目录运行 uv sync，再双击此脚本。'
[[ -f .env ]] || fail '缺少 .env 配置。请参考 .env.example 配置模型 API Key。'

echo '正在启动 Muse…'
echo '工作台：http://127.0.0.1:8000/'
echo '保持此终端窗口开启；按 Control+C 停止服务。'

# Wait for this service to become ready before opening the workbench.
(
  for attempt in {1..40}; do
    if curl --silent --fail --max-time 1 "$URL" >/dev/null; then
      open "$URL"
      exit 0
    fi
    sleep 0.5
  done
) &
opener_pid=$!
trap 'kill "$opener_pid" 2>/dev/null || true' EXIT
if ! .venv/bin/python -m uvicorn muse.app:app --host 127.0.0.1 --port 8000 --loop asyncio; then
  fail 'Muse 启动失败，请查看上面的错误信息。'
fi
