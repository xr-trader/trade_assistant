import hmac
import base64
import time
import datetime
from . import consts as c


def sign(message, secretKey):
    mac = hmac.new(bytes(secretKey, encoding='utf8'), bytes(message, encoding='utf-8'), digestmod='sha256')
    d = mac.digest()
    return base64.b64encode(d)


def pre_hash(timestamp, method, request_path, body):
    return str(timestamp) + str.upper(method) + request_path + body


def get_header(api_key, sign, timestamp, passphrase, flag):
    header = dict()
    header[c.CONTENT_TYPE] = c.APPLICATION_JSON
    header[c.OK_ACCESS_KEY] = api_key
    header[c.OK_ACCESS_SIGN] = sign
    header[c.OK_ACCESS_TIMESTAMP] = str(timestamp)
    header[c.OK_ACCESS_PASSPHRASE] = passphrase
    header['x-simulated-trading'] = flag
    return header


def parse_params_to_str(params):
    """GET 参数 → query string（**URL 与签名串共用**这一份）。

    ⚠️ **跳过 ``None`` 与空串**（对齐官方新版 0.4.x 的同名修复；我们这份旧 fork 漏了）。

    改前会把"调用方用 ``None``/``""`` 表示不传"的参数拼成**字面量**发给交易所：

    * ``src/panel_cache_builder.py`` 的 ``after=last_bill_id or None``、
      ``"before": last_ts or None`` ⇒ URL 变成 ``...&before=None``
    * **真机复现（2026-10-04，活跃-叶）**：OKX 回
      ``HTTP 400 / code=51000 / "Parameter before error"``
      ⇒ 该函数第一页就 ``break`` ⇒ **面板的「划转流水」「平仓历史」一条都拿不到**
    * ``src/okx_bills.py`` 还会把默认空串 ``before=""`` 一并透传（``before=`` 同样非法）

    Bitget 侧早已这样过滤（``bitget_client._clean_params``），本次把两所口径对齐。

    ⚠️ 清洗发生在**拼 request_path 之时**，而签名用的就是同一个 ``request_path``
    （见 ``client._request``）⇒ 签名与 URL 天然一致，不会因为少传一个参数而验签失败。
    """
    parts = []
    for key, value in (params.items() if isinstance(params, dict) else []):
        if value is None or value == "":
            continue
        parts.append("%s=%s" % (key, value))
    return "?" + "&".join(parts) if parts else ""


def get_timestamp():
    now = datetime.datetime.utcnow()
    t = now.isoformat("T", "milliseconds")
    return t + "Z"


def signature(timestamp, method, request_path, body, secret_key):
    if str(body) == '{}' or str(body) == 'None':
        body = ''
    message = str(timestamp) + str.upper(method) + request_path + str(body)

    mac = hmac.new(bytes(secret_key, encoding='utf8'), bytes(message, encoding='utf-8'), digestmod='sha256')
    d = mac.digest()

    return base64.b64encode(d)
