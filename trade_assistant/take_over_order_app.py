import time
import asyncio

# take_over_order_app.py
# -*- coding: utf-8 -*-

import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from src.user_logger import setup_logger, setup_uvicorn_file_loggers, set_request_id, get_request_id

# 先创建 logger，再导入其他模块
setup_uvicorn_file_loggers()
logger = setup_logger("backend", bind_default=True)

# NaN-safe JSON encoder（处理 NaN/Inf 浮点值进 JSON 的问题）
from src.json_safe import patch_json_response

patch_json_response()

from src.endpoints import (
    base_info,
    check_status,
    health,
    restart_backend,
    restart_workers,
    restart_service,
    upgrade_program,
    check_upgrade,
    chart_data,
    history_data,
    bulk_history_data,
    kline_coverage,
    feishu_qrcode,
    feishu_status,
    tg_chat_id,
    set_api_info,
    set_quick_open_route,
    set_notice_info,
    login_bot,
    set_follow_info,
    place_order,
    risk_preview,
    pnl_preview,
    management_risk_preview,
    file_action,
    monitor_program,
    close_order,
    management_tool,
    order_reverse,
    order_add,
    grid_mode,
    condition_set,
    condition_cancel,
    condition_list,
    get_file_info,
    line_data,
    periods_report,
    update_indicators,
    update_watchlist,
    all_tickers,
    signal_monitor_program,
    rise_fall_list,
    prev_days_rank,
    edit_sys_set,
    signal_reports,
    get_balances,
    usdt_transfer,
    transfer_quota_info,
    panel_display,
    panel_order_report,
    panel_position_history,
    panel_position_history_live,
    panel_balances,
    audit_logs,
    audit_logs_export,
    error_logs,
    monitor_events,
    plan_events,
    send_test_message,
    refresh_key,
    signal_open,
)
from src.auth import access_token_endpoint, refresh_token_endpoint, logout_all_endpoint
from src.read_edit import FileOperations
from src.send_message import DingPushMessage

try:
    from src.websocket_manager import websocket_market, kline_ws_manager
except Exception as _ws_import_err:
    import logging

    logging.getLogger("takeover").error(
        "websocket_manager 导入失败，WS 功能不可用: %s", _ws_import_err
    )
    websocket_market = None
    kline_ws_manager = None
from src.private_ws_manager import get_private_ws_manager
from src.watchlist_ticker_manager import get_watchlist_ticker_manager


AUTH_ROUTES = [
    ("/access_token", access_token_endpoint),
    ("/refresh_token", refresh_token_endpoint),
    ("/logout_all", logout_all_endpoint),
]

BASE_ROUTES = [
    ("/base_info", base_info),
    ("/check_status", check_status),
    ("/health", health),
    ("/refresh_key", refresh_key),
    ("/restart_backend", restart_backend),
    ("/restart_workers", restart_workers),
    ("/restart_service", restart_service),
    ("/upgrade_program", upgrade_program),
    ("/check_upgrade", check_upgrade),
    ("/chart_data", chart_data),
    ("/history_data", history_data),
    ("/bulk_history_data", bulk_history_data),
    ("/kline_coverage", kline_coverage),
    ("/line_data", line_data),
    ("/periods_report", periods_report),
    ("/update_indicators", update_indicators),
    ("/update_watchlist", update_watchlist),
    ("/all_tickers", all_tickers),
    ("/rise_fall_list", rise_fall_list),
    ("/prev_days_rank", prev_days_rank),
    ("/signal_reports", signal_reports),
    ("/get_balances", get_balances),
    ("/usdt_transfer", usdt_transfer),
    ("/transfer_quota", transfer_quota_info),
    ("/panel_display", panel_display),
    ("/panel_order_report", panel_order_report),
    ("/panel_position_history", panel_position_history),
    ("/panel_position_history_live", panel_position_history_live),
    ("/panel_balances", panel_balances),
    ("/audit_logs", audit_logs),
    ("/audit_logs_export", audit_logs_export),
    ("/error_logs", error_logs),
    ("/monitor_events", monitor_events),
    # ★ 2026-10-16：**触发中心**（5 类计划的删除事件流 ✓，取代旧 monitor_events 的"触发记录"）
    ("/plan_events", plan_events),
]


