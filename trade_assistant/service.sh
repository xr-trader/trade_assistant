#!/bin/bash

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/config.sh"

usage() {
  echo "Usage: bash trade_assistant/service.sh [start|stop|restart|status|logs] <service>"
  echo "Services: ${ALL_SERVICES[*]}"
  exit 1
}

list_service_pids() {
  local service="$1"
  local pattern
  pattern="$(service_match_pattern "$service")" || return 1
  # 排除自身脚本链中的 shell 包装进程，避免 pgrep -f 把
  # 执行本脚本的会话链（远程 SSH / cron 命令行文本含模式串，
  # 如 "bash .../kline_bot.sh" 会被 pgrep -f "kline_bot" 误匹配）误判为服务进程。
  # 注意：只排除 bash/sh 包装进程，不排除服务进程本体——
  # 当 HTTP 重启由服务进程内部触发时，祖先链包含服务本体（uvicorn），
  # 若整条祖先链都排除会导致 stop 判断“not running”、重启失效。
  local exclude=""
  local p="$$"
  while [ -n "$p" ] && [ "$p" -gt 1 ]; do
    local cmd
    cmd="$(ps -o args= -p "$p" 2>/dev/null)"
    if [[ "$cmd" == bash* || "$cmd" == sh* || "$cmd" == /bin/bash* || "$cmd" == /bin/sh* || "$cmd" == /usr/bin/bash* || "$cmd" == /usr/bin/sh* ]]; then
      exclude="$exclude $p"
    fi
    p="$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')"
  done
  pgrep -f "$pattern" 2>/dev/null | awk -v ex="$exclude" '
    BEGIN { split(ex, a, " "); for (i in a) excl[a[i]] = 1 }
    !($1 in excl)
  '
}

sync_pid_file() {
  local service="$1"
  local pid_file
  pid_file="$(service_pid_file "$service")"
  local first_pid
  first_pid="$(list_service_pids "$service" | head -n 1)"
  if [ -n "$first_pid" ]; then
    echo "$first_pid" > "$pid_file"
  else
    rm -f "$pid_file"
  fi
}

is_running() {
  local service="$1"
  if list_service_pids "$service" | grep -q .; then
    sync_pid_file "$service"
    return 0
  fi
  rm -f "$(service_pid_file "$service")"
  return 1
}

start_service() {
  local service="$1"
  ensure_runtime_dirs

  if is_running "$service"; then
    echo "$service is already running. pid=$(cat "$(service_pid_file "$service")")"
    return 0
  fi

  local cwd cmd log_file pid_file
  cwd="$(service_cwd "$service")" || exit 1
  cmd="$(service_command "$service")" || exit 1
  log_file="$(service_log_file "$service")" || exit 1
  pid_file="$(service_pid_file "$service")"

  # 清理旧 PID 文件，让 SingleInstance 不会读到残留
  rm -f "$pid_file"

  # 启动进程，$! 是直接启动的进程 PID
  cd "$cwd" || exit 1
  export TAKEOVER_LOG_DIR="$LOG_DIR"
  # 9>&- ：关闭从 ensure_running.sh 继承来的 flock 锁 fd（fd 9）。
  # 守护进程是长活进程，一旦继承该 fd，ensure_running.sh 退出后锁仍被它持有，
  # 之后每次 ensure_running 都判为 "already running" 直接退出 ⇒ 保活彻底失效、
  # 挂掉的服务永远拉不起来（2026-09-29 事故：pusher 继承锁，backend/4 worker 全没起）。
  env $cmd >/dev/null 2>&1 9>&- &

  # 等待进程启动，用 pgrep 检测（不用 is_running，因为它会写 PID 文件干扰 SingleInstance）
  local waited=0
  while [ "$waited" -lt 5 ]; do
    # 优先等 SingleInstance 写入 PID 文件
    if [ -f "$pid_file" ]; then
      break
    fi
    # 后备：进程已在运行但还没写 PID 文件
    if list_service_pids "$service" | grep -q .; then
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done

  # 用 pgrep 检测（不用 is_running，它会写 PID 文件干扰 SingleInstance）
  if list_service_pids "$service" | grep -q .; then
    echo "$service started. pid=$(cat "$pid_file" 2>/dev/null) log=$log_file"
  sync_pid_file "$service"
  # 记录启动审计日志
  $PYTHON_BIN -u src/audit_service_event.py startup "$service" >>"$LOG_DIR/audit_service.log" 2>&1 || true
  else
    echo "$service failed to start. check log=$log_file"
    rm -f "$pid_file"
    return 1
  fi
}

stop_service() {
  local service="$1"
  local pid_file
  pid_file="$(service_pid_file "$service")"

  local pids
  pids="$(list_service_pids "$service")"
  if [ -z "$pids" ]; then
    echo "$service is not running"
    rm -f "$pid_file"
    return 0
  fi

  echo "$pids" | xargs kill 2>/dev/null || true

  local waited=0
  while [ "$waited" -lt 5 ]; do
    if ! list_service_pids "$service" | grep -q .; then
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done

  if list_service_pids "$service" | grep -q .; then
    list_service_pids "$service" | xargs kill -9 2>/dev/null || true
  fi

  # 等待 SingleInstance 清理 PID 文件
  waited=0
  while [ "$waited" -lt 3 ]; do
    if [ ! -f "$pid_file" ]; then
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
  rm -f "$pid_file"
  echo "$service stopped"
  # 记录停止审计日志
  cd "$PYTHON_ROOT" || true
  $PYTHON_BIN -u src/audit_service_event.py shutdown "$service" >>"$LOG_DIR/audit_service.log" 2>&1 || true
}

status_service() {
  local service="$1"
  if is_running "$service"; then
    local pids
    pids="$(list_service_pids "$service" | paste -sd, -)"
    echo "$service is running. pids=$pids"
  else
    echo "$service is not running"
  fi
}

logs_service() {
  local service="$1"
  local log_file
  log_file="$(service_log_file "$service")" || exit 1
  if [ ! -f "$log_file" ]; then
    echo "[warn] 日志文件不存在: $log_file" >&2
    exit 1
  fi
  tail -f "$log_file"
}

[ $# -eq 2 ] || usage

ACTION="$1"
SERVICE="$2"

service_exists "$SERVICE" || usage

case "$ACTION" in
  start) start_service "$SERVICE" ;;
  stop) stop_service "$SERVICE" ;;
  restart)
    # 计划内重启（service.sh restart）⇒ 后端不生成/不推送应急刷新密钥
    # （不覆盖调用方已传的来源：升级脚本 export 的 upgrade 要保留 ✓）
    export XIAOER_START_REASON="${XIAOER_START_REASON:-restart}"
    stop_service "$SERVICE"
    start_service "$SERVICE"
    ;;
  status) status_service "$SERVICE" ;;
  logs) logs_service "$SERVICE" ;;
  *) usage ;;
esac
