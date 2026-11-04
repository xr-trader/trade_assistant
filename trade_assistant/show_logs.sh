#!/bin/bash
# =============================================================================
# show_logs.sh — trade_assistant 日志查看工具
#
# 功能:
#   1. 查看指定服务/全部服务的日志（tail 指定行数 / -f 持续跟踪）
#   2. --error  只检索错误/异常行（ERROR/Traceback/Exception/失败 等），跨轮转文件
#   3. --check  日志体检：文件大小、最后更新时间、是否仍在写入、目录总占用、
#               阈值提醒 —— 用于发现"日志写黑洞"（文件被删/句柄断裂）和空间膨胀
#   4. 自动展开程序内轮转文件（*.N.log，CustomRotatingFileHandler 20MB×5）
#
# 与健康检测的关系:
#   health_check.sh 负责"自动"摘录错误段并推送；本脚本是"人工"排障入口，
#   二者互补：自动发现 → 人工用 show_logs.sh --error/--check 深入。
#
# 用法:
#   bash trade_assistant/show_logs.sh                    # 交互菜单（默认 -f）
#   bash trade_assistant/show_logs.sh backend            # 看 backend 日志
#   bash trade_assistant/show_logs.sh all -n 200         # 全部服务看 200 行
#   bash trade_assistant/show_logs.sh all -e             # 全部服务只看错误
#   bash trade_assistant/show_logs.sh -c                 # 日志体检
#   bash trade_assistant/show_logs.sh backend -f         # 持续跟踪
# =============================================================================

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "$SCRIPT_DIR/config.sh" ]; then
  APP_DIR="$SCRIPT_DIR"
elif [ -f "$SCRIPT_DIR/trade_assistant/config.sh" ]; then
  APP_DIR="$SCRIPT_DIR/trade_assistant"
else
  echo "未找到 config.sh，请在 trade_assistant 目录内或其上一层目录执行。"
  exit 1
fi

. "$APP_DIR/config.sh"

# 轮转保留份数（与 src/user_logger.py backupCount 保持一致）
ROTATE_KEEP="${ROTATE_KEEP:-5}"

# 体检阈值
CHECK_FILE_WARN_MB="${CHECK_FILE_WARN_MB:-50}"      # 单文件超过提醒
CHECK_DIR_WARN_MB="${CHECK_DIR_WARN_MB:-300}"       # log 目录总量超过提醒
CHECK_STALE_SEC="${CHECK_STALE_SEC:-600}"           # 主日志超过此秒数未更新 → 疑似停写

usage() {
  echo "Usage: bash trade_assistant/show_logs.sh [service|all] [-n lines] [-f] [-e] [-c]"
  echo "Services: ${ALL_SERVICES[*]} private_ws audit_service"
  echo ""
  echo "选项:"
  echo "  -n, --lines N   显示行数（默认 80）"
  echo "  -f, --follow    持续跟踪（tail -f）"
  echo "  -e, --error     只检索错误/异常（ERROR/Traceback/Exception 等）"
  echo "  -c, --check     日志体检（大小/更新时间/是否在写/目录占用），不打印日志"
  exit 1
}

show_menu() {
  echo "请选择要查看的日志："
  echo "1) backend"
  echo "2) kline_bot"
  echo "3) modify_order"
  echo "4) signal_bot"
  echo "5) pusher"
  echo "6) private_ws"
  echo "7) audit_service"
  echo "8) all"
  echo "0) 退出"
}

select_target() {
  local choice=""
  while true; do
    show_menu
    read -r -p "请输入序号: " choice
    case "$choice" in
      1) TARGET="backend"; return 0 ;;
      2) TARGET="kline_bot"; return 0 ;;
      3) TARGET="modify_order"; return 0 ;;
      4) TARGET="signal_bot"; return 0 ;;
      5) TARGET="pusher"; return 0 ;;
      6) TARGET="private_ws"; return 0 ;;
      7) TARGET="audit_service"; return 0 ;;
      8) TARGET="all"; return 0 ;;
      0) exit 0 ;;
      *) echo "输入无效，请重新选择"; echo ;;
    esac
  done
}

print_service_header() {
  local service="$1"
  local log_file="$2"
  echo "===== $service ====="
  echo "$log_file"
}

