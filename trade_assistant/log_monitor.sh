#!/bin/bash
# ============================================================================
# 日志监控脚本 — 实时跟踪后端日志，错误时推送飞书 + 存快照
#
# 用法:
#   bash trade_assistant/log_monitor.sh start|stop|restart|status
#
# 依赖:
#   - trade_assistant/config.sh         (环境配置)
#   - trade_assistant/src/pusher/       (飞书推送服务，监听 :5100)
# ============================================================================

set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/config.sh"

LOG_DIR="$PYTHON_ROOT/log"
PID_FILE="$RUN_DIR/log_monitor.pid"
SNAPSHOT_DIR="$LOG_DIR/error_snapshots"
PUSHER_URL="${PUSHER_URL:-http://127.0.0.1:5100/enqueue}"

# 要监控的日志文件（使用 find 动态收集 *.log，排除快照目录和备份）
MONITOR_PATTERN="$LOG_DIR/*.log"

usage() {
  echo "Usage: bash trade_assistant/log_monitor.sh start|stop|restart|status"
  exit 1
}

is_running() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

start_monitor() {
  if is_running; then
    echo "log_monitor is already running. pid=$(cat "$PID_FILE")"
    return 0
  fi

  ensure_runtime_dirs
  mkdir -p "$SNAPSHOT_DIR"

  # 以 nohup 方式启动监控循环
  nohup bash "$SCRIPT_DIR/log_monitor.sh" _daemon > /dev/null 2>&1 &
  echo $! > "$PID_FILE"
  echo "log_monitor started. pid=$!"
}

stop_monitor() {
  if ! is_running; then
    echo "log_monitor is not running"
    rm -f "$PID_FILE"
    return 0
  fi
  local pid
  pid=$(cat "$PID_FILE")
  # 先杀子进程（tail -F 等），再杀父进程
  pkill -P "$pid" 2>/dev/null || true
  kill "$pid" 2>/dev/null || true
  sleep 1
  rm -f "$PID_FILE"
  echo "log_monitor stopped"
}

status_monitor() {
  if is_running; then
    echo "log_monitor is running. pid=$(cat "$PID_FILE")"
  else
    echo "log_monitor is not running"
  fi
}

# ── 守护进程主体 ──
_daemon() {
  # 标记文件，用于跨子进程降频清理快照
  CLEANUP_MARKER="$RUN_DIR/.log_monitor_cleanup"

  while true; do
    # 构建 tail 参数：动态收集所有 .log 文件
    LOG_FILES=()
    while IFS= read -r -d '' f; do
      LOG_FILES+=("$f")
    done < <(find "$LOG_DIR" -maxdepth 1 -name '*.log' -print0 2>/dev/null)

    if [ ${#LOG_FILES[@]} -eq 0 ]; then
      echo "[log_monitor] 未找到日志文件，等待 10 秒后重试" >> "$LOG_DIR/log_monitor.log"
      sleep 10
      continue
    fi

    echo "[log_monitor] 开始监控 ${#LOG_FILES[@]} 个日志文件" >> "$LOG_DIR/log_monitor.log"

    # tail -F 跟踪所有文件（文件轮转后自动重跟）
    tail -F "${LOG_FILES[@]}" 2>/dev/null | while read -r line; do
      # 快速过滤：只处理错误级别
      echo "$line" | grep -E "ERROR|CRITICAL|Exception|Traceback" > /dev/null 2>&1 || continue

      # 提取关键信息用于飞书推送
      timestamp=$(echo "$line" | grep -oP '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}' || date '+%Y-%m-%d %H:%M:%S')
      level=$(echo "$line" | grep -oP 'ERROR|CRITICAL' || echo "ERROR")
      module=$(echo "$line" | grep -oP '\[\K[^\]]+(?=\])' | head -2 | tail -1 || echo "unknown")
      msg=$(echo "$line" | sed 's/.*\(ERROR\|CRITICAL\): *//' | head -c 200)

      # 存快照（用 +%N 纳秒避免同一秒文件覆盖）
      snapshot_file="$SNAPSHOT_DIR/error_$(date '+%Y%m%d_%H%M%S_%N')_$(echo "$module" | tr -cd 'a-zA-Z0-9').log"
      echo "$line" > "$snapshot_file"

      # 降频清理：每 60 秒最多清一次，避免高频错误时反复扫盘
      now_ts=$(date '+%s')
      last_clean=$(cat "$CLEANUP_MARKER" 2>/dev/null || echo 0)
      if [ $((now_ts - last_clean)) -gt 60 ]; then
        echo "$now_ts" > "$CLEANUP_MARKER"
        find "$SNAPSHOT_DIR" -name '*.log' -type f -print0 | tail -z -n +501 | xargs -0 rm -f 2>/dev/null || true
      fi

      # 推送飞书（通过本机 pusher 服务，JSON 特殊字符转义）
      msg_esc=$(printf '%s' "$msg" | sed 's/["\]/\\&/g; s/\t/\\t/g; s/\r//g')
      module_esc=$(printf '%s' "$module" | sed 's/["\]/\\&/g; s/\t/\\t/g; s/\r//g')
      push_body="{\"send_type\":\"normal\",\"msg\":\"[$module_esc] $msg_esc\"}"
      curl -s -X POST "$PUSHER_URL" \
        -H "Content-Type: application/json" \
        -d "$push_body" \
        -o /dev/null \
        --connect-timeout 3 --max-time 5 2>/dev/null || true
    done

    echo "[log_monitor] tail 已退出，5 秒后重新扫描..." >> "$LOG_DIR/log_monitor.log"
    sleep 5
  done
}

# ── 入口 ──
case "${1:-}" in
  start)   start_monitor ;;
  stop)    stop_monitor ;;
  restart) stop_monitor; start_monitor ;;
  status)  status_monitor ;;
  _daemon) _daemon ;;
  *)       usage ;;
esac
