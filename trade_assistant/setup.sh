#!/bin/bash
# ==============================================================
# setup.sh — 一键部署（文件已上传到服务器后执行）
# 用法:
#   1. 将项目文件传到服务器任意目录 (scp / rsync)
#   2. cd 到项目目录
#   3. bash setup.sh      # 非 root 会自动 sudo 提权，阿里云/腾讯云同一条命令
#
# 系统要求（硬性，执行前会自检）: Ubuntu 24.04 LTS / x86_64(amd64) / Python 3.12
#   跳过自检: SKIP_ENV_CHECK=1 bash setup.sh
#
#   ★ 2026-10-08 新增**可选跳过开关**（默认全 0 = 行为与此前**完全一致**，生产流程零影响）。
#     用途：在同一台机上**并行部署一份用于验证**（不打扰正在运行的实例）。
#       SKIP_STATIC=1     跳过前端静态同步（**并跳过它会触发的 stop.sh**）
#       SKIP_NGINX=1      跳过 nginx 配置生成/站点禁用/reload
#       SKIP_CRON=1       跳过写 crontab 保活（**不碰你现有的保活条目**）
#       SKIP_SERVICES=1   跳过启动服务（也不 stop —— 别人正在跑）
#       SKIP_FIREWALL=1   跳过 selinux/防火墙
#       ⚠️ 只做**验证**时用；正式部署**不要**跳过这些，否则服务不会被保活/代理
# ==============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$SCRIPT_DIR"

# ── 默认配置（可通过环境变量覆盖） ──
NGINX_WEB_ROOT="${NGINX_WEB_ROOT:-/usr/share/nginx/takeoverorder}"
NGINX_CONF_PATH="${NGINX_CONF_PATH:-/etc/nginx/conf.d/takeoverorder_static.conf}"
NGINX_HTTP_PORT="${NGINX_HTTP_PORT:-80}"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
SWAPFILE_PATH="${SWAPFILE_PATH:-/swapfile}"
SWAPFILE_SIZE="${SWAPFILE_SIZE:-2G}"
APP_SERVER_NAME="${APP_SERVER_NAME:-}"

PYTHON_BIN="/usr/bin/python3"

CRON_BOOT_JOB="@reboot bash $APP_ROOT/boot.sh >/dev/null 2>&1"
CRON_GUARD_JOB="* * * * * bash $APP_ROOT/ensure_running.sh all >/dev/null 2>&1"
CRON_KLINE_RESTART="0 3 * * * bash $APP_ROOT/restart_kline_bot_daily.sh >> $APP_ROOT/log/kline_bot_restart.log 2>&1"
# ★ 2026-10-27：内存/CPU 监控（每分钟 + 开机标记）——用户报障时先看 log/memwatch.log ✓
CRON_MEMWATCH_MIN="* * * * * bash $APP_ROOT/memwatch.sh >/dev/null 2>&1"
CRON_MEMWATCH_BOOT="@reboot bash $APP_ROOT/memwatch.sh --boot >/dev/null 2>&1"
CRON_MEMWATCH_HALF="* * * * * sleep 30; bash $APP_ROOT/memwatch.sh >/dev/null 2>&1"
# ★ 2026-10-27：每周日 09:30 安全更新窗口（只让路、不退出程序）
CRON_APT_WINDOW="30 9 * * 0 bash $APP_ROOT/apt_window.sh >> $APP_ROOT/log/apt_window.log 2>&1"
# 退役服务清单（本条**只用于"安装时剪掉历史保活行"**）：新装只写上面三条（boot / `all` / daily），
# 但**老机器的 crontab 可能还留着按服务一行的历史保活**（如 `ensure_running.sh signal_monitor`）
# ⇒ 不剪掉的话退役服务会被 cron 重新拉起、与 `signal_bot` 双跑 ✗（2026-10-12 真机事故成因之一）。
# ⚠️ 升级路径的权威定义在 **deploy/dist/upgrade.sh** 的 `RETIRED_SERVICES`（用户 2026-10-14 裁定
#    「在 upgrade.sh 做处理」）；这里是**安装路径**的同款剪枝 ⇒ **两处清单必须一致，改一处要同时改另一处**。
RETIRED_SERVICES="${RETIRED_SERVICES:-signal_monitor}"