# 后端日志过滤：排除 WebSocket 相关日志行（仅用于 backend.log / fast_*.log）
filter_backend() {
  grep -v -E '\bws\b.*\[WebSocket\]|\[WebSocket\]|websocket\.|ws\.send|ws\.recv|on_message|on_open|on_close|on_error|_ws_|WebSocketDisconnect' || true
}

# 展开轮转文件：base → base + base.1.log ... base.N.log（存在才输出）
# 注意命名：CustomRotatingFileHandler 轮转为 {去掉.log} + .N.log，
# 如 modify_order.log → modify_order.1.log（而非 modify_order.log.1.log）
expand_rotated() {
  local base="$1"
  local stem="${base%.log}"
  local i
  [ -f "$base" ] && echo "$base"
  for i in $(seq 1 "$ROTATE_KEEP"); do
    [ -f "${stem}.${i}.log" ] && echo "${stem}.${i}.log"
  done
}

service_log_files() {
  case "$1" in
    backend)
      echo "$LOG_DIR/backend.log"
      echo "$LOG_DIR/fast_error.log"
      echo "$LOG_DIR/fast_access.log"
      echo "$LOG_DIR/ws.log"
      ;;
    kline_bot)
      echo "$LOG_DIR/kline_bot.log"
      echo "$LOG_DIR/kline_bot_history_download.log"
      echo "$LOG_DIR/kline_bot_data_update.log"
      echo "$LOG_DIR/kline_bot_signal.log"
      ;;
    modify_order)
      echo "$LOG_DIR/modify_order.log"
      echo "$LOG_DIR/trade_flow.log"
      ;;
    signal_bot)
      echo "$LOG_DIR/signal_bot.log"
      ;;
    private_ws)
      echo "$LOG_DIR/private_ws.log"
      ;;
    pusher)
      echo "$LOG_DIR/pusher.log"
      echo "$LOG_DIR/pusher/pusher.log"
      echo "$LOG_DIR/fast_error.log"
      echo "$LOG_DIR/fast_access.log"
      ;;
    audit_service)
      echo "$LOG_DIR/audit_service.log"
      ;;
    *)
      return 1
      ;;
  esac
}

# 收集某服务的全部实际日志文件（含轮转），输出到 stdout
collect_log_files() {
  local service="$1"
  local log_file base
  while IFS= read -r log_file; do
    [ -n "$log_file" ] || continue
    while IFS= read -r base; do
      [ -n "$base" ] || continue
      echo "$base"
    done < <(expand_rotated "$log_file")
  done < <(service_log_files "$service")
}

# 该日志文件是否属于 WebSocket 专用日志（不应被 filter_backend 过滤）
is_ws_log() {
  case "$(basename "$1")" in
    ws.log|private_ws.log) return 0 ;;
    *) return 1 ;;
  esac
}

