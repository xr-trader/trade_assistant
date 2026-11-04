#!/bin/bash
# ==============================================================
# pusher.sh — 服务端飞书 Pusher 管理
# 用法: bash pusher.sh {start|stop|restart|status}
# ==============================================================
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

case "${1:-usage}" in
  start|stop|restart|status)
    bash "$SCRIPT_DIR/service.sh" "$1" pusher
    ;;
  *)
    echo "用法: bash pusher.sh {start|stop|restart|status}"
    exit 1
    ;;
esac
