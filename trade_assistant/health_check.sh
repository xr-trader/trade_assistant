#!/bin/bash
# =============================================================================
# health_check.sh — trade_assistant 发布后健康检测
#
# 职责：
#   1. 检测服务进程（清单见 config.sh 的 ALL_SERVICES —— 信号检测服务 2026-10-14 起
#      从进程内线程**改回独立进程**并更名为 `signal_bot`，重新纳入检测 ✓）
#   2. 检测端口监听（backend :8000 / pusher :5100）
#   3. 检测健康接口（backend /check_status、pusher /health）
#   4. 任一失败 → 自动收集对应服务日志的错误段（ERROR/Traceback/Exception），
#      输出最近日志帮助定位，避免"只报失败不报原因"
#
# 用法：
#   bash health_check.sh             完整检测（默认打印通过项）
#   bash health_check.sh --quiet     只打印失败项
#   bash health_check.sh -n 40       诊断时每个失败服务日志尾部行数（默认 25）
#   HEALTH_NOTIFY=0 bash health_check.sh   关闭失败人工通知（默认开启）
#   HEALTH_SEND_TYPE=all bash health_check.sh   通知 send_type（默认 normal）
#
# 退出码：0 = 全部通过；1 = 存在失败（已输出日志诊断）
#
# 集成：新链路安装/升级（install.sh / upgrade.sh）完成后自动调用；
#       失败时先 ensure_running.sh 拉起，再测一次，仍失败则报错并保留日志诊断。
# 定位：脚本主要由 AI 执行发布时调用，失败诊断输出需结构化、可直接分析；
#       同时失败会通过本地 Pusher(:5100/enqueue) 推送人工通知（钉钉/电报/飞书）。
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/config.sh"

QUIET=0
TAIL_LINES=25
while [[ $# -gt 0 ]]; do
  case "$1" in
    --quiet) QUIET=1; shift ;;
    -n) TAIL_LINES="${2:-25}"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

PASS=0
FAIL=0
FAILED_SERVICES=()

log_ok() {
  PASS=$((PASS + 1))
  [[ $QUIET -eq 1 ]] || echo "  [✔] $1"
}

log_fail() {
  FAIL=$((FAIL + 1))
  echo "  [✘] $2"
  FAILED_SERVICES+=("$1")
}

echo "===== trade_assistant 健康检测 ($(date '+%F %T')) ====="

# ---- 1) 服务进程 ----
echo "-- 服务进程 --"
for svc in "${ALL_SERVICES[@]}"; do
  pattern="$(service_match_pattern "$svc")" || continue
  pid="$(pgrep -f "$pattern" 2>/dev/null | head -1)"
  if [ -n "$pid" ]; then
    log_ok "$svc 运行中 (pid=$pid)"
  else
    log_fail "$svc" "$svc 未运行 (期望进程: $pattern)"
  fi
done

# ---- 2) 端口监听 ----
echo "-- 端口监听 --"
check_port() {
  local name="$1" port="$2"
  if ss -tln 2>/dev/null | grep -q ":$port "; then
    log_ok "$name :$port 监听中"
  else
    log_fail "$name" "$name :$port 未监听"
  fi
}
check_port backend 8000
check_port pusher 5100

# ---- 3) 健康接口 ----
echo "-- 健康接口 --"
# backend: 返回 401(需鉴权) 即说明服务已正常响应；000 = 无响应
bc="$(curl -s -o /dev/null -m 10 -w '%{http_code}' -X POST \
  "http://127.0.0.1:8000/check_status" 2>/dev/null || echo 000)"
if [ "$bc" != "000" ]; then
  log_ok "backend :8000 响应 (HTTP $bc)"
else
  log_fail "backend" "backend :8000 无响应"
fi

pc="$(curl -s -o /dev/null -m 10 -w '%{http_code}' "http://127.0.0.1:5100/health" 2>/dev/null || echo 000)"
if [ "$pc" = "200" ]; then
  log_ok "pusher :5100 健康 (HTTP 200)"
else
  log_fail "pusher" "pusher :5100 健康检查 HTTP $pc"
fi

# ---- 4) 汇总 ----
echo "=============================="
echo "  通过 $PASS 项 / 失败 $FAIL 项"
if [ "$FAIL" -eq 0 ]; then
  echo "  ✔ 全部服务正常，发布健康检测通过"
  exit 0
fi

# ---- 5) 失败诊断：抓取失败服务日志的错误段 ----
echo ""
echo "===== 失败诊断（最近错误日志） ====="
for svc in "${FAILED_SERVICES[@]}"; do
  log_file="$(service_log_file "$svc" 2>/dev/null || true)"
  echo ""
  echo "----- $svc ($log_file) -----"
  if [ -f "$log_file" ]; then
    echo "[最近 ${TAIL_LINES} 行]"
    tail -n "$TAIL_LINES" "$log_file" 2>/dev/null | sed 's/^/    /'
    echo "[错误关键字]"
    grep -nE "ERROR|Traceback|Exception|Error|error" "$log_file" 2>/dev/null \
      | tail -n 10 | sed 's/^/    /' || echo "    (无错误关键字)"
  else
    echo "    (日志文件不存在: $log_file)"
  fi
done

echo ""
echo "===== 诊断摘要（供快速定位） ====="
for svc in "${FAILED_SERVICES[@]}"; do
  log_file="$(service_log_file "$svc" 2>/dev/null || true)"
  last_err="$(grep -E 'ERROR|Traceback|Exception' "$log_file" 2>/dev/null | tail -1 | head -c 200 || true)"
  echo "  [失败] $svc → 日志: ${log_file:-未找到}"
  echo "         最近错误: ${last_err:-（无错误关键字）}"
done
echo "  回滚参考: $SCRIPT_DIR/backups/pre_*（新链路升级备份）或 bash upgrade.sh --rollback"

# ---- 6) 人工通知：失败时经本地 Pusher 推送（钉钉/电报/飞书）----
if [ "$FAIL" -gt 0 ] && [ "${HEALTH_NOTIFY:-1}" = "1" ]; then
  SEND_TYPE="${HEALTH_SEND_TYPE:-normal}"
  summary="【trade_assistant 发布健康检测失败】 $(date '+%F %T')"
  summary+="\\n失败 ${FAIL} 项: ${FAILED_SERVICES[*]}"
  for svc in "${FAILED_SERVICES[@]}"; do
    log_file="$(service_log_file "$svc" 2>/dev/null || true)"
    last_err="$(grep -E 'ERROR|Traceback|Exception' "$log_file" 2>/dev/null | tail -1 | head -c 200 || true)"
    summary+="\\n• ${svc}: ${last_err:-（无错误关键字）}"
  done
  esc="$(printf '%s' "$summary" | sed 's/["\]/\\&/g')"
  if curl -s -X POST "http://127.0.0.1:5100/enqueue" \
      -H "Content-Type: application/json" \
      -d "{\"send_type\":\"${SEND_TYPE}\",\"msg\":\"${esc}\"}" \
      -o /dev/null --connect-timeout 3 --max-time 5 2>/dev/null; then
    echo ""
    echo "  ⚠️ 已推送人工通知（send_type=${SEND_TYPE}）"
  else
    echo ""
    echo "  ⚠️ 人工通知推送失败（Pusher 不可用或未配置渠道）"
  fi
fi

echo ""
echo "===== 诊断完成 ====="
exit 1
