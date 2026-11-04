#!/bin/bash
# 每 30 秒记录：CPU / 内存 / swap / 负载 / IO等待 / D状态 / 网络计数 / ping / conntrack / RSS top5
# · 输出：<项目>/log/memwatch.log（>2万行裁到1万）
# · 断档：log/memwatch_gaps.log；OOM：log/memwatch_oom.log；内核网卡报错：log/memwatch_kernel.log
# · 可用内存 <=150MB（`MEMWATCH_AVAIL_LIMIT` 可调）连续 2 次 → 推送（30 分钟冷却，记录到 log/memalert.log）
#    ⚠️ 只看可用内存：**swap_out>0 不再触发**（平台/调度差异会频繁误报，2026-10-29 用户裁定）
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$ROOT/log/memwatch.log"
STATE="$ROOT/log/.memalert_last"
GAP_LOG="$ROOT/log/memwatch_gaps.log"
LAST_TS="$ROOT/log/.memwatch_last_epoch"
LAST_LINE="$ROOT/log/.memwatch_last_line"
NET_STATE="$ROOT/log/.memwatch_prev_net"
DISK_STATE="$ROOT/log/.memwatch_prev_disk"
VM_STATE="$ROOT/log/.memwatch_prev_vm"
CPU_STATE="$ROOT/log/.memwatch_prev_cpu"
OOM_LOG="$ROOT/log/memwatch_oom.log"
KERNEL_LOG="$ROOT/log/memwatch_kernel.log"
mkdir -p "$ROOT/log"
if [ "$1" = "--boot" ]; then
  echo "===== BOOT $(date "+%F %T") =====" >> "$LOG"
fi
ts=$(date "+%F %T")
_now=$(date +%s)

# ── 断档检测 ──
if [ -f "$LAST_TS" ]; then
  _prev=$(cat "$LAST_TS" 2>/dev/null || echo 0)
  _gap=$((_now - _prev))
  if [ "$_gap" -gt 90 ] && [ "$_prev" -gt 0 ]; then
    _prevline=$(cat "$LAST_LINE" 2>/dev/null || echo "-")
    printf "%s GAP %ss | 上次: %s\n" "$ts" "$_gap" "$_prevline" >> "$GAP_LOG"
  fi
fi
echo "$_now" > "$LAST_TS"

read -r load1 load5 load15 _ < /proc/loadavg
mem=$(free -m | awk "NR==2{printf \"total=%s used=%s avail=%s\", \$2,\$3,\$7}")
avail=$(free -m | awk "NR==2{print \$7}")
swap=$(free -m | awk "NR==3{printf \"used=%s total=%s\", \$3,\$2}")
cpu=$(top -bn1 | awk "/^%Cpu/{printf \"%.1f\", 100-\$8}")

# ── iowait%（/proc/stat 两次差值）──
_wa="--"
read -r _c _u _n _s _i _w _irq _sirq _st _rest < /proc/stat
_total=$((_u+_n+_s+_i+_w+_irq+_sirq+_st))
if [ -f "$CPU_STATE" ]; then
  read -r _pu _pi _pw _pt < "$CPU_STATE" 2>/dev/null || true
  _dt=$((_total - ${_pt:-0})); _di=$((_i - ${_pi:-0})); _dw=$((_w - ${_pw:-0}))
  if [ "$_dt" -gt 0 ]; then _wa=$(awk "BEGIN{printf \"%.1f\", 100*$_dw/$_dt}"); fi
fi
echo "$_total $_i $_w $_total" > "$CPU_STATE"

# ── D 状态进程数（IO 阻塞）──
_dstate=$(ps -eo stat= 2>/dev/null | awk "/^D/{n++} END{print n+0}")

# ── 网络：所有非 lo 网卡的 rx/tx 字节总和 + 错误/丢包 ──
read -r _rx _tx _err _drop < <(awk 'NR>2 { line=$0; sub(/^[ \t]+/,"",line); n=split(line,f,/[ :]+/); if (f[1]=="lo") next; rx+=f[2]; tx+=f[10]; err+=f[4]+f[12]; drop+=f[5]+f[13] } END{print rx+0, tx+0, err+0, drop+0}' /proc/net/dev)
_drx=0; _dtx=0
if [ -f "$NET_STATE" ]; then
  read -r _prx _ptx < "$NET_STATE" 2>/dev/null || true
  _drx=$((_rx - ${_prx:-_rx})); _dtx=$((_tx - ${_ptx:-_tx}))
fi
echo "$_rx $_tx" > "$NET_STATE"

# ── 磁盘 IO（/proc/diskstats：vda 读/写扇区增量 ⇒ KB/s）──
read -r _drd _dwr < <(awk '$3=="vda"{print $6*512, $10*512}' /proc/diskstats 2>/dev/null | tail -1)
_drd=${_drd:-0}; _dwr=${_dwr:-0}
_drd_d=0; _dwr_d=0
if [ -f "$DISK_STATE" ]; then
  read -r _pdrd _pdwr < "$DISK_STATE" 2>/dev/null || true
  _drd_d=$((_drd - ${_pdrd:-_drd})); _dwr_d=$((_dwr - ${_pdwr:-_dwr}))
fi
echo "$_drd $_dwr" > "$DISK_STATE"

# ── swap in/out（/proc/vmstat：pswpin/pswpout 页数增量）──
read -r _si _so < <(awk '$1=="pswpin"{si=$2} $1=="pswpout"{so=$2} END{print si+0, so+0}' /proc/vmstat)
_si_d=0; _so_d=0
if [ -f "$VM_STATE" ]; then
  read -r _psi _pso < "$VM_STATE" 2>/dev/null || true
  _si_d=$((_si - ${_psi:-_si})); _so_d=$((_so - ${_pso:-_so}))
