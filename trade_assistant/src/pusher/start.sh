#!/bin/bash
# 手动前台启动 pusher（服务化启停请用 service.sh，启动命令定义在 config.sh 的
# service_command()）。原内容 `exec python3 server.py` 已失效：pusher 模块自
# 2026-09-29 起以 .so 形态部署，正式启动方式是 uvicorn 字符串加载。
DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$DIR"
exec python3 -m uvicorn src.pusher.server:app \
  --host "${PUSHER_HOST:-127.0.0.1}" --port "${PUSHER_PORT:-5100}"