CONFIG_ROUTES = [
    ("/feishu_qrcode", feishu_qrcode),
    ("/feishu_status", feishu_status),
    ("/tg_chat_id", tg_chat_id),
    ("/set_api_info", set_api_info),
    ("/set_quick_open_route", set_quick_open_route),
    ("/set_notice_info", set_notice_info),
    ("/set_follow_info", set_follow_info),
    ("/edit_sys_set", edit_sys_set),
    ("/get_file_info", get_file_info),
    ("/file_action", file_action),
]

ORDER_ROUTES = [
    ("/place_order", place_order),
    ("/risk_preview", risk_preview),
    ("/pnl_preview", pnl_preview),
    ("/management_risk_preview", management_risk_preview),
    ("/close_order", close_order),
    ("/management_tool", management_tool),
    ("/order_reverse", order_reverse),
    ("/order_add", order_add),
    ("/grid_mode", grid_mode),
    ("/condition_set", condition_set),
    ("/condition_cancel", condition_cancel),
    ("/condition_list", condition_list),
]

BOT_ROUTES = [
    ("/login_bot", login_bot),
    ("/monitor_program", monitor_program),
    ("/signal_monitor_program", signal_monitor_program),
    ("/send_test_message", send_test_message),
    ("/signal_open", signal_open),
]


