#!/bin/bash
# 每周安全更新窗口（用户 2026-10-27 裁定）
# · 不退出任何程序：写 run/apt_window.flag ⇒ signal_bot 跳过本轮、kline_bot 后台刷新本轮跳过；
#   `monitor`/`urgent` 的按需 K 线刷新不受影响 ✓
# · 用服务器既有设计 `unattended-upgrade`（仅 distro + security 源）
# · apt 低优先级跑（Nice=19 / ionice idle），不抢 CPU/IO
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLAG="$ROOT/run/apt_window.flag"
LOG="$ROOT/log/apt_window.log"
mkdir -p "$ROOT/run" "$ROOT/log"
echo "start $(date '+%F %T')" > "$FLAG"
cleanup() { rm -f "$FLAG"; }
trap cleanup EXIT

log() { printf "[%s] %b\n" "$(date '+%F %T')" "$*" >> "$LOG"; }
push() {
  curl -s -m 5 -X POST http://127.0.0.1:5100/enqueue \
    -H "Content-Type: application/json" \
    -d "{\"send_type\":\"normal\",\"msg\":$(printf "%b" "$1" | /usr/bin/python3 -c "import json,sys;print(json.dumps(sys.stdin.read()))")}" \
    >/dev/null 2>&1 || true
}

log "=== apt 维护窗口开始（flag 已置）==="
# 等当前 K 线写盘结束（最多 60s；flag 已让后台循环停在新一轮之外）
for _ in $(seq 1 30); do
  ls "$ROOT"/run/.kline*lock >/dev/null 2>&1 || break
  sleep 2
done

export DEBIAN_FRONTEND=noninteractive
nice -n 19 ionice -c3 apt-get update >> "$LOG" 2>&1
RC1=$?
nice -n 19 ionice -c3 unattended-upgrade -d >> "$LOG" 2>&1
RC2=$?
PKGS=$(grep -A1 "Packages that will be upgraded" "$LOG" | tail -1 | cut -c1-300)
REBOOT=no; [ -f /var/run/reboot-required ] && REBOOT=yes
if [ "$RC1" -eq 0 ] && [ "$RC2" -eq 0 ]; then
  MSG="✅ apt 安全更新完成\n升级: ${PKGS:-无}\n需重启: ${REBOOT}"
else
  MSG="❌ apt 更新失败（update=${RC1} upgrade=${RC2}）\n需重启: ${REBOOT}\n详见 log/apt_window.log"
fi
log "$MSG"
push "【维护窗口】$MSG"
log "=== 窗口结束（flag 已清）==="
