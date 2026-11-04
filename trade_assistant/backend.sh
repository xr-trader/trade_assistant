#!/bin/bash

set -e

PYTHON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  echo "Usage: bash trade_assistant/backend.sh [--debug] [start|stop|restart|status|logs]"
  exit 1
}

# 解析参数
ACTION=""
EXTRA_ENV=""
while [ $# -gt 0 ]; do
  case "$1" in
    --debug)
      EXTRA_ENV="LOG_LEVEL=DEBUG"
      shift
      ;;
    start|stop|restart|status|logs)
      ACTION="$1"
      shift
      ;;
    *)
      usage
      ;;
  esac
done

[ -n "$ACTION" ] || usage

# 传递环境变量给 service.sh
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
if [ -n "$EXTRA_ENV" ]; then
  export LOG_LEVEL="DEBUG"
fi

bash "$PYTHON_DIR/service.sh" "$ACTION" backend
