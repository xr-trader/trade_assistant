# -*- coding: utf-8 -*-
"""OKX 传输层常量。

2026-10-02 精简
===============

**从 302 行 / 255 个端点常量 精简到只保留传输层需要的内容。**

为什么可以大幅精简
------------------
本项目的全部 REST 动作已统一走 `custom_actions`，端点路径**写在
[`exchange/okx_actions.py`](../okx_actions.py) 里（路径字面量）**，
不再引用本文件的端点常量。

因此本文件只剩两类内容 —— 都是 `client.py` / `utils.py` 发请求所必需的：

1. **HTTP 头名与鉴权头名**（`OK_ACCESS_*` 等）
2. **HTTP 方法名与 API 基址**（`GET` / `POST` / `API_URL` / `SERVER_TIMESTAMP_URL`）

> 原先那 255 个端点常量里，本项目实际只用到 29 个，且这 29 个现在已经变成
> `okx_actions.py` 里的路径字面量。保留其余 226 个的代价是：官方 SDK 新版
> **重命名了其中 101 个**（如 `CANAEL_ORDER` → `CANCEL_ORDER`），
> 升级时会产生大量无谓的 diff。

需要查某个端点路径时
--------------------
- [`docs/action-map.md`](../../docs/action-map.md) —— 27 个在用动作的两所端点对照
- [`vendor/okx-python-sdk/okx/consts.py`](../../vendor/okx-python-sdk/okx/consts.py) —— 官方最新版全量常量（**参考期**，不进构建）

完整备份：`tools/baseline/backups/consts.py.bak_full_before_slim`
"""

# ── API 基址 ──
API_URL = "https://www.okx.com"
SERVER_TIMESTAMP_URL = "/api/v5/public/time"

# ── HTTP 头 ──
CONTENT_TYPE = "Content-Type"
APPLICATION_JSON = "application/json"

# ── 鉴权头 ──
OK_ACCESS_KEY = "OK-ACCESS-KEY"
OK_ACCESS_SIGN = "OK-ACCESS-SIGN"
OK_ACCESS_TIMESTAMP = "OK-ACCESS-TIMESTAMP"
OK_ACCESS_PASSPHRASE = "OK-ACCESS-PASSPHRASE"

# ── HTTP 方法 ──
GET = "GET"
POST = "POST"
