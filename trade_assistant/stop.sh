#!/bin/bash

set -e

PYTHON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$PYTHON_DIR/config.sh"

usage() {
  echo "Usage: bash trade_assistant/stop.sh [all|trade_assistant|service_name]"
  echo "Services: ${ALL_SERVICES[*]}"
  exit 1
}

TARGET="${1:-all}"

stop_one() {
  bash "$PYTHON_DIR/service.sh" stop "$1"
}

case "$TARGET" in
  all)
    for (( idx=${#PYTHON_SERVICES[@]}-1 ; idx>=0 ; idx-- )); do
      stop_one "${PYTHON_SERVICES[idx]}"
    done
    ;;
  trade_assistant|python)
    for (( idx=${#PYTHON_SERVICES[@]}-1 ; idx>=0 ; idx-- )); do
      stop_one "${PYTHON_SERVICES[idx]}"
    done
    ;;
  *)
    service_exists "$TARGET" || usage
    stop_one "$TARGET"
    ;;
esac




