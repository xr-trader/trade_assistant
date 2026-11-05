#!/usr/bin/env bash
# =============================================================================
# trade_assistant — 安装入口（**转发壳**）
#
# ⚠️ 2026-10-11 改造（用户裁定：「downloads 退出舞台」）
#
#   本文件**不再是**安装逻辑本体，降级成一个**转发壳**：
#   它只做一件事 —— 把权威安装器 `$BASE_URL/install.sh` 拉下来执行 ✓
#
#   【为什么必须改】
#     · 旧版本文件是「zip 线」安装器，第 25 行写死
#         ZIP_URL=http://<内部服务器>/downloads/okx_assistant.zip
#       ⇒ 从**已停更的旧分发线**拉**老包**（其自带 `APP_ROOT` 默认 `/root/okx_assistant`），
#         这是「随时可能被拉到老参数」的入口 ✗
#     · 本文件会被**打包进 trade_assistant.tar.gz**，每次标准升级都会铺到各机器
#       ⇒ 只在某台机器上就地改是**治标**（下次升级就被覆盖回来）✗ ⇒ 必须在这里改 ✓
#
# ⚠️ 2026-11-04（用户裁定）：本文件**不再随包分发** ✗（`publish.sh` 已排除）
#   保留在仓库里只作「应急手动入口」（需要时 scp 到机器上跑一次）；
#   日常重装请直接：`curl -fsSL <base>/install.sh -o /tmp/i.sh && bash /tmp/i.sh`
#
# 用户视角命令（2026-11-04 起走 GitHub ✓）:
#   curl -fSL https://github.com/xr-trader/trade_assistant/releases/latest/download/install.sh -o /tmp/install_trade.sh && bash /tmp/install_trade.sh
#
# 可选环境变量:
#   BASE_URL      产物来源（默认 GitHub Release；装完的机器按 src/dist_channel.py 解析 ✓）
#   APP_ROOT      应用目录（默认 $INSTALL_ROOT/trade_assistant）
#   INSTALL_ROOT  父目录（默认 /root）
# =============================================================================
set -euo pipefail

BASE_URL_WAS_SET="${BASE_URL:-}"
INSTALL_ROOT="${INSTALL_ROOT:-/root}"
APP_ROOT="${APP_ROOT:-$INSTALL_ROOT/trade_assistant}"

# ── 分发基址（2026-11-04 用户裁定：「全新部署，统一走 github」✓）──────────────
#   · 优先从**已安装的** `src/dist_channel.py` 取 —— 地址只在那一个文件里写死 ✓
#     （本文件升级后也躺在 `/root/trade_assistant/install.sh`，那时能 import ✓）
#   · 全新机器还没有源码 ⇒ 退回下面的同值字面量 ✓
#     ⚠️ 改地址 = 改 `src/dist_channel.py` 的 GITHUB_OWNER/GITHUB_REPO **和**这里的字面量（两处）
#   ⚠️ 231 的 `/trade/` 已**不是**安装源 ✗（它只给「测试用户」做**升级**源，由权限决定 ✓）
_DIST_LITERAL="https://github.com/xr-trader/trade_assistant/releases/latest/download"
if [ -z "$BASE_URL_WAS_SET" ]; then
  BASE_URL="$(cd "$APP_ROOT" 2>/dev/null && python3 -c 'from src.dist_channel import RELEASE_BASE; print(RELEASE_BASE)' 2>/dev/null || true)"
  [ -n "$BASE_URL" ] || BASE_URL="$_DIST_LITERAL"
fi

# 非 root ⇒ sudo 提权重跑（腾讯云默认 ubuntu 登录；阿里云默认 root，直接往下走）
if [ "$(id -u)" != "0" ]; then
  if command -v sudo >/dev/null 2>&1 && [ -f "$0" ] && [ -r "$0" ]; then
    echo "[install] 当前用户非 root ⇒ sudo 提权后继续"
    exec sudo -- env BASE_URL="$BASE_URL" INSTALL_ROOT="$INSTALL_ROOT" APP_ROOT="$APP_ROOT" bash "$0" "$@"
  fi
  echo "[install] ✗ 安装需要 root；建议：curl -fSL $BASE_URL/install.sh -o /tmp/install_trade.sh && sudo bash /tmp/install_trade.sh" >&2
  exit 1
fi

command -v curl >/dev/null 2>&1 || { echo "[install] ✗ 缺少 curl" >&2; exit 1; }

TMP="$(mktemp -d /tmp/trade_install_entry.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

echo "[install] 拉取权威安装器：$BASE_URL/install.sh"
if ! curl -fSL --connect-timeout 15 -o "$TMP/install.sh" "$BASE_URL/install.sh"; then
  echo "[install] ✗ 下载失败：$BASE_URL/install.sh（检查分发服务器是否可达）" >&2
  exit 1
fi

# 守卫：必须是新链路安装器（用 trade_assistant.tar.gz，而不是老 zip ✗）
# 目的：万一 BASE_URL 指到了老分发线，也**拒绝执行**，绝不按老参数装 ✗
if ! grep -q 'trade_assistant\.tar\.gz' "$TMP/install.sh"; then
  echo "[install] ✗ 下载到的安装器不是新链路（未见 trade_assistant.tar.gz）⇒ 拒绝执行 ✗" >&2
  exit 92
fi

BASE_URL="$BASE_URL" APP_ROOT="$APP_ROOT" bash "$TMP/install.sh" "$@"