# ── 颜色输出 ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

info()  { printf "${GREEN}[INFO]${NC}  %s\n" "$*"; }
warn()  { printf "${YELLOW}[WARN]${NC}  %s\n" "$*"; }
err()   { printf "${RED}[ERROR]${NC} %s\n" "$*"; }

# ── 权限：非 root 自动提权 ────────────────────────────────────────────────────
# 阿里云轻量默认 root 登录、腾讯云轻量默认 ubuntu 登录 ⇒ 手动上传后 `bash setup.sh`
# 两边都能直接跑。install.sh 内部调用本脚本时已经是 root，这里自然跳过（行为不变）。
if [[ "$(id -u)" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1 && [[ -f "${BASH_SOURCE[0]}" && -r "${BASH_SOURCE[0]}" ]]; then
        info "当前用户非 root，正在通过 sudo 提权后继续部署..."
        # ⚠️ sudo 默认 env_reset，必须显式透传下面这些可覆盖配置，否则用户设的值会被丢掉
        exec sudo -- env \
            NGINX_WEB_ROOT="$NGINX_WEB_ROOT" \
            NGINX_CONF_PATH="$NGINX_CONF_PATH" \
            NGINX_HTTP_PORT="$NGINX_HTTP_PORT" \
            BACKEND_HOST="$BACKEND_HOST" \
            BACKEND_PORT="$BACKEND_PORT" \
            SWAPFILE_PATH="$SWAPFILE_PATH" \
            SWAPFILE_SIZE="$SWAPFILE_SIZE" \
            APP_SERVER_NAME="$APP_SERVER_NAME" \
            SKIP_ENV_CHECK="${SKIP_ENV_CHECK:-0}" \
            SKIP_FIREWALL="${SKIP_FIREWALL:-0}" \
            SKIP_SERVICES="${SKIP_SERVICES:-0}" \
            SKIP_CRON="${SKIP_CRON:-0}" \
            SKIP_NGINX="${SKIP_NGINX:-0}" \
            SKIP_STATIC="${SKIP_STATIC:-0}" \
            bash "${BASH_SOURCE[0]}" "$@"
    fi
    err "部署需要 root 权限（当前用户: $(id -un)）"
    err "请任选其一: ① sudo bash ${BASH_SOURCE[0]}   ② sudo -i 后重新执行"
    exit 1
fi

# ── 辅助函数 ──

command_exists() { command -v "$1" >/dev/null 2>&1; }

# ── 环境自检 ────────────────────────────────────────────────────────────────
# ⚠️ 与 install.sh 的 verify_env 保持一致（两处各自独立拦截，改一处必须同步另一处）
# 为什么硬拦：程序以 Cython 编译且去掉了 ABI 后缀，任何 Python 都会尝试加载，
# 环境不符时的失败形态是 "undefined symbol: _PyObject_NextNotImplemented"，
# 不是「模块找不到」，排查成本极高 ⇒ 装之前就拦住。
verify_env() {
  if [ "${SKIP_ENV_CHECK:-0}" = "1" ]; then
    info "环境自检已跳过（SKIP_ENV_CHECK=1）"
    return 0
  fi

  local os_pretty os_id os_ver arch py_ver="未安装"
  os_pretty="$(awk -F= '$1=="PRETTY_NAME"{gsub(/"/,"");print $2;exit}' /etc/os-release 2>/dev/null || true)"
  os_id="$(awk -F= '$1=="ID"{gsub(/"/,"");print $2;exit}' /etc/os-release 2>/dev/null || true)"
  os_ver="$(awk -F= '$1=="VERSION_ID"{gsub(/"/,"");print $2;exit}' /etc/os-release 2>/dev/null || true)"
  arch="$(uname -m 2>/dev/null || true)"
  if [ -x "$PYTHON_BIN" ]; then
    py_ver="$("$PYTHON_BIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "无法执行")"
  fi

  local problems=""
  [ "$os_id" = "ubuntu" ] || problems="${problems}      - 需要 Ubuntu 系统（当前: ${os_pretty:-未知}）\n"
  case "$os_ver" in
    24.04*) ;;
    *) problems="${problems}      - 需要 Ubuntu 24.04 LTS（当前: ${os_pretty:-未知}）\n" ;;
  esac
  case "$arch" in
    x86_64|amd64) ;;
    *) problems="${problems}      - 需要 x86_64(amd64) 架构（当前: ${arch:-未知}）\n" ;;
  esac
  [ "$py_ver" = "3.12" ] || problems="${problems}      - 需要 Python 3.12（当前: ${PYTHON_BIN} → ${py_ver}）\n"

  info "环境自检: ${os_pretty:-未知} / ${arch:-未知} / Python ${py_ver}"

  if [ -n "$problems" ]; then
    printf '%b' "$problems"
    err "系统环境不满足要求，已停止部署。"
    err "程序按 x86_64 + Python 3.12 编译，环境不符时装完服务起不来。"
    err "请使用 Ubuntu 24.04 LTS（x86_64）的服务器；确需强行部署可 SKIP_ENV_CHECK=1 跳过自检。"
    exit 1
  fi

  # 兜底提醒：服务由 config.sh 以 /usr/bin/python3 启动，但 upgrade.sh 会用 PATH 里的
  # python3 跑升级状态写入（同样是 .so），PATH 里若是别的 Python 会导致升级状态写不进去。
  local path_py path_py_ver=""
  path_py="$(command -v python3 2>/dev/null || true)"
  if [ -n "$path_py" ] && [ "$path_py" != "$PYTHON_BIN" ] && [ -x "$path_py" ]; then
    path_py_ver="$("$path_py" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)"
    [ "$path_py_ver" = "3.12" ] || \
      warn "PATH 中的 python3 指向 ${path_py} (${path_py_ver:-?})，非 3.12；可能影响升级状态写入"
  fi
}

