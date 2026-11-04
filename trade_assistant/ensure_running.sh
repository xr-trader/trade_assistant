#!/bin/bash

set -eu

PYTHON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$PYTHON_DIR/config.sh"

LOCK_FILE="$RUN_DIR/ensure_running.lock"
RANDOM_START_MIN="${RANDOM_START_MIN:-1}"
RANDOM_START_MAX="${RANDOM_START_MAX:-5}"
BOOT_BACKEND_WAIT_SECONDS="${BOOT_BACKEND_WAIT_SECONDS:-30}"
# pusher（uvicorn + 加载 .so）就绪等待窗口。慢机器/内存紧张时 15s 不够，会误判失败。
PUSHER_READY_WAIT_SECONDS="${PUSHER_READY_WAIT_SECONDS:-60}"

usage() {
  echo "Usage: bash ensure_running.sh [all|backend|workers|service_name]"
  exit 1
}

acquire_lock() {
  ensure_runtime_dirs
  # 兼容迁移：旧版 mkdir 锁是目录格式（内含 pid 文件），不可能有 flock 持有者，
  # 直接 rm -rf 清理残留；新版 flock 文件锁不动，保持互斥
  if [ -d "$LOCK_FILE" ]; then
    rm -rf "$LOCK_FILE" 2>/dev/null || true
  fi
  # flock 文件锁：⚠️ 锁随 fd 9 关闭而释放 —— 所以 **service.sh 启动守护进程时必须
  # `9>&-`**，否则长活的守护进程会继承 fd 9，本脚本退出后锁仍被它持有，之后每次
  # ensure_running 都判为 "already running" 直接退出 ⇒ 保活失效、服务再也拉不起来。
  exec 9>"$LOCK_FILE" 2>/dev/null || exit 0
  if ! flock -n 9; then
    echo "ensure_running is already running"
    exit 0
  fi
}

random_sleep() {
  local min="$1"
  local max="$2"
  if [ "$max" -lt "$min" ]; then
    max="$min"
  fi
  local delay=$(( min + RANDOM % (max - min + 1) ))
  sleep "$delay"
}

backend_ready() {
  local status
  local port="${BACKEND_PORT:-8000}"
  status="$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${port}/check_status" || true)"
  # 000 表示无响应（连接拒绝），其他任何状态码都说明后端已启动
  [ "$status" != "000" ]
}

wait_backend_ready() {
  local waited=0
  while [ "$waited" -lt "$BOOT_BACKEND_WAIT_SECONDS" ]; do
    if backend_ready; then
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  return 1
}

start_backend_if_needed() {
  bash "$PYTHON_DIR/service.sh" start backend
  if ! wait_backend_ready; then
    echo "backend did not become ready within ${BOOT_BACKEND_WAIT_SECONDS}s"
    return 1
  fi
}

wait_pusher_ready() {
  # uvicorn 启动后需要时间绑定端口 + 完成 lifespan 初始化
  sleep 2
  local waited=0
  local max_wait="$PUSHER_READY_WAIT_SECONDS"
  while [ "$waited" -lt "$max_wait" ]; do
    local status
    status="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:5100/health" 2>/dev/null || true)"
    if [ "$status" = "200" ]; then
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  echo "pusher did not become ready within ${max_wait}s"
  return 1
}

shuffle_workers() {
  # ★ 2026-10-14：`signal_bot` 必须在这里**显式列出** —— `all` 分支只跑本清单，
  #   漏了它 ⇒ 新服务**不会被保活拉起** ✗（config.sh 的 PYTHON_SERVICES 只用于 status/stop 遍历）
  #   回归：tests/local_verify_signal_bot_service.py 断言「PYTHON_SERVICES 里的每个
  #   worker 都出现在这里」（防再漏 ✓）
  printf '%s\n' kline_bot modify_order signal_bot condition | awk 'BEGIN{srand()} {print rand() "\t" $0}' | sort -k1,1n | cut -f2-
}

start_workers_if_needed() {
  local service
  while IFS= read -r service; do
    [ -n "$service" ] || continue
    random_sleep "$RANDOM_START_MIN" "$RANDOM_START_MAX"
    bash "$PYTHON_DIR/service.sh" start "$service"
    # 验证 worker 是否真正启动成功
    if ! bash "$PYTHON_DIR/service.sh" status "$service" >/dev/null 2>&1; then
      echo "[ensure_running] 警告: $service 启动后未检测到运行状态" >&2
    fi
  done < <(shuffle_workers)

  # automonitor 已移除
  random_sleep "$RANDOM_START_MIN" "$RANDOM_START_MAX"
}

TARGET="${1:-all}"

acquire_lock

case "$TARGET" in
  all)
    bash "$PYTHON_DIR/service.sh" start pusher
    # ⚠️ 就绪等待失败**只告警、不中断**（`|| echo`）：
    # 本脚本是 `set -e`，若这里直接 return 1，backend 与 4 个 worker 会**全部不启动**，
    # 于是一次「慢启动」就被放大成整套服务下线（2026-09-29 事故）。
    wait_pusher_ready || echo "[ensure_running] 警告: pusher 未在 ${PUSHER_READY_WAIT_SECONDS}s 内就绪，继续启动其余服务" >&2
    start_backend_if_needed || echo "[ensure_running] 警告: backend 未在 ${BOOT_BACKEND_WAIT_SECONDS}s 内就绪，继续启动 worker" >&2
    start_workers_if_needed
    ;;
  backend)
    start_backend_if_needed
    ;;
  workers)
    start_workers_if_needed
    ;;
  pusher)
    bash "$PYTHON_DIR/service.sh" start pusher
    ;;
  *)
    service_exists "$TARGET" || usage
    if [ "$TARGET" = "backend" ]; then
      start_backend_if_needed
    else
      bash "$PYTHON_DIR/service.sh" start "$TARGET"
    fi
    ;;
esac
