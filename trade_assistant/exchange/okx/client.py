import requests
import json
import datetime
from . import consts as c, utils, exceptions
from exchange import http_pool


# OKX 公共请求超时（连接 5s / 读取 10s）
# 背景：原 requests.get/post 未设 timeout（默认无限阻塞），当 OKX 限流/网络波动时,
# 连接会长时间挂起，叠加 all_tickers 的 single-flight 单飞，会把并发请求一起拖住数十秒。
_REQUESTS_TIMEOUT = (5, 10)

#: 鉴权类 HTTP 状态码：出现即丢掉该账户的连接池（fail-open，下次重建）
_AUTH_STATUS = (401, 403)


class Client(object):

    def __init__(self, api_key, api_secret_key, passphrase, use_server_time=False, flag='1'):

        self.API_KEY = api_key
        self.API_SECRET_KEY = api_secret_key
        self.PASSPHRASE = passphrase
        self.use_server_time = use_server_time
        self.flag = flag

    def _request(self, method, request_path, params):

        if method == c.GET:
            request_path = request_path + utils.parse_params_to_str(params)
        # url
        url = c.API_URL + request_path

        timestamp = utils.get_timestamp()

        # sign & header
        if self.use_server_time:
            timestamp = self._get_timestamp()

        body = json.dumps(params) if method == c.POST else ""

        sign = utils.sign(utils.pre_hash(timestamp, method, request_path, str(body)), self.API_SECRET_KEY)
        header = utils.get_header(self.API_KEY, sign, timestamp, self.PASSPHRASE, self.flag)

        # send request
        # ★ D2：走**按 api_key 隔离**的连接池（改造前是模块级 requests.get/post，
        #   每个请求都重新 TCP+TLS 握手 —— 真机实测 OKX REST 1012 次/分）。
        #   认证头**只挂这一次请求**，绝不写进 Session（防跨账户串号）；
        #   POST 在 http_pool 的重试白名单外 ⇒ 下单仍是一次即止。
        response = None
        try:
            if method == c.GET:
                response = http_pool.request(
                    "GET", url, api_key=self.API_KEY, headers=header,
                    timeout=_REQUESTS_TIMEOUT,
                )
            elif method == c.POST:
                response = http_pool.request(
                    "POST", url, api_key=self.API_KEY, headers=header,
                    data=body, timeout=_REQUESTS_TIMEOUT,
                )
        except requests.exceptions.RequestException:
            # 连接被断/超时：池里的连接可能已坏 ⇒ 丢掉，下次重建（fail-open）
            http_pool.drop_session(self.API_KEY)
            raise

        # exception handle
        if not str(response.status_code).startswith('2'):
            if response.status_code in _AUTH_STATUS:
                http_pool.drop_session(self.API_KEY)
            raise exceptions.OkxAPIException(response)

        return response.json()

    def _request_without_params(self, method, request_path):
        return self._request(method, request_path, {})

    def _request_with_params(self, method, request_path, params):
        return self._request(method, request_path, params)

    def _get_timestamp(self):
        """取交易所服务器时间，返回 **ISO8601 毫秒 + Z**（与 `utils.get_timestamp()` 同格式）。

        ⚠️ 两处纠正（对齐官方新版 0.4.4）——**本项目正常路径用不到它**（休眠缺陷）：

        ① 改前直接把响应里的**毫秒时间戳字符串**塞进 ``OK-ACCESS-TIMESTAMP`` 头 ——
           该头必须是 ISO8601，一旦 ``use_server_time=True`` 就必然验签失败（401）。
        ② 官方新版已**弃用** ``use_server_time``（构造参数收到即 DeprecationWarning、
           内部恒为 False）：它会给**每个**业务请求额外加一次 ``/public/time`` 调用
           （请求数翻倍），而本地时钟偏移本来就有 ±30s 容忍度 ⇒ 得不偿失。
           本方法保留只为兼容；**不要**打开该开关。
        """
        url = c.API_URL + c.SERVER_TIMESTAMP_URL
        response = http_pool.request(
            "GET", url, api_key=self.API_KEY, timeout=_REQUESTS_TIMEOUT
        )
        if response.status_code != 200:
            return ""
        try:
            ts = int(response.json()["data"][0]["ts"])
        except Exception:
            return ""
        moment = datetime.datetime.fromtimestamp(ts / 1000.0, tz=datetime.timezone.utc)
        return moment.isoformat("T", "milliseconds").replace("+00:00", "Z")

