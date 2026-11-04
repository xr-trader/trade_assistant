# -*- coding: utf-8 -*-
"""Bitget 客户端（经典账户 v2）—— 常量 + 签名 + HTTP，**单文件扁平化**。

⚠️ 为什么扁平而非 ``exchange/bitget/{consts,utils,client}.py``
==============================================================

项目护栏 ``check_unique_names`` 禁止同名 ``.py``，而 ``exchange/okx/`` 下已有
``consts.py`` / ``utils.py`` / ``exceptions.py`` / ``client.py``。同名会让
"看到 import client 时不知道是哪个所"成为真实风险，故合并为一文件。

⚠️ 本模块只服务**经典账户**。统一账户（UTA）走 ``/api/v3/...``；
本项目账户实测为经典模式（v3 私有接口一律 ``40084``）。

签名规范（真机实测）
====================

::

    签名串 = ACCESS-TIMESTAMP + METHOD + requestPath + "?" + queryString + body
              ↑ 毫秒字符串      ↑ 大写                                  ↑ 无 query 时为空
    ACCESS-SIGN = base64(HMAC-SHA256(签名串, secretKey))

⚠️ **两个坑（都踩过）**：

1. ``?`` 必须随 queryString 一起进签名串 —— 否则**无 query 的请求能过、
   带 query 的全部 ``40009 sign signature error``**，极易误判成算法错。
2. 签名串用**原始值**、URL 用**URL 编码值**。参数含逗号时（如
   ``orderType=conditional,oco``），用编码值签名同样报 ``40009``。
   无特殊字符时两者相同 ⇒ 只在含逗号/空格/中文时暴露，极易漏测。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict
from urllib.parse import urlencode

import requests

from exchange import http_pool



# ══════════════════════════════════════════════════════════════════
#  常量
# ══════════════════════════════════════════════════════════════════

#: REST 基址（实测：``GET /api/v2/public/time`` → ``{"code":"00000"}``）
API_URL = "https://api.bitget.com"

#: 服务器时间端点（签名的 ACCESS-TIMESTAMP 需与之相差 < 30s）
SERVER_TIMESTAMP_URL = "/api/v2/public/time"

GET = "GET"
POST = "POST"

#: 请求头名（与 OKX 完全不同：Bitget 用 ``ACCESS-*``）
CONTENT_TYPE = "Content-Type"
APPLICATION_JSON = "application/json"
BG_ACCESS_KEY = "ACCESS-KEY"
BG_ACCESS_SIGN = "ACCESS-SIGN"
BG_ACCESS_TIMESTAMP = "ACCESS-TIMESTAMP"
BG_ACCESS_PASSPHRASE = "ACCESS-PASSPHRASE"

#: 成功码 —— ⚠️ 不是 OKX 的 ``"0"``，是 **5 个 0** 的字符串
SUCCESS_CODE = "00000"

#: ★ 模拟盘开关（真机实测：**只有 "1" 有效**）
#:
#: Bitget 模拟盘**不换域名**（``api-sim.bitget.com`` DNS 都解析不了），
#: 而是在**同一个** ``api.bitget.com`` 上加这个头：
#:
#: ============================  ========================================
#: 头取值                        实测结果
#: ============================  ========================================
#: 不传 / ``"0"`` / ``"true"`` /  头被忽略，**走实盘**（返回实盘账户权益）
#: ``"false"`` / ``"demo"`` …
#: ``"1"``                      **切模拟盘**；若 key 是实盘的 ⇒
#:                             ``40099 exchange environment is incorrect``
#: ============================  ========================================
#:
#: ⇒ **模拟盘必须在 Bitget「模拟交易」里单独申请 key**，不能复用实盘 key。
PAPTRADING_HEADER = "PAPTRADING"
PAPTRADING_ON = "1"

#: 产品类型（本项目只做 USDT 本位永续）

# ══════════════════════════════════════════════════════════════════
#  异常
# ══════════════════════════════════════════════════════════════════

class BitgetAPIException(Exception):
    """HTTP 非 2xx 时抛出。附带状态码与响应体，便于上层判定是否可重试。"""

    def __init__(self, response):
        self.status_code = getattr(response, "status_code", None)
        self.response = response
        try:
            self.body = response.json()
        except Exception:
            self.body = getattr(response, "text", "")
        super().__init__(f"BitgetAPIException(status={self.status_code}, body={self.body})")

# ══════════════════════════════════════════════════════════════════
#  签名
# ══════════════════════════════════════════════════════════════════




def get_timestamp() -> str:
    """毫秒时间戳字符串（**不是** OKX 的 ISO8601 格式）。"""
    return str(int(time.time() * 1000))


def pre_hash(timestamp: str, method: str, request_path: str, query_string: str, body: str) -> str:
    """拼签名串。

    query_string **不含** 前导 ``?``；本函数负责补上。
    """
    qs = ("?" + query_string) if query_string else ""
    return f"{timestamp}{method.upper()}{request_path}{qs}{body}"


def sign(message: str, secret_key: str) -> str:
    """HMAC-SHA256 + base64（返回值是 **str**，便于直接塞进 header）。"""
    mac = hmac.new(
        secret_key.encode("utf-8"), message.encode("utf-8"), digestmod=hashlib.sha256
    )
    return base64.b64encode(mac.digest()).decode("utf-8")


def _clean_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """跳过 ``None``（调用方用 ``None`` 表示"不传"），布尔转小写。"""
    clean: Dict[str, Any] = {}
    for k, v in (params or {}).items():
        if v is None:
            continue
        clean[k] = ("true" if v else "false") if isinstance(v, bool) else v
    return clean


def parse_params_to_str(params: Dict[str, Any]) -> str:
    """GET 参数 → **URL 用**的 query string（已 URL 编码）。"""
    if not params:
        return ""
    return urlencode(_clean_params(params))


def parse_params_to_sign(params: Dict[str, Any]) -> str:
    """GET 参数 → **签名用**的 query string（**原始值，不做 URL 编码**）。

    ★ 真机实测（2026-10-03）
    ------------------------

    签名串必须用**未编码**的值，URL 才用编码后的值。用编码值签名时：

    ::

        orderType=conditional,oco
          → 签名用 "conditional%2Coco"  ⇒ 40009 sign signature error  ❌
          → 签名用 "conditional,oco"    ⇒ 通过（错误码变成参数问题）   ✅

    ``planType=profit_loss,normal_plan`` 同样复现。

    ⚠️ 参数**无特殊字符时两种写法结果相同**，所以这个坑只在含
    逗号/空格/中文等字符时才暴露 —— 极易漏测。
    """
    if not params:
        return ""
    clean = _clean_params(params)
    return "&".join("%s=%s" % (k, v) for k, v in clean.items())


def get_header(api_key: str, signature: str, timestamp: str, passphrase: str,
               demo: bool = False) -> Dict[str, str]:
    """构造鉴权请求头。

    ``demo=True`` 时附加 ``PAPTRADING: 1`` 切到模拟盘环境
    （``api_list.flag == "1"`` 即模拟盘，与 OKX 的 ``flag`` 语义一致）。
    """
    h = {
        CONTENT_TYPE: APPLICATION_JSON,
        BG_ACCESS_KEY: api_key,
        BG_ACCESS_SIGN: signature,
        BG_ACCESS_TIMESTAMP: str(timestamp),
        BG_ACCESS_PASSPHRASE: passphrase,
    }
    if demo:
        h[PAPTRADING_HEADER] = PAPTRADING_ON
    return h

# ══════════════════════════════════════════════════════════════════
#  HTTP 客户端
# ══════════════════════════════════════════════════════════════════




#: 超时（连接 5s / 读取 10s）—— 与 OKX 侧一致，避免限流时长期挂起
_REQUESTS_TIMEOUT = (5, 10)

#: 鉴权类 HTTP 状态码：出现即丢掉该账户的连接池（fail-open，下次重建）
_AUTH_STATUS = (401, 403)


class Client(object):
    """Bitget REST 客户端。

    参数与 OKX 版保持同形，便于 ``factory`` 统一构造：
    ``Client(api_key, secret_key, passphrase)``。
    """

    def __init__(self, api_key, api_secret_key, passphrase, use_server_time=False, flag="0"):
        self.API_KEY = api_key
        self.API_SECRET_KEY = api_secret_key
        self.PASSPHRASE = passphrase
        self.use_server_time = use_server_time
        #: Bitget 经典账户**没有** ``x-simulated-trading`` 头；
        #: 保留字段只为与 OKX 构造签名兼容（模仿盘见 api_list.flag）。
        #: 模拟盘标识：``"1"`` = 模拟盘（与 OKX 的 ``flag`` 语义对齐）。
        #: 实测：只有 ``"1"`` 会切环境；其他值一律被忽略走实盘。
        self.flag = str(flag) if flag is not None else "0"

    @property
    def is_demo(self) -> bool:
        """是否模拟盘（``flag == "1"``）。"""
        return self.flag == "1"

    def _request(self, method, request_path, params):
        method = method.upper()

        # ★ 两个 query string：签名用【原始值】，URL 用【URL 编码值】
        #   真机实测：把编码值放进签名串时，含逗号的参数会 40009。
        #   详见 parse_params_to_sign 的注释。
        if method == GET:
            qs_sign = parse_params_to_sign(params or {})
            qs_url = parse_params_to_str(params or {})
            body = ""
        else:
            qs_sign = qs_url = ""
            body = json.dumps(params) if params else ""

        url = API_URL + request_path + (("?" + qs_url) if qs_url else "")

        timestamp = get_timestamp()
        if self.use_server_time:
            timestamp = self._get_timestamp() or timestamp

        message = pre_hash(timestamp, method, request_path, qs_sign, body)
        signature = sign(message, self.API_SECRET_KEY)
        header = get_header(self.API_KEY, signature, timestamp,
                            self.PASSPHRASE, demo=self.is_demo)

        # ★ D2：走**按 api_key 隔离**的连接池（改造前是模块级 requests.get/post，
        #   每个请求重新 TCP+TLS 握手）。认证头只挂这一次请求，绝不写进 Session；
        #   POST 不在重试白名单里 ⇒ 下单/撤单仍是一次即止。
        try:
            if method == GET:
                response = http_pool.request(
                    "GET", url, api_key=self.API_KEY, headers=header,
                    timeout=_REQUESTS_TIMEOUT,
                )
            elif method == POST:
                response = http_pool.request(
                    "POST", url, api_key=self.API_KEY, headers=header,
                    data=body, timeout=_REQUESTS_TIMEOUT,
                )
            else:
                raise ValueError("Bitget Client 不支持的 HTTP 方法: %r" % method)
        except requests.exceptions.RequestException:
            http_pool.drop_session(self.API_KEY)
            raise

        if not str(response.status_code).startswith("2"):
            if response.status_code in _AUTH_STATUS:
                # 鉴权失败：丢掉该账户连接池（下次重建），fail-open
                http_pool.drop_session(self.API_KEY)
            raise BitgetAPIException(response)

        return response.json()

    def _request_without_params(self, method, request_path):
        return self._request(method, request_path, {})

    def _request_with_params(self, method, request_path, params):
        return self._request(method, request_path, params)

    def _get_timestamp(self):
        """取服务器时间（毫秒字符串）。失败返回空串，由调用方回落本地时间。"""
        try:
            response = http_pool.request(
                "GET", API_URL + SERVER_TIMESTAMP_URL, api_key=self.API_KEY,
                timeout=_REQUESTS_TIMEOUT,
            )
            if response.status_code == 200:
                return str(response.json()["data"]["serverTime"])
        except Exception:
            pass
        return ""