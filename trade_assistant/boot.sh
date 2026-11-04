#!/bin/bash

set -e

PYTHON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bash "$PYTHON_DIR/ensure_running.sh" "${1:-all}"
