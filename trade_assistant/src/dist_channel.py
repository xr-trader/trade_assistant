# -*- coding: utf-8 -*-
# cython: language_level=3

# src/dist_channel.py
"""
============================================================================
分发通道 —— 安装 / 升级 / 远程版本 **唯一**的地址来源
============================================================================

用户裁定（2026-11-04，原话）：
  「我希望 安装与更新，全部从 github 拉取」「用户默认是走 github 的且没有兜底，走不通就是走不通」
  「新增一个用户权限，测试用户，有测试用户的，走 231 的更新且没有兜底，走不通就走不通」
  「全新部署，统一走 github」「231 只根据权限来做升级源」

⇒ 定稿口径（**两条通道，都没有任何兜底** ✗ —— 拉不到就报错，绝不悄悄换一条线）：

  ① 正式（默认，普通用户）  https://github.com/<owner>/<repo>/releases/latest/download
       · `latest` 由 GitHub 保证**永不指向 prerelease** ⇒ 测试版天然不会流到用户端 ✓
       · 全新安装（install.sh）也走这条 ✓（安装时还没有权限位，只有一个源 ✓）

  ② 测试（`user_permissions` 含 `beta_channel`）  本机 `sys_set.manager_host` 的 `/trade/`
       · 即「231 内部迭代线」—— 231 上 manager_host 就是 231 ⇒ 拉到 231 的版本 ✓
       · **不把 231 的 IP 写进代码** ✗：包的 `.so` 会被公开分发，写死 = 公开生产 IP ✗
         且 144 测试机照旧拉 144 自己的 `/trade/`（与 2026-10-13 以来行为**逐字一致** ✓）
       · manager_host 为空 ⇒ 返回空串 ⇒ **调用方必须报错**（不兜底 ✓）

★ `sys_set.manager_host` 从此**只用于「测试通道的基址」+ 授权/心跳**，
  正式通道**完全不读它** ✗（用户端不需要也不应该知道授权服务器地址）。

★ 换仓库：只改下面 `GITHUB_OWNER` / `GITHUB_REPO` 两个常量（地址只在这一处 ✓）。
============================================================================
"""

from __future__ import annotations

# ── 写死处（唯一）───────────────────────────────────────────────────────────
GITHUB_OWNER = "xr-trader"
GITHUB_REPO = "trade_assistant"
RELEASE_BASE = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest/download"

#: 测试通道的权限位（授权端账号表 `user_permissions` 里的一个字符串 ✓
#: 与 `allow_grid` / `allow_manage` 同款命名口径 ✓；显示名「测试用户」由前端负责）
BETA_PERMISSION = "beta_channel"

CHANNEL_RELEASE = "release"
CHANNEL_TEST = "test"
CHANNELS = (CHANNEL_RELEASE, CHANNEL_TEST)

#: 分发线上的资产名（三处 URL 共用，改名前先 grep ✓）
PACKAGE_NAME = "trade_assistant.tar.gz"   # 后端程序包（内含 dist_temp/ 客户端热更新）
UPGRADE_SCRIPT_NAME = "upgrade.sh"        # 权威升级脚本（deploy/dist/upgrade.sh）
INSTALL_SCRIPT_NAME = "install.sh"        # 首次安装
VERSION_NAME = "version.json"             # 后端版本（「检查更新」比它）

#: 测试通道（`/trade/`）的端口与路径 —— 与 2026-10-13 以来**逐字相同** ✓
SERVER_PORT = 80
SERVER_PATH = "/trade"


# ── 权限归一（与 `endpoints._get_current_permissions` / 授权端 `_normalize_permissions`
#    同口径：list / 逗号串 / 带引号串都认 ✓ —— 本模块**零依赖**，包内 shell 也能 import ✓）
def parse_permissions(value) -> list[str]:
    """把 `user_permissions` 归一成字符串列表（空/None ⇒ []）。"""
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value).split(",") if x.strip()]


def is_beta(value) -> bool:
    """是否「测试用户」（`user_permissions` 含 `beta_channel`）。"""
    return BETA_PERMISSION in parse_permissions(value)


def channel_for(value) -> str:
    """通道名：`test` / `release`（**权限决定**，不看任何其它配置 ✓）。"""
    return CHANNEL_TEST if is_beta(value) else CHANNEL_RELEASE


# ── 基址解析 ────────────────────────────────────────────────────────────────
def server_base(manager_host) -> str:
    """测试通道基址：`http://<manager_host 去端口>:80/trade`。

    ⚠️ 与改造前 `endpoints._build_upgrade_package_url` 的拼法**逐字相同** ✓
       （manager_host 可能是带端口的 API 网关，如 `:8080` ⇒ 恒定走 80，避免 /trade 404）
    空 ⇒ 返回 ""（调用方必须报错，**不兜底** ✗）。
    """
    host = str(manager_host or "").strip()
    if not host:
        return ""
    return f"http://{host.split(':')[0]}:{SERVER_PORT}{SERVER_PATH}"


def base_for(permissions, manager_host="") -> str:
    """分发基址（三条 URL 共用）。

    参数：
        permissions  — `sys_set.user_permissions` 原值（list / 逗号串 / None 都行 ✓）
        manager_host — `sys_set.manager_host` 原值（**只有测试通道会用到** ✓）

    返回：基址（不带尾斜杠）；测试通道且缺 manager_host ⇒ ""（调用方报错 ✓）
    """
    if channel_for(permissions) == CHANNEL_TEST:
        return server_base(manager_host)
    return RELEASE_BASE


def url_for(asset, permissions, manager_host="") -> str:
    """资产 URL；基址解析不出来 ⇒ ""（调用方报错，**绝不回落到另一条通道** ✗）。"""
    base = base_for(permissions, manager_host)
    if not base:
        return ""
    return f"{base.rstrip('/')}/{asset}"
