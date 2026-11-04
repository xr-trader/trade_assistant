#!/bin/bash

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_ROOT="$PROJECT_ROOT/trade_assistant"
VUE_ROOT="$PROJECT_ROOT/vue"

RUN_DIR="$PYTHON_ROOT/run"
LOG_DIR="$PYTHON_ROOT/log"

PYTHON_BIN="/usr/bin/python3"
NPM_BIN="${NPM_BIN:-npm}"

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
AUTOMONITOR_INITIAL_DELAY="${AUTOMONITOR_INITIAL_DELAY:-20}"
WORKERS_AUTOMONITOR_DELAY="${WORKERS_AUTOMONITOR_DELAY:-10}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

PYTHON_SERVICES=(
  backend
  kline_bot
  modify_order
  # ★ 2026-10-14：信号检测**回到独立进程**并更名为 `signal_bot`（入口 src/signal_bot_api.py）
  #   —— 原来的 `signal_monitor` 名字与进程内线程架构已作废 ✗
  #   K 线新鲜度由 JSON 请求/回执与 kline_bot 协调（src/signal_refresh_bridge.py ✓）
  signal_bot
  condition
  pusher
)

ALL_SERVICES=(
  backend
  kline_bot
  modify_order
  signal_bot
  condition
  pusher
)

ensure_runtime_dirs() {
  mkdir -p "$RUN_DIR" "$LOG_DIR"
}

service_pid_file() {
  echo "$RUN_DIR/$1.pid"
}

service_log_file() {
  case "$1" in
    backend) echo "$LOG_DIR/backend.log" ;;
    kline_bot) echo "$LOG_DIR/kline_bot.log" ;;
    modify_order) echo "$LOG_DIR/modify_order.log" ;;
    signal_bot) echo "$LOG_DIR/signal_bot.log" ;;
    condition) echo "$LOG_DIR/condition.log" ;;
    pusher) echo "$LOG_DIR/pusher.log" ;;
    *) return 1 ;;
  esac
}

service_cwd() {
  case "$1" in
    backend|kline_bot|modify_order|signal_bot|condition|pusher) echo "$PYTHON_ROOT" ;;
    *) return 1 ;;
  esac
}

service_command() {
  case "$1" in
    backend)
      echo "MALLOC_ARENA_MAX=2 MALLOC_TRIM_THRESHOLD_=131072 LOG_LEVEL=$LOG_LEVEL $PYTHON_BIN -m uvicorn take_over_order_app:app --host $BACKEND_HOST --port $BACKEND_PORT --timeout-keep-alive 30"
      ;;
    kline_bot)
      echo "LOG_LEVEL=$LOG_LEVEL TAKEOVER_LOG_PROCESS=kline_bot $PYTHON_BIN -u ./src/kline_bot_api.py"
      ;;
    modify_order)
      echo "LOG_LEVEL=$LOG_LEVEL TAKEOVER_LOG_PROCESS=modify_order $PYTHON_BIN -u ./src/modify_order_monitor_api.py"
      ;;
    signal_bot)
      echo "LOG_LEVEL=$LOG_LEVEL TAKEOVER_LOG_PROCESS=signal_bot $PYTHON_BIN -u ./src/signal_bot_api.py"
      ;;
    condition)
      echo "LOG_LEVEL=$LOG_LEVEL TAKEOVER_LOG_PROCESS=condition $PYTHON_BIN -u ./src/condition_process.py"
      ;;
    pusher)
      echo "LOG_LEVEL=$LOG_LEVEL $PYTHON_BIN -u -m uvicorn src.pusher.server:app --host ${PUSHER_HOST:-127.0.0.1} --port ${PUSHER_PORT:-5100}"
      ;;
    *)
      return 1
      ;;
  esac
}

service_match_pattern() {
  case "$1" in
    backend)
      echo "uvicorn take_over_order_app:app"
      ;;
    kline_bot)
      echo "./src/kline_bot_api.py"
      ;;
    modify_order)
      echo "./src/modify_order_monitor_api.py"
      ;;
    signal_bot)
      echo "./src/signal_bot_api.py"
      ;;
    condition)
      echo "./src/condition_process.py"
      ;;
    pusher)
      echo "src.pusher.server:app"
      ;;
    *)
      return 1
      ;;
  esac
}

service_exists() {
  case "$1" in
    backend|kline_bot|modify_order|signal_bot|condition|pusher) return 0 ;;
    *) return 1 ;;
  esac
}

# activate_venv 已移除 — Ubuntu 22.04 系统 Python 直接可用