show_log() {
  local service="$1"
  local lines="$2"
  local follow="$3"
  local log_files=()
  local log_file

  while IFS= read -r log_file; do
    [ -n "$log_file" ] || continue
    [ -f "$log_file" ] || { echo "[warn] 日志文件不存在: $log_file" >&2; continue; }
    log_files+=("$log_file")
  done < <(collect_log_files "$service")

  [ ${#log_files[@]} -gt 0 ] || exit 1

  if [ "$follow" = "true" ]; then
    # 多文件 follow：backend 统一过滤 WebSocket 噪音（ws.log 内容本身是 WS 日志，
    # 实时跟踪时一并滤掉可减少刷屏；需要细看时用非 follow 模式，ws.log 会原样展示）
    if [ "$service" = "backend" ]; then
      tail -n "$lines" -f "${log_files[@]}" | filter_backend
    else
      tail -n "$lines" -f "${log_files[@]}"
    fi
  else
    for log_file in "${log_files[@]}"; do
      print_service_header "$service" "$log_file"
      if [ "$service" = "backend" ] && ! is_ws_log "$log_file"; then
        tail -n "$lines" "$log_file" | filter_backend
      else
        tail -n "$lines" "$log_file"
      fi
      echo
    done
  fi
}

show_all_logs() {
  local lines="$1"
  local follow="$2"
  local services=("${ALL_SERVICES[@]}" "audit_service")
  local service log_file

  if [ "$follow" = "true" ]; then
    local args=()
    for service in "${services[@]}"; do
      while IFS= read -r log_file; do
        [ -n "$log_file" ] || continue
        [ -f "$log_file" ] || continue
        args+=("-n" "$lines" "-f" "$log_file")
      done < <(collect_log_files "$service")
    done
    if [ ${#args[@]} -eq 0 ]; then
      echo "[warn] 没有可跟踪的日志文件（log 目录为空或被清理）" >&2
      exit 1
    fi
    tail "${args[@]}"
  else
    for service in "${services[@]}"; do
      while IFS= read -r log_file; do
        [ -n "$log_file" ] || continue
        [ -f "$log_file" ] || continue
        print_service_header "$service" "$log_file"
        tail -n "$lines" "$log_file"
        echo
      done < <(collect_log_files "$service")
    done
  fi
}

# ---- 错误检索 ----
ERROR_PATTERN="ERROR|Traceback|Exception|CRITICAL|FATAL|failed|失败|错误|异常"
show_errors() {
  local target="$1"
  local lines="$2"
  local services=()
  local service log_file

  if [ "$target" = "all" ]; then
    services=("${ALL_SERVICES[@]}" "audit_service")
  else
    services=("$target")
  fi

  for service in "${services[@]}"; do
    while IFS= read -r log_file; do
      [ -n "$log_file" ] || continue
      [ -f "$log_file" ] || continue
      print_service_header "$service" "$log_file"
      grep -nE "$ERROR_PATTERN" "$log_file" | tail -n "$lines" || true
      echo
    done < <(collect_log_files "$service")
  done
}

# ---- 日志体检 ----
human_size() {
  local b="$1"
  if [ "$b" -ge 1048576 ]; then echo "$((b / 1048576))MB"
  elif [ "$b" -ge 1024 ]; then echo "$((b / 1024))KB"
  else echo "${b}B"; fi
}

human_age() {
  local s="$1"
  if [ "$s" -lt 60 ]; then echo "${s}s前"
  elif [ "$s" -lt 3600 ]; then echo "$((s / 60))m前"
  else echo "$((s / 3600))h前"; fi
}

stat_size() {
  python3 -c "import os,sys; print(os.path.getsize(sys.argv[1]))" "$1" 2>/dev/null || echo 0
}

stat_mtime() {
  python3 -c "import os,sys; print(int(os.path.getmtime(sys.argv[1])))" "$1" 2>/dev/null || echo 0
}

service_running() {
  local service="$1"
  local pattern
  pattern="$(service_match_pattern "$service")" || return 1
  pgrep -f "$pattern" >/dev/null 2>&1
}

check_logs() {
  local now; now=$(date +%s)
  local ver="?"
  if [ -f "$APP_DIR/version.json" ]; then
    ver="$(python3 -c "import json;print(json.load(open('$APP_DIR/version.json')).get('version','?'))" 2>/dev/null || echo '?')"
  fi

  if [ ! -d "$LOG_DIR" ]; then
    echo "[warn] log 目录不存在: $LOG_DIR"
    exit 0
  fi

  echo "===== trade_assistant 日志体检 ($(date '+%Y-%m-%d %H:%M:%S')) 版本: $ver ====="
  echo "log 目录: $LOG_DIR"

  local total_hr
  total_hr="$(du -sh "$LOG_DIR" 2>/dev/null | awk '{print $1}')"
  echo "目录总占用: ${total_hr:-0}   (提醒阈值: ${CHECK_DIR_WARN_MB}MB)"
  echo "--------------------------------------------------------------"
  printf "%-36s %9s %10s  %s\n" "文件" "大小" "最后更新" "状态"

  local service log_file f size mtime age size_hr age_hr mark main_base rel
  local dir_bytes=0
  for service in "${ALL_SERVICES[@]}" private_ws audit_service; do
    main_base="$(basename "$(service_log_file "$service" 2>/dev/null || true)")"
    while IFS= read -r log_file; do
      [ -n "$log_file" ] || continue
      while IFS= read -r f; do
        [ -n "$f" ] || continue
        size="$(stat_size "$f")"
        mtime="$(stat_mtime "$f")"
        [ "$mtime" -gt 0 ] || continue
        age=$(( now - mtime ))
        [ "$age" -lt 0 ] && age=0
        size_hr="$(human_size "$size")"
        age_hr="$(human_age "$age")"
        dir_bytes=$((dir_bytes + size))
        rel="${f#"$LOG_DIR"/}"
        mark=""
        # 轮转文件（*.N.log）是历史归档，不判停写
        case "$(basename "$f")" in
          *.[0-9].log) mark="[归档]" ;;
          *)
            # 只对"服务主日志"判停写；fast_error/ws/trade_flow 等为条件触发日志，
            # 长时间不更新是正常的，不算写黑洞
            if [ "$(basename "$f")" = "$main_base" ] && service_running "$service" && [ "$age" -gt "$CHECK_STALE_SEC" ]; then
              mark="⚠ 疑似停写"
            fi
            ;;
        esac
        if [ "$size" -ge $((CHECK_FILE_WARN_MB * 1048576)) ]; then
          mark="$mark ⚠ 超大(${size_hr})"
        fi
        printf "%-38s %9s %10s  %s\n" "$rel" "$size_hr" "$age_hr" "$mark"
      done < <(expand_rotated "$log_file")
    done < <(service_log_files "$service")
  done

  echo "--------------------------------------------------------------"
  if [ "$dir_bytes" -ge $((CHECK_DIR_WARN_MB * 1048576)) ]; then
    echo "⚠ log 目录总占用超过 ${CHECK_DIR_WARN_MB}MB，建议发布前用 cleanup_remote_logs.sh 清理"
  else
    echo "✔ log 目录空间正常"
  fi
  echo "说明: [归档]=轮转历史文件; ⚠疑似停写=服务在运行但主日志超过 ${CHECK_STALE_SEC}s 未更新（可能写黑洞/句柄断裂）"
}

# ---- 主逻辑 ----
# 先解析全部选项，位置参数（服务名）最多一个；选项可放任意位置，
# 如 `show_logs.sh --check` / `show_logs.sh backend -e` 都合法。
LINES=80
FOLLOW=false
ERROR_MODE=false
CHECK_MODE=false
TARGET=""
POSITIONAL=""

while [ $# -gt 0 ]; do
  case "$1" in
    -n|--lines)
      [ $# -ge 2 ] || usage
      case "$2" in
        ''|*[!0-9]*) echo "[error] -n 需要正整数: $2" >&2; usage ;;
      esac
      [ "$2" -gt 0 ] 2>/dev/null || { echo "[error] -n 需要正整数: $2" >&2; usage; }
      LINES="$2"
      shift 2
      ;;
    -f|--follow)
      FOLLOW=true
      shift
      ;;
    -e|--error)
      ERROR_MODE=true
      shift
      ;;
    -c|--check)
      CHECK_MODE=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      usage
      ;;
    *)
      if [ -n "$POSITIONAL" ]; then
        echo "[error] 只能指定一个服务: $1" >&2
        usage
      fi
      POSITIONAL="$1"
      shift
      ;;
  esac
done

TARGET="$POSITIONAL"

if [ "$CHECK_MODE" = "true" ]; then
  # --check 为全局日志体检（展示所有已知日志文件），服务名可不带
  check_logs
  exit 0
fi

if [ -z "$TARGET" ]; then
  if [ "$ERROR_MODE" = "true" ]; then
    TARGET="all"   # 未指定服务时，错误检索默认全局
  else
    select_target
    FOLLOW=true
  fi
fi

case "$TARGET" in
  all)
    if [ "$ERROR_MODE" = "true" ]; then
      show_errors "all" "$LINES"
    else
      show_all_logs "$LINES" "$FOLLOW"
    fi
    ;;
  *)
    case "$TARGET" in
      private_ws|audit_service) ;;
      *) service_exists "$TARGET" || usage ;;
    esac
    if [ "$ERROR_MODE" = "true" ]; then
      show_errors "$TARGET" "$LINES"
    else
      show_log "$TARGET" "$LINES" "$FOLLOW"
    fi
    ;;
esac
