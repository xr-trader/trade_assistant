#!/bin/bash
# ==============================================================
# local.sh — 本地开发启动脚本（不上传到服务器）
# 用法: bash local.sh backend start|stop|restart|status
#       bash local.sh workers start|stop|restart|status
#       bash local.sh all    start|stop|restart|status
# ==============================================================

PYTHON_BIN="/Users/lixianren/miniconda3/bin/python3"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

usage() {
  echo "用法: bash local.sh {backend|workers|all|backend-dev} [start|stop|restart|status]"
  echo ""
  echo "  backend-dev — 开发模式启动 backend（带 --reload 热重载）"
  exit 1
}

ACTION="${2:-start}"
SERVICE="$1"

case "$SERVICE" in
  backend)
    PYTHON_BIN=$PYTHON_BIN bash "$SCRIPT_DIR/service.sh" "$ACTION" backend
    ;;
  backend-dev)
    if [ "$ACTION" != "start" ]; then
      PYTHON_BIN=$PYTHON_BIN bash "$SCRIPT_DIR/service.sh" "$ACTION" backend
    else
      echo "[local] 开发模式启动 backend（热重载）..."
      cd "$SCRIPT_DIR"
      $PYTHON_BIN -m uvicorn take_over_order_app:app \
        --host 127.0.0.1 --port 8000 --reload
    fi
    ;;
  workers)
    PYTHON_BIN=$PYTHON_BIN bash "$SCRIPT_DIR/workers.sh" "$ACTION"
    ;;
  all)
    PYTHON_BIN=$PYTHON_BIN bash "$SCRIPT_DIR/service.sh" "$ACTION" backend
    PYTHON_BIN=$PYTHON_BIN bash "$SCRIPT_DIR/workers.sh" "$ACTION"
    ;;
  *)
    usage
    ;;
esac
