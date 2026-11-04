#!/bin/bash

set -e

PYTHON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$PYTHON_DIR/config.sh"

usage() {
  echo "Usage: bash trade_assistant/workers.sh [--debug] [start|stop|restart|status]"
  echo "Managed workers: kline_bot modify_order"
  exit 1
}

WORKER_SERVICES=(
  kline_bot
  modify_order
)

run_action() {
  local action="$1"
  local service="$2"
  bash "$PYTHON_DIR/service.sh" "$action" "$service"
}

# 解析参数
ACTION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --debug)
      export LOG_LEVEL="DEBUG"
      shift
      ;;
    start|stop|restart|status)
      ACTION="$1"
      shift
      ;;
    *)
      usage
      ;;
  esac
done

[ -n "$ACTION" ] || usage

case "$ACTION" in
  start)
    for service in "${WORKER_SERVICES[@]}"; do
      run_action start "$service"
    done
      ;;
  stop)
    for (( idx=${#WORKER_SERVICES[@]}-1 ; idx>=0 ; idx-- )); do
      run_action stop "${WORKER_SERVICES[idx]}"
    done
    ;;
  restart)
    for (( idx=${#WORKER_SERVICES[@]}-1 ; idx>=0 ; idx-- )); do
      run_action stop "${WORKER_SERVICES[idx]}"
    done
    for service in "${WORKER_SERVICES[@]}"; do
      run_action start "$service"
    done
      ;;
  status)
    for service in "${WORKER_SERVICES[@]}"; do
      run_action status "$service"
    done
    ;;
  *)
    usage
    ;;
esac