fi
echo "$_si $_so" > "$VM_STATE"

# ── ping 默认网关（1 秒超时）──
_gw=$(ip route 2>/dev/null | awk "/^default/{print \$3; exit}")
_ping="--"
if [ -n "$_gw" ]; then
  if ping -c1 -W1 "$_gw" >/dev/null 2>&1; then _ping="ok"; else _ping="FAIL"; fi
fi

# ── conntrack ──
_conn="--"
if [ -r /proc/sys/net/netfilter/nf_conntrack_count ]; then
  _conn="$(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null)/$(cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null)"
fi

top5=$(ps -eo rss,pcpu,args --sort=-rss | awk 'NR>1 && NR<=7{
  name=$0
  if (name ~ /kline_bot_api/) n="kline"
  else if (name ~ /take_over_order_app/) n="backend"
  else if (name ~ /pusher[.]server/) n="pusher"
  else if (name ~ /modify_order_monitor_api/) n="modify"
  else if (name ~ /signal_bot_api/) n="signal"
  else if (name ~ /condition_process/) n="condition"
  else if (name ~ /take_over_manager_app/) n="manager"
  else n="other"
  printf "%s:%dMB/%s%% ", n, int($1/1024), $2
}')
_drd_kb=$((_drd_d/1024)); _dwr_kb=$((_dwr_d/1024))
_line="$ts load=$load1 cpu=${cpu}% wa=${_wa} D=${_dstate} mem=$mem swap=$swap disk_r=${_drd_kb}KB/s disk_w=${_dwr_kb}KB/s swap_in=${_si_d}pg/s swap_out=${_so_d}pg/s net_rx=${_drx}B/s net_tx=${_dtx}B/s err=${_err} drop=${_drop} ping=${_ping} conn=${_conn} top5: $top5"
echo "$_line" >> "$LOG"
echo "$_line" > "$LAST_LINE"
sync -d "$LOG" 2>/dev/null || sync

# ── 内核 OOM / 网卡报错抄录（journald 被杀也能读）──
_oom="$(dmesg -T 2>/dev/null | grep -iE "out of memory|killed process" | tail -20)"
if [ -n "$_oom" ]; then
  _last=""
  [ -f "$OOM_LOG" ] && _last=$(tail -20 "$OOM_LOG" 2>/dev/null)
  [ "$_oom" != "$_last" ] && printf "%s\n--- %s ---\n" "$_oom" "$ts" >> "$OOM_LOG"
fi
_kern="$(dmesg -T 2>/dev/null | grep -iE "NETDEV WATCHDOG|virtio_net|tx timeout|link is not ready|link up|link down|eth0:.*(error|reset)" | tail -10)"
if [ -n "$_kern" ]; then
  _lastk=""
  [ -f "$KERNEL_LOG" ] && _lastk=$(tail -10 "$KERNEL_LOG" 2>/dev/null)
  [ "$_kern" != "$_lastk" ] && printf "%s\n--- %s ---\n" "$_kern" "$ts" >> "$KERNEL_LOG"
fi

lines=$(wc -l < "$LOG" 2>/dev/null || echo 0)
if [ "$lines" -gt 20000 ]; then tail -n 10000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"; fi

# ── 内存告警（**只看可用内存**：avail<=LIMIT 连续 2 次 ⇒ 推送；30 分钟冷却）──
# ⚠️ 2026-10-29 用户裁定：**不再用 swap_out>0 触发**。
#   是否开始换页与平台/内核调度相关（同一负载在不同机型上表现不同）⇒ 旧逻辑会频繁误报；
#   swap_in/out 仍保留在日志与消息里，但**只作诊断信息，不参与触发** ✗。
LIMIT="${MEMWATCH_AVAIL_LIMIT:-150}"
STREAK="$ROOT/log/.memalert_streak"
_hit=0
if [ "${avail:-9999}" -le "$LIMIT" ]; then
  _hit=1
fi
if [ "$_hit" = "1" ]; then
  _streak=$(( $(cat "$STREAK" 2>/dev/null || echo 0) + 1 ))
  echo "$_streak" > "$STREAK"
else
  echo 0 > "$STREAK"
  _streak=0
fi
if [ "$_streak" -ge 2 ]; then
  last=0
  [ -f "$STATE" ] && last=$(cat "$STATE" 2>/dev/null || echo 0)
  if [ $((_now - last)) -ge 1800 ]; then
    echo "$_now" > "$STATE"
    _why="可用 ${avail}MB（阈值 ${LIMIT}MB）"
    msg="⚠️ 内存预警：${_why}
负载=${load1} CPU=${cpu}% wa=${_wa} D=${_dstate}
${mem} swap=${swap} disk_r=${_drd_kb}KB/s disk_w=${_dwr_kb}KB/s swap_in=${_si_d}pg/s swap_out=${_so_d}pg/s
TOP: ${top5}"
    curl -s -m 5 -X POST http://127.0.0.1:5100/enqueue       -H "Content-Type: application/json"       -d "{\"send_type\":\"normal\",\"msg\":$(printf "%b" "$msg" | /usr/bin/python3 -c "import json,sys;print(json.dumps(sys.stdin.read()))")}"       >> "$ROOT/log/memalert.log" 2>&1 || true
    echo "$ts ALERT ${_why} pushed" >> "$ROOT/log/memalert.log"
  fi
fi