# create_app_user() 已移除 — 统一使用 root 运行

# 等待 dpkg/apt 锁释放（应对系统自动更新 unattended-upgrades 占用锁导致安装失败）
wait_for_dpkg_lock() {
  local wait_seconds="${DPKG_LOCK_WAIT:-180}"
  local waited=0
  local locked=1
  while [ "$waited" -lt "$wait_seconds" ]; do
    locked=1
    if command_exists fuser; then
      fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/cache/apt/archives/lock >/dev/null 2>&1 || locked=0
    else
      # 无 fuser 时退化为进程检测
      if ! pgrep -x apt-get >/dev/null 2>&1 && ! pgrep -x apt >/dev/null 2>&1 && \
         ! pgrep -x dpkg >/dev/null 2>&1 && ! pgrep -f "unattended-upgr" >/dev/null 2>&1; then
        locked=0
      fi
    fi
    [ "$locked" -eq 0 ] && break
    if [ "$waited" -eq 0 ]; then
      info "检测到 dpkg/apt 锁被占用（可能是系统自动更新），等待释放..."
    fi
    sleep 5
    waited=$((waited + 5))
  done
  if [ "$locked" -eq 1 ]; then
    warn "dpkg 锁等待超时（${wait_seconds}s），继续执行（可能失败）"
  fi
}

install_python_deps() {
  # 已安装标记，避免每次重复 pip install
  if [ -f "$APP_ROOT/.deps_installed" ]; then
    info "Python 依赖已安装，跳过"
    return 0
  fi
  info "安装 Python 依赖..."
  wait_for_dpkg_lock
  apt-get update -qq
  apt-get install -y python3-pip python3-dev curl iputils-ping iproute2 unattended-upgrades >/dev/null
  # PEP 668（Ubuntu 24.04+）：项目独占使用，--break-system-packages 安全
  pip3 install --break-system-packages -r "$APP_ROOT/requirements.txt"
  touch "$APP_ROOT/.deps_installed"
  info "Python 依赖安装完成（系统 Python 3）"
}