# 启动任务
async def startup_task():
    logger.info("启动任务开始")
    # ── 写入启动时间到 version.json ──
    # start_time 与 version / update_date 同口径：UTC 秒级时间戳（展示时由前端格式化）
    try:
        _version_file = Path(__file__).resolve().parent / "version.json"
        if _version_file.is_file():
            import json as _json
            _v = _json.loads(_version_file.read_text(encoding="utf-8"))
            _v["start_time"] = int(time.time())
            _version_file.write_text(_json.dumps(_v, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.info("start_time 已写入 version.json: %s", _v["start_time"])
    except Exception as _e:
        logger.warning("start_time 写入失败: %s", _e)
    # 启动多账户私有 WS 管理器（持仓/订单实时推送）
    try:
        private_ws = get_private_ws_manager(kline_ws_manager)
        asyncio.create_task(private_ws.start())
        logger.info("PrivateWsManager 已启动")
    except Exception as e:
        logger.warning("PrivateWsManager 启动失败: %s", e)
    # 启动自选列表实时价 WS 推送器 —— **每个交易所各一个实例、各一条连接**
    #
    # ⚠️ 绝不跨所（2026-10-03 用户裁定）：
    #    「okx 的 ws 不应该出现在 bitget 上，bitget 的也不应该出现在 okx 上」
    #    ⇒ 用 active_exchanges() 逐个起：每个实例只订阅该所的自选标的，
    #      推送 payload 带 `exchange`，前端按 `exchg|ticker` 精确落位。
    #    只起「已登记订阅规格」的所（见 _WATCH_WS_SPECS）—— 没登记的**不猜频道名**。
    try:
        from src.read_edit import active_exchanges as _active_ex
        from src.watchlist_ticker_manager import _WATCH_WS_SPECS as _ws_specs
        _started = []
        for _ex in _active_ex():
            if _ex not in _ws_specs:
                logger.warning("自选实时价：%s 未登记订阅规格，跳过（不猜频道名）", _ex)
                continue
            _wlt = get_watchlist_ticker_manager(kline_ws_manager, exchange=_ex)
            asyncio.create_task(_wlt.start())
            _started.append(_ex)
        logger.info("WatchlistTickerManager 已启动: %s", _started)
    except Exception as e:
        logger.warning("WatchlistTickerManager 启动失败: %s", e)
    # ── 应急刷新密钥（一次性 + 1 小时）：**只在被保活/开机拉起时**生成并推送 ──────
    # 用户 2026-10-28 定稿：
    #   · 用途只有一个：忘了登录密钥 ⇒ 云平台强制重启 ⇒ 保活拉起 ⇒ 钉钉收到新钥匙
    #   · 计划内 restart / upgrade / install **一律不生成、不推送**
    #     （由启动脚本传 XIAOER_START_REASON；默认 keepalive = cron 保活 / @reboot 开机拉起）
    #   · 钥匙本身**一次性 + 1 小时有效**：校验成功即作废（src/endpoints.py::refresh_key）
    #   · 「更新完成」推送已按「所有更新的都不发送」删掉；升级状态仍写 upgrade_status ✓
    _start_reason = str(
        os.environ.get("XIAOER_START_REASON", "keepalive") or "keepalive"
    ).strip().lower()

    try:
        if _start_reason != "keepalive":
            logger.info(
                "启动来源=%s ⇒ 跳过应急刷新密钥的生成与推送（仅保活/开机拉起才发）",
                _start_reason,
            )
        else:
            sys_sets = FileOperations().read_file_info("sys_set")
            if sys_sets:
                sys_set = sys_sets[0]
                access_key = sys_set.get("auth_key", "")
                if access_key:
                    import secrets

                    refresh_key = secrets.token_urlsafe(16)
                    FileOperations().edit_file_info(
                        "sys_set",
                        {"auth_key": access_key},
                        "edit",
                        {
                            "refresh_key": refresh_key,
                            "refresh_key_expires_at": str(int(time.time() + 3600)),
                        },
                    )
                    await asyncio.sleep(2)
                    DingPushMessage().ding_push_message(
                        "【小二管家】\n"
                        + "─" * 16
                        + "\n"
                        + f"启动成功\n刷新密钥(一次性，1小时有效): {refresh_key}\n"
                        + "─" * 16
                        + "\n"
                        + "登录页点击「忘记访问秘钥」输入此密钥即可获取",
                        send_type="normal",
                    )
                    logger.info("启动通知已推送（来源=keepalive）")
                else:
                    logger.info("auth_key 未配置，跳过刷新密钥生成")
    except Exception as e:
        logger.warning("启动通知推送失败: %s", e)

    # ── 升级完成标记：只维护状态，**不再推送** ─────────────────────────────────
    # 用户 2026-10-28：「所有更新的都不需要发送」⇒ 升级通知整条去掉；
    # 但 `upgrade_status` 仍要标记成功（界面「升级状态」靠它 ✓）。
    try:
        _upgrade_marker = Path(__file__).resolve().parent / "run" / ".upgrade_notify"
        if _upgrade_marker.is_file():
            _upgrade_marker.unlink()
            try:
                from src import upgrade_status
                upgrade_status.mark_success()
            except Exception as _e:
                logger.warning("upgrade_status.mark_success 失败: %s", _e)
            logger.info("升级完成标记已处理（按裁定：不推送更新通知）")
    except Exception as _e:
        logger.warning("升级完成标记处理失败: %s", _e)




# 创建 lifespan 事件处理器
@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup_task()
    yield
    # 停止私有 WS 管理器
    try:
        private_ws = get_private_ws_manager()
        await private_ws.stop()
        logger.info("PrivateWsManager 已停止")
    except Exception as e:
        logger.warning("PrivateWsManager 停止异常: %s", e)
    # 停止自选列表实时价 WS 推送器（每个交易所的实例都要停）
    try:
        from src.watchlist_ticker_manager import _managers as _wlt_managers
        for _ex, _wlt in list(_wlt_managers.items()):
            try:
                await _wlt.stop()
            except Exception as e:
                logger.warning("WatchlistTickerManager[%s] 停止异常: %s", _ex, e)
        logger.info("WatchlistTickerManager 已停止: %s", list(_wlt_managers))
    except Exception as e:
        logger.warning("WatchlistTickerManager 停止异常: %s", e)


# 创建 FastAPI 应用
# 安全最佳实践: 生产环境默认关闭 OpenAPI 文档，开发时设置 ENABLE_DOCS=1 开启
_docs_enabled = os.getenv("ENABLE_DOCS", "") == "1"
app = FastAPI(
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

# 挂载静态文件目录，指向 dist 文件夹

# 配置 CORS
# 默认允许所有来源（桌面壳等非浏览器 origin 也需要访问）。
# 如需限制，通过环境变量 CORS_ORIGINS 配置允许的来源（逗号分隔）。
_cors_origins = os.getenv("CORS_ORIGINS", "").strip()
if _cors_origins:
    origins = [o.strip() for o in _cors_origins.split(",") if o.strip()]
else:
    origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 添加 GZip 压缩中间件
app.add_middleware(GZipMiddleware, minimum_size=100)

# ── 请求链追踪中间件 ──
# 每个 HTTP 请求自动分配 request_id，记录耗时
# 使用纯 ASGI 中间件，避免 BaseHTTPMiddleware 的 Semaphore.acquire 警告

import time as _time
from starlette.types import ASGIApp, Scope, Receive, Send

class RequestIdMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app
    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        set_request_id()
        req_id = get_request_id()
        _start = _time.perf_counter()
        logger.info("[REQ] %s %s | req_id=%s", scope["method"], scope["path"], req_id)
        await self.app(scope, receive, send)  # 不能用 call_next，纯 ASGI 不走 Starlette 包装
        elapsed_ms = (_time.perf_counter() - _start) * 1000
        # 响应状态码在 call_next 之后无法直接从纯 ASGI 获取，记录路径+耗时即可
        logger.info("[REQ] %s %s done | %.1fms", scope["method"], scope["path"], elapsed_ms)

app.add_middleware(RequestIdMiddleware)

# ── 全局并发请求限制中间件 ──
# 防止多个终端/多标签页突发流量压垮单 worker
import asyncio as _asyncio
_MAX_CONCURRENT_REQUESTS = 20
_request_semaphore = _asyncio.Semaphore(_MAX_CONCURRENT_REQUESTS)

_UNLIMITED_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}

class ConcurrencyLimiterMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app
    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http" or scope["path"] in _UNLIMITED_PATHS:
            await self.app(scope, receive, send)
            return
        try:
            await _asyncio.wait_for(_request_semaphore.acquire(), timeout=5.0)
        except _asyncio.TimeoutError:
            from fastapi.responses import JSONResponse
            resp = JSONResponse(status_code=429, content={"status": "error", "msg": "服务器繁忙，请稍后重试"})
            await resp(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            _request_semaphore.release()

app.add_middleware(ConcurrencyLimiterMiddleware)

def register_post_routes(route_group):
    for path, handler in route_group:
        app.post(path)(handler)


# 注册路由
for route_group in (
    AUTH_ROUTES,
    BASE_ROUTES,
    CONFIG_ROUTES,
    ORDER_ROUTES,
    BOT_ROUTES,
):
    register_post_routes(route_group)

# 注册公开 GET 路由（无需鉴权）
# download_kline 已停用：K 线不再从其他服务器拉取（endpoints.download_kline 直接返回停用）

# 注册 WebSocket 路由
app.websocket("/ws/market")(websocket_market)

# ── 路由清单（供 sync_nginx_api_routes.py 提取 ──
# 所有 POST 路由按分组汇总 + 特殊路由
ALL_POST_ROUTES = []
for _group in (AUTH_ROUTES, BASE_ROUTES, CONFIG_ROUTES, ORDER_ROUTES, BOT_ROUTES):
    for _path, _ in _group:
        ALL_POST_ROUTES.append(_path)

REGISTERED_ROUTES = {
    "post": ALL_POST_ROUTES,
    "get": [],
    "websocket": ["/ws/market"],
}

try:
    dist_dir = "dist_temp"
    enable_backend_static = os.getenv("ENABLE_BACKEND_STATIC", "").strip() == "1"
    if enable_backend_static:
        if os.path.isdir(dist_dir) and os.listdir(dist_dir):
            app.mount("", StaticFiles(directory=dist_dir, html=True), name="static")
            logger.info("已挂载静态资源目录: %s", dist_dir)
        else:
            logger.warning("静态资源目录不存在或为空，跳过挂载: %s", dist_dir)
    else:
        logger.info("静态资源由 nginx 提供，后端跳过挂载: %s", dist_dir)
except Exception as e:
    logger.warning("静态资源挂载失败(不影响启动): %s", e)
