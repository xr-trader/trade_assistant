#!/bin/bash

PYTHON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$PYTHON_DIR/config.sh"

usage() {
  echo "Usage: bash trade_assistant/status.sh [all|trade_assistant|service_name]"
  echo "Services: ${ALL_SERVICES[*]}"
  exit 1
}

TARGET="${1:-all}"

status_one() {
  bash "$PYTHON_DIR/service.sh" status "$1"
}

case "$TARGET" in
  all)
    for service in "${ALL_SERVICES[@]}"; do
      status_one "$service"
    done
    ;;
  trade_assistant|python)
    for service in "${PYTHON_SERVICES[@]}"; do
      status_one "$service"
    done
    ;;
  *)
    service_exists "$TARGET" || usage
    status_one "$TARGET"
    ;;
esac