check_swap() {
  # 机器已启用【任意】swap 就直接复用。
  # ⚠️ 原先只按 $SWAPFILE_PATH 名字判断（grep /swapfile），会漏掉系统镜像自带的 swap：
  #    腾讯云 Ubuntu 镜像自带 /swap.img ⇒ 判定"没有 swap"⇒ 又 fallocate 一个 /swapfile，
  #    结果机器上两份 swap 并存、fstab 多一条。阿里云镜像默认无 swap，所以主站不暴露这个问题。
  local existing
  existing="$(swapon --show --noheadings 2>/dev/null | awk 'NF {printf "%s(%s) ", $1, $3}')"
  if [ -n "$existing" ]; then
    info "系统已有 swap，跳过创建: ${existing% }"
    return 0
  fi
  if [ ! -f "$SWAPFILE_PATH" ]; then
    info "创建 swap 文件: ${SWAPFILE_SIZE}..."
    fallocate -l "$SWAPFILE_SIZE" "$SWAPFILE_PATH"
    chmod 600 "$SWAPFILE_PATH"
    mkswap "$SWAPFILE_PATH" >/dev/null
  fi
  swapon "$SWAPFILE_PATH" 2>/dev/null || true
  if ! grep -q "^$SWAPFILE_PATH " /etc/fstab 2>/dev/null; then
    echo "$SWAPFILE_PATH none swap sw 0 0" >> /etc/fstab
  fi
  info "swap 设置完成"
}

install_nginx_if_needed() {
  if command_exists nginx; then
    info "nginx 已安装"
    return 0
  fi
  info "安装 nginx..."
  if command_exists yum; then
    yum -y install nginx || { yum -y install epel-release; yum -y install nginx; }
  elif command_exists apt-get; then
    wait_for_dpkg_lock
    apt-get update -qq && apt-get -y install nginx
  else
    err "无法安装 nginx，请手动安装后重试"
    exit 1
  fi
}

# create_venv 已移除 — Ubuntu 24.04 系统 Python（3.12）直接可用，配合 --break-system-packages（PEP 668）

sync_static_files() {
  local source_dist="$APP_ROOT/dist_temp"
  if [ ! -d "$source_dist" ] || [ -z "$(ls -A "$source_dist" 2>/dev/null)" ]; then
    warn "静态资源目录不存在或为空: ${source_dist}，跳过（保留远端现有文件）"
    return 0
  fi
  info "同步前端静态文件到 nginx..."
  mkdir -p "$NGINX_WEB_ROOT"
  if command_exists rsync; then
    rsync -a "$source_dist"/ "$NGINX_WEB_ROOT"/dist_temp/ 2>/dev/null
  else
    cp -a "$source_dist"/. "$NGINX_WEB_ROOT"/dist_temp/ 2>/dev/null
  fi
  chmod -R a+rX "$NGINX_WEB_ROOT"
}

generate_nginx_conf() {
  if [ ! -f "$APP_ROOT/sync_nginx_api_routes.py" ]; then
    warn "sync_nginx_api_routes.py 不存在，跳过 nginx 配置生成"
    return 0
  fi
  info "生成 nginx 配置..."

  # 确定服务器名称
  local server_name="$APP_SERVER_NAME"
  if [ -z "$server_name" ]; then
    server_name="$(curl -s --max-time 3 ifconfig.me 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')"
    server_name="${server_name:-localhost}"
  fi

  # 检测 TakeoverManageServer 目录
  local manager_dir="$(cd "$APP_ROOT/../TakeoverManageServer" 2>/dev/null && pwd || true)"

  APP_SERVER_NAME="$server_name" \
  NGINX_WEB_ROOT="$NGINX_WEB_ROOT" \
  BACKEND_HOST="$BACKEND_HOST" \
  BACKEND_PORT="$BACKEND_PORT" \
  NGINX_API_PORT="$NGINX_HTTP_PORT" \
  NGINX_CONF_PATH="$NGINX_CONF_PATH" \
  MANAGER_APP_ROOT="${manager_dir:-}" \
  "$PYTHON_BIN" "$APP_ROOT/sync_nginx_api_routes.py"
}

disable_conflicting_nginx_sites() {
  local candidates=(
    /etc/nginx/conf.d/default.conf
    /etc/nginx/default.d/default.conf
    /etc/nginx/sites-enabled/default
  )
  local file bak_target
  for file in "${candidates[@]}"; do
    [ -f "$file" ] || continue
    # sites-enabled 下的备份必须移出该目录（nginx include sites-enabled/* 会加载所有文件，
    # 残留的 default_server 会抢占默认站点导致 API 转发失效）
    if [[ "$file" == /etc/nginx/sites-enabled/* ]]; then
      bak_target="/etc/nginx/sites-available/$(basename "$file").bak_takeover_$(date +%s)"
    else
      bak_target="${file}.bak_takeover_$(date +%s)"
    fi
    mv "$file" "$bak_target" 2>/dev/null || true
    info "禁用冲突配置: $file -> $bak_target"
  done

  # 清理历史遗留：sites-enabled 下旧的 .bak_takeover_* 备份（防重装时误加载）
  for file in /etc/nginx/sites-enabled/*.bak_takeover_*; do
    [ -f "$file" ] || continue
    mv "$file" "/etc/nginx/sites-available/$(basename "$file")" 2>/dev/null || true
    info "移出历史备份: $file"
  done

  # 查找其他占用 8000 端口的 nginx 配置
  for dir in /etc/nginx/conf.d /etc/nginx/default.d /etc/nginx/sites-enabled; do
    [ -d "$dir" ] || continue
    for file in "$dir"/*.conf; do
      [ -f "$file" ] || continue
      [ "$file" = "$NGINX_CONF_PATH" ] && continue
      if grep -q "proxy_pass http://127.0.0.1:8000" "$file" 2>/dev/null; then
        mv "$file" "${file}.bak_takeover_$(date +%s)" 2>/dev/null || true
        info "禁用冲突配置: $file"
      fi
    done
  done
}

configure_selinux() {
  # Ubuntu 24.04 默认没有 selinux，直接跳过
  command_exists getenforce || return 0
  local mode
  mode="$(getenforce 2>/dev/null || true)"
  [ "$mode" = "Disabled" ] && return 0
  command_exists setsebool && setsebool -P httpd_can_network_connect 1 >/dev/null 2>&1 || true
}

open_firewall() {
  # Ubuntu 24.04 云服务器一般用安全组，没有 firewalld，直接跳过
  command_exists firewall-cmd || return 0
  systemctl is-active firewalld >/dev/null 2>&1 || return 0
  firewall-cmd --permanent --add-service=http >/dev/null 2>&1 || true
  firewall-cmd --reload >/dev/null 2>&1 || true
}

reload_nginx() {
  nginx -t || { err "nginx 配置检查失败"; return 1; }
  if pgrep -x nginx >/dev/null 2>&1; then
    nginx -s reload >/dev/null 2>&1 || true
    info "nginx 已重新加载"
  else
    nginx >/dev/null 2>&1 || systemctl start nginx 2>/dev/null || service nginx start 2>/dev/null || true
    info "nginx 已启动"
  fi
  if command_exists systemctl; then
    systemctl enable nginx >/dev/null 2>&1 || true
  fi
}

# install_systemd_services 已移除 — 统一使用 crontab ensure_running.sh 保活

enable_persistent_journal() {
  # ★ 2026-10-27：让 journal 持久化（否则重启后 `journalctl -b -1` 查不到卡死前日志 ✓）
  if [ -d /var/log/journal ]; then
    return 0
  fi
  mkdir -p /var/log/journal
  if grep -q "^Storage=" /etc/systemd/journald.conf 2>/dev/null; then
    sed -i "s/^Storage=.*/Storage=persistent/" /etc/systemd/journald.conf
  else
    echo "Storage=persistent" >> /etc/systemd/journald.conf
  fi
  systemctl restart systemd-journald >/dev/null 2>&1 || true
  info "已开启 journal 持久化（/var/log/journal）"
}

disable_auto_updates() {
  # ★ 2026-10-27（用户裁定）：关掉系统自动更新/固件刷新，统一改走每周 apt 维护窗口
  #   （144 曾因 apt/fwupd 与 kline 全量刷新撞车导致卡死；窗口只让路、不退出程序 ✓）
  systemctl disable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true
  systemctl disable --now unattended-upgrades >/dev/null 2>&1 || true
  systemctl disable --now fwupd-refresh.timer >/dev/null 2>&1 || true
  systemctl mask fwupd-refresh.service >/dev/null 2>&1 || true
  info "已关闭系统自动更新/固件刷新（改由 apt_window.sh 每周日 09:30 执行安全更新）"
}

set_crontab() {
  # 安装 systemd 服务（如果可用）


  # 无论是否有 systemd，都加 crontab 保活兜底
  # systemd Restart=always 对 SIGTERM 等场景可能不生效
  local current
  current="$(crontab -l 2>/dev/null || true)"
  current="$(printf "%s\n" "$current" | grep -vF "$CRON_BOOT_JOB" | grep -vF "$CRON_GUARD_JOB" | grep -vF "$CRON_KLINE_RESTART" | grep -vF "memwatch.sh" | grep -vF "apt_window.sh" || true)"
  # ★ 剪掉退役服务的历史保活行（规则与 deploy/dist/upgrade.sh 的 filter_retired_services 一致：
  #   行内出现退役服务名且该名字处于**词首边界** ⇒ 删；不含该名字的行一律原样保留 ✓）
  # 幂等：没有这类行 ⇒ 文本不变 ⇒ 写回的仍是原内容 ✓
  local _svc _pat
  for _svc in $RETIRED_SERVICES; do
    [ -n "$_svc" ] || continue
    _pat="(^|[^A-Za-z0-9_])${_svc}"
    current="$(printf "%s\n" "$current" | grep -vE "$_pat" || true)"
  done
  {
    printf "%s\n" "$current"
    printf "%s\n" "$CRON_BOOT_JOB"
    printf "%s\n" "$CRON_GUARD_JOB"
    printf "%s\n" "$CRON_KLINE_RESTART"
    printf "%s\n" "$CRON_MEMWATCH_MIN"
    printf "%s\n" "$CRON_MEMWATCH_BOOT"
    printf "%s\n" "$CRON_MEMWATCH_HALF"
    printf "%s\n" "$CRON_APT_WINDOW"
  } | awk 'NF' | crontab -
  info "crontab 已设置（开机自启 + 进程保活 + kline_bot 每日重启 + memwatch）"
}

start_services() {
  info "启动服务..."
  # 首次安装属**计划内**启动 ⇒ 后端不生成/不推送应急刷新密钥
  # （用户 2026-10-28：只有被保活/开机拉起时才推送；装的人在现场，不需要钥匙）
  export XIAOER_START_REASON="${XIAOER_START_REASON:-install}"
  # 清理残留的 ensure_running 锁（防止旧进程导致启动跳过；目录/文件格式都兼容）
  local lock_dir="$APP_ROOT/run/ensure_running.lock"
  if [ -e "$lock_dir" ]; then
    local old_pid
    old_pid=$(cat "$lock_dir/pid" 2>/dev/null || true)
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
      kill "$old_pid" 2>/dev/null || true
      sleep 1
      kill -9 "$old_pid" 2>/dev/null || true
    fi
    rm -rf "$lock_dir"
  fi
  pkill -f "ensure_running.sh" 2>/dev/null || true

  bash "$APP_ROOT/stop.sh" 2>/dev/null || true
  sleep 1
  bash "$APP_ROOT/boot.sh" 2>/dev/null || {
    warn "boot.sh 执行失败，尝试 ensure_running..."
    bash "$APP_ROOT/ensure_running.sh" all 2>/dev/null || true
  }
}

print_summary() {
  local ip
  ip="$(curl -4 -s --max-time 3 ifconfig.me 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')"
  echo ""
  echo "========================================="
  echo "       部署完成"
  echo "========================================="
  echo ""
  echo "  应用目录:   $APP_ROOT"
  echo "  Python:      /usr/bin/python3（系统 3.12）"
  echo "  后端监听:   ${BACKEND_HOST}:${BACKEND_PORT}"
  echo "  网页端口:   80（HTTP 静态文件）"
  if [ "$NGINX_HTTP_PORT" = "80" ]; then
    echo "  API 端口:   80（与网页共用）"
  else
    printf "${RED}  API 端口:   %s（非默认端口！）${NC}\n" "$NGINX_HTTP_PORT"
    printf "${RED}  ⚠ 请在云服务器安全组 / 防火墙中放行 TCP %s 端口${NC}\n" "$NGINX_HTTP_PORT"
    printf "${RED}  ⚠ 例如: firewall-cmd --add-port=%s/tcp${NC}\n" "$NGINX_HTTP_PORT"
  fi
  echo "  静态目录:   $NGINX_WEB_ROOT/dist_temp"
  echo ""
  echo "-----------------------------------------"
  echo "  访问地址:   http://${ip:-<服务器IP>}"
  echo "  API 地址:   http://${ip:-<服务器IP>}:${NGINX_HTTP_PORT}"
  echo "-----------------------------------------"
  echo ""
  echo "  常用命令:"
  echo "    查看状态:   bash $APP_ROOT/status.sh all"
  echo "    查看日志:   bash $APP_ROOT/show_logs.sh all -f"
  echo "    启动服务:   bash $APP_ROOT/boot.sh"
  echo "    停止服务:   bash $APP_ROOT/stop.sh"
  echo "    重启服务:   bash $APP_ROOT/restart.sh"
  echo ""
  echo "  已配置开机自启、进程保活（crontab → ensure_running.sh）" 
  echo "========================================="
  echo ""
}

# ── 主流程 ──

main() {
  # 检查必需文件
  if [ ! -f "$APP_ROOT/requirements.txt" ]; then
    err "requirements.txt 不存在，请确认在项目根目录执行"
    exit 1
  fi

  echo ""
  info "=== trade_assistant 一键部署 ==="
  echo ""

  # 0. 环境自检（不满足直接退出，避免装完服务起不来的隐性故障）
  verify_env

  # 1. 系统基础
  check_swap
  wait_for_dpkg_lock
  apt-get update -qq
  install_nginx_if_needed

  # 交互式选择 API 代理端口（10 秒倒计时，默认 80）
  if [ -t 0 ]; then
    echo ""
    info "网页端口固定为 80，API 代理默认复用 80（10 秒倒计时）"
    user_port=""
    for i in $(seq 10 -1 1); do
      printf "\r${YELLOW}  [%2d 秒] 回车使用 80，或输入自定义端口: ${NC}" "$i"
      if read -r -t 1 user_input < /dev/tty 2>/dev/null; then
        user_port="$user_input"
        echo ""
        break
      fi
    done
    if [ -z "$user_port" ]; then
      printf "\r%*s\r" 80 ""
      NGINX_HTTP_PORT="80"
      info "使用默认端口: 网页 80，API 代理 80"
    else
      NGINX_HTTP_PORT="$user_port"
      info "网页端口 80，API 代理端口: $NGINX_HTTP_PORT"
    fi
  fi

  # 2. Python 依赖
  install_python_deps

  # 3. 确保数据目录权限

  # 4. 前端静态文件
  if [ "${SKIP_STATIC:-0}" = "1" ]; then
    info "SKIP_STATIC=1 ⇒ 跳过前端静态同步"
  else
    sync_static_files
  fi
  # 确保数据目录可写（二次授权，避免 rsync 等操作后权限丢失）
  mkdir -p "$APP_ROOT/configs" "$APP_ROOT/json" "$APP_ROOT/exchange/okx_data"
  if [ "${SKIP_STATIC:-0}" = "1" ] || [ "${SKIP_SERVICES:-0}" = "1" ]; then
    info "跳过 stop.sh（SKIP_STATIC=1 或 SKIP_SERVICES=1 —— 可能有别的实例在跑）"
  else
    # 先停服避免 chown 与 kline_bot 原子写入的 .tmp 竞态
    bash "$APP_ROOT/stop.sh" 2>/dev/null || true
  fi
  # chown takeover 已移除 — 统一使用 root

  # 5. Nginx 配置
  if [ "${SKIP_NGINX:-0}" = "1" ]; then
    info "SKIP_NGINX=1 ⇒ 跳过 nginx 配置（不动你现有的站点）"
  else
    generate_nginx_conf
    disable_conflicting_nginx_sites
  fi

  # 6. 系统安全配置
  if [ "${SKIP_FIREWALL:-0}" = "1" ]; then
    info "SKIP_FIREWALL=1 ⇒ 跳过 selinux/防火墙"
  else
    configure_selinux
    open_firewall
  fi

  # 7. 服务保活
  if [ "${SKIP_CRON:-0}" = "1" ]; then
    info "SKIP_CRON=1 ⇒ 跳过 crontab（**不碰现有保活**）"
  else
    enable_persistent_journal
    disable_auto_updates
    set_crontab
  fi

  # 8. 启动服务
  if [ "${SKIP_SERVICES:-0}" = "1" ]; then
    info "SKIP_SERVICES=1 ⇒ 跳过启动服务"
  else
    start_services
  fi

  # 9. 加载 nginx
  if [ "${SKIP_NGINX:-0}" = "1" ]; then
    :
  else
    reload_nginx
  fi

  # 10. 完成
  print_summary
}

main "$@"
