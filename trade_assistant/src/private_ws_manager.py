# -*- coding: utf-8 -*-
"""
PrivateWsManager — 多账户 OKX 私有频道 WebSocket 管理器

============================================================
设计目标
============================================================
1. 为每个 API Key 建立独立的 OKX 私有 WS 连接
2. 订阅 positions / orders / account 频道
3. 收到数据后更新内存缓存，并通过 WebSocketManager 广播到前端
4. 完全独立于公共行情 WS，不影响现有实时 K 线推送

============================================================
架构
============================================================
    api_key_1 ── ws_private ── OKX private WS ── positions / orders / account
    api_key_2 ── ws_private ── OKX private WS ── positions / orders / account
    ...                             ↓
                             合并 + 更新内存缓存
                                   ↓
                          WebSocketManager.broadcast_to_clients()

============================================================
启动时机
============================================================
    # 在 take_over_order_app.py 的 startup 事件中：
    private_ws = PrivateWsManager(ws_manager)
    asyncio.create_task(private_ws.start())

    # 在 shutdown 事件中：
    await private_ws.stop()

============================================================
集成注意
============================================================
- 不修改现有 websocket_manager.py 的核心逻辑
- 通过 ws_manager.send_to_client(ws_id, payload) 推送
- 通过 ws_manager.broadcast(payload) 广播给所有客户端
"""

import asyncio
import json
import time
import hmac
import base64
import copy
from typing import Dict, List, Optional

import websockets

from exchange.endpoint_registry import ws_endpoint
from src.user_logger import get_logger
from src.read_edit import FileOperations
from src.api_data_store import read_api_json

logger = get_logger("private_ws")

# ── OKX 私有 WS 地址 ──
# ⚠️ 地址统一从 exchange/endpoints.py 取（唯一出处）。**按账户的 api_list.exchange
#    实时解析**（见 _add_connection），所以一个进程里可以同时服务多所账户。
#    下面这些常量保留为「okx」的快照供日志/兼容引用。
OKX_PRIVATE_WS_URL = ws_endpoint("okx", "private")
# 模拟盘
OKX_PRIVATE_WS_URL_DEMO = ws_endpoint("okx", "private", demo=True)

# ── OKX 业务 WS 地址（策略委托 orders-algo 频道需要）──
OKX_BUSINESS_WS_URL = ws_endpoint("okx", "business")
# 模拟盘
OKX_BUSINESS_WS_URL_DEMO = ws_endpoint("okx", "business", demo=True)


# ══════════════════════════════════════════════════════════════════════
#  UTA(v3) 账户探测（Bitget）
# ══════════════════════════════════════════════════════════════════════
#: `api_key → 是否 UTA(v3)` 的**进程内缓存**（探测是只读 REST，但没必要每次建连都问一遍）。
_UTA_ACCOUNT_CACHE: Dict[str, bool] = {}


def _strategy_orders_singleton(exchange) -> bool:
    """该账户所属交易所的策略单是否**单例替换**语义（判据在**能力表**）。

    ★ 用户 2026-10-05 报告「前端还是 3 条离场」的根因：
      Bitget `type=tpsl` 是**同一持仓同方向单例**（再挂 = 复用同一 orderId 并覆盖参数，
      见根 `AGENTS.md` §3.0 事故记录）⇒ 被替换掉的旧单**不再推 WS 帧**；
      而 `_on_orders` 对 algo 缓存是**合并**语义 ⇒ 旧行**永远留在缓存里** ✗
      ⇒ 每次 `orders_push` 都把已不存在的旧行推给前端（真机：REST 权威只有 1 笔，
      日志却 `algo=2` / `离场 2个`）✗✗。

    命中 ⇒ 推送前必须用**权威 REST 列表**对账（见 `_reconcile_algo_cache`）。
    ⚠️ 业务层**不得**写交易所硬编码分支（架构铁律 9）—— 一律查能力表；
    取不到（异常/空）⇒ ``False`` ⇒ **不进该分支**（旧行为，OKX 一字不变 ✓）。
    """
    try:
        from exchange.capabilities import strategy_orders_singleton

        return bool(strategy_orders_singleton(exchange))
    except Exception as e:  # noqa: BLE001 —— 能力表取不到不该拖垮 WS 接收循环
        logger.warning("取策略单单例能力失败（按非单例处理，旧行为）: %s", e)
        return False


def _is_uta_account(api_info: dict) -> bool:
    """该 Bitget 账户是否 **UTA(v3)**（判据 = 只读 REST `get_account_config`）。

    ★ 为什么必须探：UTA(v3) 与 Classic(v2) 的**私有 WS 命名空间与订阅参数都不同** ——
      * UTA(v3) ：端点 `/v3/ws/private`，订阅 `{"instType":"UTA","topic":"position"}`（**用 topic**）
      * Classic(v2)：端点 `/v2/ws/private`，订阅 `{"instType":"USDT-FUTURES","channel":"positions",
        "instId":"default"}`（**用 channel**）
      拿 v2 那套去连 UTA ⇒ **登录成功、订阅回执正常，但一帧数据都不来**（数据恒空）✗
      —— 这正是用户报告的现场。故**连之前必须先判定**。

    判据（真机 2026-10-05）：
      * `code == "0"`    ⇒ **UTA** ✓
      * `code == "40084"` ⇒ **Classic（经典）** —— v3 私有端点一律 40084（平台限制）✓
      * 其它错误/异常     ⇒ 记 **WARNING** 并按**非 UTA**（v2）处理 —— **绝不因此崩掉私有 WS**
        （私有 WS 挂了会连带持仓/入场/离场推送全丢）

    ⚠️ **只对 bitget 调用**（OKX 的 `get_account_config` 同样返回 `code="0"`，
       不判交易所就会把 OKX 误判成 UTA ✗）。调用点已用 `_is_uta_capable` 守卫。
    """
    api_key = str(api_info.get("api_key") or "")
    if not api_key:
        return False
    if api_key in _UTA_ACCOUNT_CACHE:
        return _UTA_ACCOUNT_CACHE[api_key]

    is_uta = False
    try:
        # 适配器只能由工厂构造（架构铁律 10）
        from exchange.factory import create_exchange

        bot = create_exchange(api_info)
        # 只读 GET（交易红线按 GET 语义放行 ✓），空参数与该动作在业务层的既有调用一致
        res = bot.custom_actions({}, "get_account_config")
        code = str((res or {}).get("code"))
        if code == "0":
            is_uta = True
            logger.info("PrivateWs [%s] 账户类型探测: UTA(v3) ⇒ 私有 WS 走 /v3/ws/private + topic",
                        api_key[:8])
        elif code == "40084":
            logger.info("PrivateWs [%s] 账户类型探测: Classic(经典, v2) ⇒ 私有 WS 走 /v2/ws/private + channel",
                        api_key[:8])
        else:
            logger.warning(
                "PrivateWs [%s] 账户类型探测返回未预期结果 code=%s msg=%s ⇒ 按非 UTA(v2) 处理",
                api_key[:8], code, (res or {}).get("msg"),
            )
    except Exception as e:  # 传输层/翻译层异常都不该拖垮私有 WS
        logger.warning(
            "PrivateWs [%s] 账户类型探测异常（按非 UTA(v2) 处理，不崩）: %s", api_key[:8], e,
        )

    _UTA_ACCOUNT_CACHE[api_key] = is_uta
    return is_uta


def _is_uta_capable(exchange) -> bool:
    """该账户所属交易所是否有 UTA(v3) 私有 WS 命名空间 —— **查能力表**（不在这写交易所分支）。

    ★ 为什么不能拿"探测结果"当唯一判据：对 OKX 调 `get_account_config` 同样返回
      `code="0"` ⇒ 会把 OKX 误判成 UTA ✗。故先用能力表把范围收在 bitget。
    """
    from exchange.capabilities import supports_uta
    return supports_uta(exchange)


# ══════════════════════════════════════════════════════════════════════
#  订单分流 / 终态口径（★ 2026-10-16 入场列表 `state=filled` 幽灵行事故）
# ══════════════════════════════════════════════════════════════════════

#: 私有 WS **内部频道名 → 订单缓存桶**（★ 分流只看**收到它的频道**，绝不看"有无 `algoId`" ✗）。
#:
#: 真机事故（2026-10-16，入场列表里出现 `state=filled` 的幽灵行）
#: ---------------------------------------------------------------
#: OKX `orders` 频道**也会推「策略单触发后生成的子单」**（`source=6/7/13/25`），
#: 这类帧**同时带 `algoId` + `ordId`**（父计划单的 ID 挂在子单上）⇒ 旧判据
#: 「有 `algoId` 就归 algo 桶」把它并进 `algo_data`，于是**按 `algoId` 覆盖了父计划单**
#: （父单 `effective` → 子单 `filled`）⇒ 入场列表里留下一行 `state=filled`，
#: 每轮随 `limit_data` 推给前端 ✗✗（库里其实已经删干净了 ✓）。
#: ⇒ 判据改成**按频道**：`orders` → `limit_data`、`orders-algo` → `algo_data` ✓。
#: ⚠️ 频道名是**内部（OKX）口径**：OKX 与 Bitget v2 取 `arg.channel`；
#:    Bitget v3(UTA) 由 `arg.topic` 反查（见 `_receive_loop` /
#:    `exchange.capabilities.uta_topic_to_channel`）⇒ 两所都拿得到 ✓。
_ORDER_BUCKET_BY_CHANNEL: Dict[str, str] = {
    "orders": "limit_data",        # 普通限价/市价单（含策略触发生成的子单）
    "orders-algo": "algo_data",    # 策略/条件委托（OKX `orders-algo`；Bitget 条件单）
}

#: **algo（策略委托）行终态** —— 出现在入场/离场列表里即"这张单已不在挂着"⇒ 必须剔除 ✓。
#:
#: 依据 OKX 官方枚举原文（`orders-algo` WS 频道 / `GET /api/v5/trade/order-algo`）：
#:     `live` 待生效 · `pause` 暂停生效 · `partially_effective` 部分生效 ·
#:     `effective` 已生效 · `canceled` 已撤销 · `order_failed` 委托失败 ·
#:     `partially_failed` 部分委托失败
#: 本清单补了两个（2026-10-16）：
#:   * `filled` —— OKX 的 algo 枚举里**根本没有这个词** ⇒ 一旦出现必是**串频道污染**
#:     （见 `_ORDER_BUCKET_BY_CHANNEL`）✗；且 Bitget `planStatus` 的 `executed`/`success`
#:     经 `exchange/bitget_status.normalize_plan_status` **归一成 `filled`**（已触发）
#:     ⇒ 不剔就会**永远**留在入场/离场列表里 ✗✗（两所都需要它 ✓）。
#:   * `partially_failed` —— 官方原文「部分委托失败」，**不是"挂着"**（业务层自己的清理判据
#:     `src/check_order_group.py` 就是 `["canceled","order_failed","partially_failed"]` ✓；
#:     `tests/audit_delete_taxonomy_test.py` 亦裁定「这两个是**未入这个订单**的时候」✓）。
#: ⚠️ **有意不剔**：`live` / `pause`（还在等触发）与 `partially_effective`（**部分**生效 ⇒
#:    可能仍有未生效部分挂着）⇒ 剔掉会**静默隐藏用户还可能存在的挂单** ✗；保持既有行为不变 ✓。
_ALGO_TERMINAL_STATES: tuple = (
    "canceled", "effective", "order_failed", "partially_failed", "filled",
)


class PrivateWsManager:
    """
    多账户私有频道 WebSocket 管理器。

    每个 API Key 维护一个连接。收到数据后：
    1. 更新进程内内存缓存
    2. 构造精简 payload 推送给有推送能力的 ws_manager
    """

    def __init__(self, ws_manager=None):
        """
        :param ws_manager: WebSocketManager 实例，用于向前端推送。可以为 None（仅写缓存）。
        """
        self.ws_manager = ws_manager
        self._connections: Dict[str, dict] = {}  # api_key → {task, business_task, poller_task, api_key, api_info}
        self._running = False
        self._stop_event = asyncio.Event()
        # 推送缓存：记录上次推送的入场/离场数据，避免重复推送未变化的数据
        self._push_cache: Dict[str, dict] = {}  # api_key → {"limit_hash": ..., "close_hash": ...}
        # 进程内内存缓存：WS 接收到的最新数据，供同进程模块直接读取，不写文件
        self._cache: Dict[str, dict] = {}  # api_key → {"hold_data": [...], "limit_data": [...], "algo_data": [...]}
        # 仓位动作识别基准标记：api_key → 是否已有权威持仓基准。
        # 未建立基准时首帧不产事件（否则会把存量持仓全部误判为「开仓」）。
        self._pos_baseline: Dict[str, bool] = {}
        # 空 instId 持仓兜底日志的限流器：(api_key, 原因) → 上次打印时间戳 / 折叠条数。
        # Bitget 私有 WS 周期性重连（真机实测 464 次）会反复触发兜底，不限流必刷屏。
        self._posfix_log: Dict[tuple, float] = {}
        self._posfix_skipped: Dict[tuple, int] = {}

    # ========================================================================
    # 生命周期
    # ========================================================================

    async def start(self):
        """启动管理循环：检测 api_list 变更，自动创建/销毁连接。"""
        self._running = True
        logger.info("PrivateWsManager 启动")
        while self._running:
            try:
                await self._sync_connections()
            except Exception as e:
                logger.error("PrivateWsManager 同步异常: %s", e, exc_info=True)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=30)
                break
            except asyncio.TimeoutError:
                pass
        await self._close_all()
        logger.info("PrivateWsManager 已停止")

    async def stop(self):
        """优雅停止。"""
        self._running = False
        self._stop_event.set()

    # ========================================================================
    # 连接管理
    # ========================================================================

    async def _sync_connections(self):
        """根据当前 api_list 增删连接，过滤禁用/权限不符合的 API。"""
        api_list = self._get_api_list()
        valid_apis = self._filter_valid_apis(api_list)
        current_keys = set(self._connections.keys())
        desired_keys = {item["api_key"] for item in valid_apis}

        # 新增
        for api_info in valid_apis:
            key = api_info["api_key"]
            if key not in current_keys:
                await self._add_connection(api_info)

        # 移除
        for key in current_keys - desired_keys:
            await self._remove_connection(key)

    def _filter_valid_apis(self, api_list: list) -> list:
        """过滤掉不可用的 API，统一使用 bot_utils.check_api_usable 做校验。
        注意：flag = "1" 是模拟盘，不是禁用，仍然允许。
        """
        from src.utils.bot_utils import BotUtils

        valid = []
        for api in api_list:
            # ★ 2026-10-05 用户裁定：v2/Classic 账户**只做行情缓存** ⇒ **不为它连私有 WS** ✓
            #   （否则它会推送自己的持仓，前端会看到"已废弃账户"的持仓 ✗）
            #   规则：`api_list` 有 `private_ws` 列且为 0/false/no/off ⇒ 跳过 ✓；
            #        没该列时，账户名含「仅行情缓存」⇒ 默认跳过 ✓（可用列显式覆盖 ✓）
            _ps = str(api.get("private_ws", "") or "").strip().lower()
            _nm = str(api.get("account_name", "") or "")
            # ★★ 2026-10-13 用户裁定（原话 ✓）：「模拟盘能显示什么，**实盘就应该能显示什么**」
            #   ⇒ `private_ws` **显式**给了值就以它为准 ✓（列此前未注册 ⇒ 写了也存不住 ✗，
            #     已在 `file_set.ini` 注册 ✓）；**空 ⇒ 回退按账户名推断**（老行为不变 ✓）。
            if _ps in ("1", "true", "yes", "on"):
                _skip = False
            elif _ps in ("0", "false", "no", "off"):
                _skip = True
            else:
                _skip = "仅行情缓存" in _nm
            if _skip:
                logger.info("PrivateWs 跳过行情缓存专用账户 [%s] %s（只做行情 ✓）",
                            str(api.get("api_key", "?"))[:8], _nm)
                continue
            usable, reason = BotUtils().check_api_usable(api)
            if not usable:
                logger.info(
                    "PrivateWs 过滤 API [%s]: %s",
                    api.get("api_key", "?")[:8],
                    reason,
                )
                continue
            valid.append(api)

        return valid

    def _prefill_from_snapshot(self, api_key: str):
        """连接建立时，用 json/data 分账户快照预填进程内缓存，作为 WS 就绪前的本地兜底。

        json/data/{api_key}.json 为 modify_order_monitor 秒级 REST 拉取落盘的分账户快照，
        含 hold_data / limit_data / algo_data，字段与 WS 推送格式兼容（含 instId/posSide/
        side/tdMode/state/ordType）。进程重启或 WS 断连重连后内存缓存为空，前端会显示空窗
        （入场/离场/持仓先 0 后恢复）。预填后 WS 增量推送通过 _on_positions（本地兜底+剔除）
        与 _on_orders（_merge_by_key 合并）覆盖，实现「本地兜底 + WS 增量」。
        """
        try:
            snapshot = read_api_json(api_key)
            if not isinstance(snapshot, dict):
                return
            if api_key not in self._cache:
                self._cache[api_key] = {}
            for field in ("hold_data", "limit_data", "algo_data"):
                data = snapshot.get(field)
                if not isinstance(data, list):
                    continue
                # 补 symbol 字段，保持与 WS 缓存格式一致
                for item in data:
                    if isinstance(item, dict) and not item.get("symbol"):
                        item["symbol"] = item.get("instId", "")
                self._cache[api_key][field] = data
            # 注意：预填数据只作「WS 就绪前的显示兜底」，不建立事件判定基准。
            # json/data 快照是上次运行落盘的产物，进程停止期间的真实仓位变动与它存在偏差；
            # 若拿它当基准，重启后首帧 WS 全量会把「停机期间早已发生的变动」补发成实时动作通知
            # （典型：停机期间清仓，重启瞬间收到一串「平仓」）。
            # 基准改由「首个 WS 帧」自然建立 —— 见 _on_positions 中无基准时首帧只对齐、不产事件。
            loaded = [k for k in ("hold_data", "limit_data", "algo_data") if k in self._cache[api_key]]
            if loaded:
                logger.info(
                    f"PrivateWs [{api_key[:8]}] 已用 json/data 快照预填缓存: "
                    f"hold={len(self._cache[api_key].get('hold_data', []))} "
                    f"limit={len(self._cache[api_key].get('limit_data', []))} "
                    f"algo={len(self._cache[api_key].get('algo_data', []))}"
                )
        except Exception as e:
            logger.warning(f"PrivateWs [{api_key[:8]}] 预填 json 快照失败: {e}", exc_info=True)

    async def _add_connection(self, api_info: dict):
        """为一个 API Key 创建私有 WS 连接和业务 WS 连接。"""
        api_key = api_info["api_key"]
        secret_key = api_info["secret_key"]
        passphrase = api_info.get("passphrase", "")
        flag = api_info.get("flag", "1")  # 0=实盘, 1=模拟盘

        # 预填 json/data 分账户快照作为本地兜底（供 _on_positions/_on_orders 的 WS 兜底基准）。
        # 注意：不再在此处广播预填数据 —— 兜底广播已改为“前端连上后端 WS 时”由
        # push_initial_snapshot_to_client 定向推送（websocket_market auth_success 触发）。
        # 此前绑定“后端与 OKX 建连时广播”会因前端尚未连上而丢失，且与前端连接推送重复。
        self._prefill_from_snapshot(api_key)

        # ★ 按**该账户自己的交易所**解析地址（api_list.exchange）——
        #   一个进程可同时服务多所账户，不再假设都是 OKX。
        _ex = api_info.get("exchange")
        _demo = flag != "0"
        # ★★ 2026-10-05：Bitget **UTA(v3)** 账户必须走 v3 命名空间 + `topic` 式订阅。
        #    v2 端点(v2 参数)连 UTA ⇒ 登录成功、订阅回执正常，但**数据恒空** ✗（本轮修的就是它）。
        #    Classic(经典) 账户**不支持 v3**（REST 40084 真机确认）⇒ 继续走 v2 ✓（一字不改）。
        _uta = _is_uta_account(api_info) if _is_uta_capable(_ex) else False
        _kind_private = "private_v3" if _uta else "private"
        _kind_business = "business_v3" if _uta else "business"
        # 私有 WS: positions + orders（限价单）
        private_url = ws_endpoint(_ex, _kind_private, demo=_demo)
        task = asyncio.create_task(
            self._run_connection(api_key, secret_key, passphrase, private_url, exchange=_ex, uta=_uta),
            name=f"priv_ws_{api_key[:8]}"
        )
        # 业务 WS: orders-algo（条件单/止盈止损）
        business_url = ws_endpoint(_ex, _kind_business, demo=_demo)
        business_task = asyncio.create_task(
            self._run_business_connection(api_key, secret_key, passphrase, business_url, exchange=_ex, uta=_uta),
            name=f"biz_ws_{api_key[:8]}"
        )
        self._connections[api_key] = {
            "task": task,
            "business_task": business_task,
            "api_key": api_key,
            # ★ 存下账户信息：持仓定时刷新的轮询任务需要它走工厂建适配器
            #   （架构铁律 10：适配器只能由 `create_exchange` 出来）。
            "api_info": api_info,
            # ★ 持仓定时刷新任务（仅能力表声明 `ws_positions_periodic_seconds > 0`
            #   的所才会真的起；OKX 自己每 ~5s 定时推 ⇒ 恒为 None，行为一字不变）。
            "poller_task": None,
        }
        self._connections[api_key]["poller_task"] = self._start_positions_poller(api_info)
        logger.info(
            "PrivateWs 已创建连接: %s... flag=%s exchange=%s uta=%s private_url=%s",
            api_key[:12], flag, _ex, _uta, private_url,
        )

    async def _remove_connection(self, api_key: str):
        """移除连接。"""
        conn = self._connections.pop(api_key, None)
        if conn:
            # ★ 持仓定时刷新任务也要**同样干净取消**（`cancel()` + `await`）：
            #   只 cancel 不 await 会留下未回收的任务，且它可能在连接移除后
            #   又醒来发一次 REST 请求（"关闭后不再请求"）。
            for task_key in ("task", "business_task", "poller_task"):
                t = conn.get(task_key)
                if t:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
        # 清理仓位动作识别基准，避免该 api_key 再次接入时残留旧基准
        self._pos_baseline.pop(api_key, None)
        logger.info(f"PrivateWs 已移除连接: {api_key[:12]}...")

    async def _close_all(self):
        """关闭所有连接。"""
        for api_key in list(self._connections.keys()):
            await self._remove_connection(api_key)

    # ========================================================================
    # 应用层心跳（**仅 Bitget 需要**：30s 内不发字符串 "ping" 就被服务端断）
    # ========================================================================

    def _start_app_ping(self, ws, api_key: str, exchange: str):
        """按**能力表**决定是否为该连接起应用层心跳任务；不需要 ⇒ 返回 ``None``。

        ★ 为什么必须"按交易所"而不是无脑发：**Bitget 公共与私有 WS 都要求每 30 秒
          发一次字符串 `"ping"`**（不发就主动断），而 **OKX 的 WS 协议里没有这个
          应用层报文**（乱发会被当未知报文）⇒ 判据只能查能力表
          （`exchange/capabilities.py::ws_needs_app_ping`，架构铁律 9：
          业务层不得**按交易所名写分支**，一律查表）。

        ★ 为什么先登录+订阅再起心跳：心跳是给"已建立的会话"续命的，
          登录/订阅帧还没发完就插进去只会打乱时序（且此时连接断了与心跳无关）。
        """
        # 查表：未登记的交易所**不兜底**（capability 直接抛）
        from exchange.capabilities import ws_needs_app_ping, private_ws_app_ping_interval

        if not ws_needs_app_ping(exchange):
            return None
        interval = private_ws_app_ping_interval()
        task = asyncio.create_task(self._app_ping_loop(ws, api_key, exchange))
        logger.info(
            "PrivateWs [%s] 已启动应用层心跳: 每 %.0fs 发字符串 ping（%s 私有/业务 WS 要求 <30s）",
            api_key[:8], interval, exchange,
        )
        return task

    async def _app_ping_loop(self, ws, api_key: str, exchange: str):
        """连接存活期间每 25 秒发一次字符串 ``"ping"``（**只对需要心跳的所起此任务**）。

        ⚠️ 发送**必须** try/except：心跳失败（连接正在关闭等）**绝不能**把接收循环
          一起搞崩 —— 接收循环才是持仓/订单推送的命脉。此处只记 WARNING。
        """
        from exchange.capabilities import private_ws_app_ping_interval

        interval = private_ws_app_ping_interval()
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await ws.send("ping")
                    logger.debug("PrivateWs [%s] 已发送应用层心跳 ping", api_key[:8])
                except Exception as e:  # noqa: BLE001 —— 心跳失败不升级、不抛出
                    logger.warning(
                        "PrivateWs [%s] 应用层心跳发送失败（不影响接收循环）: %s",
                        api_key[:8], e,
                    )
        except asyncio.CancelledError:
            # 连接退出/被取消 ⇒ 由 _stop_app_ping 取消，属**预期**路径，必须原样抛出
            logger.debug("PrivateWs [%s] 应用层心跳任务已取消", api_key[:8])
            raise

    async def _stop_app_ping(self, task, api_key: str):
        """取消心跳任务并**等它真正结束**（保证关闭后不再有 ping 发出、任务不泄漏）。

        ``await task`` 是必要的：只 ``cancel()`` 不 await 会留下未回收的任务，
        而且它可能在 `ws.close()` 之后才醒过来又发一次。
        """
        if task is None:
            return None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001 —— 收尾阶段任何异常都不该影响重连
            logger.warning("PrivateWs [%s] 应用层心跳任务收尾异常: %s", api_key[:8], e)
        return None

    # ========================================================================
    # 持仓定时刷新（**只对「持仓频道不周期推送」的所**起：能力表 >0，Bitget=5.0）
    # ========================================================================

    def _start_positions_poller(self, api_info: dict):
        """按**能力表**决定是否为该账户起"持仓定时刷新"任务；不需要 ⇒ 返回 ``None``。

        ★ 为什么需要：**OKX 的 `positions` 频道自己就「大约每 5 秒」定时推**
          （官方文档原文，见 `exchange/capabilities.py` 的 okx 条目）⇒ 前端浮盈/标记价
          本来就自己动；而 **Bitget UTA(v3) `topic=position` 是纯事件推送**（真机
          建连后 140 秒零数据帧）⇒ 不补定时刷新，持仓行永远停在首次快照上
          （用户现场感受：OKX 正常、Bitget 不正常）。

        ★ 为什么查表而不是**按交易所名写分支**：架构铁律 9 ——
          交易所差异只声明在 `exchange/capabilities.py`，业务层一律查表。
          未登记的交易所 ⇒ `positions_refresh_seconds` **直接抛**（不猜、不兜底）。
        """
        from exchange.capabilities import positions_refresh_seconds

        api_key = str(api_info.get("api_key") or "")
        exchange = api_info.get("exchange")
        # 未登记 / 漏登记该能力的交易所 ⇒ 这里就抛（不静默降级成 0 或默认值）
        interval = float(positions_refresh_seconds(exchange))
        if interval <= 0:
            # 该所自己会定时推（OKX ~5s）⇒ **不起任务**，OKX 行为一字不变 ✓
            logger.info(
                "PrivateWs [%s] 持仓频道 %s 自带定时推送 ⇒ 不启动持仓定时刷新（间隔=%.1fs）",
                api_key[:8], exchange, interval,
            )
            return None
        task = asyncio.create_task(
            self._positions_poll_loop(api_info), name=f"pos_poll_{api_key[:8]}"
        )
        logger.info(
            "PrivateWs [%s] 已启动持仓定时刷新: 每 %.1fs 用 REST(get_positions) 拉 %s 权威持仓",
            api_key[:8], interval, exchange,
        )
        return task

    async def _positions_poll_loop(self, api_info: dict):
        """每 `interval` 秒用 REST 拉一次**交易所权威持仓**，交给现成的 `_on_positions` 路径。

        ★ 为什么用 REST 而不是拿本地行情价自算浮盈：自算口径与交易所**必然不一致**
          （标记价/资金费/保证金口径各异）⇒ 前端会显示错的浮盈 ✗。
          这里只搬交易所给的原值（`upl`/`markPx` …）；`get_positions` 是 **GET/只读**
          ⇒ 不触发交易红线 ✓。

        ★ 为什么必须**复用 `_on_positions`**（不另写一套合并/广播）：那条路径含
          「本地兜底 + 剔除已平 + 按 (instId, posSide) 合并 + 空 instId 回填/丢弃」
          的完整语义与 `positions_push` 广播；另写一套必然出现"先 0 后 N"抖动 ✗。

        ★ `action` 取 **``"snapshot"``** + **`authoritative=True`**：REST `get_positions` 返回的是
          该账户**当前全量持仓**（不是增量）⇒ 形状就是 snapshot；而 `authoritative=True` 表达
          **来源**（我们自己拉的权威全量，不是交易所推送）—— 两个维度正交，故不塞进 action。
          在本路径下 `_on_positions` **以 REST 为准**：`merged = active`、**空就是空**
          （用户全平 ⇒ REST 返回 `[]` ⇒ 清缓存 + 由 `_diff_positions` 产出 `close` 事件
          ⇒ 前端左下角平仓通知 + 持仓行消失）。
          ✅ 这正是 2026-10-05 修掉的缺口：原来与 WS 共用 `action="snapshot"`，而该分支对
          `payload=[]` 显式**保留旧缓存**（防 Bitget 重连重发空快照的抖动）⇒ 我们自己的权威全量
          被同一个防抖套住 ⇒ **全平后不清屏、也不产 close 事件** ✗。
          ⚠️ 交易所 WS 空快照仍保留旧缓存（`authoritative` 默认 False，那条路一字未动 ✓）；
          仍不写入任何 instId 为空且回填不了的坏行（跳过 + 记日志，不兜底）。

        ★ 异常只记 WARNING/ERROR：**绝不能影响 WS 接收循环**（那条才是持仓/订单推送的命脉）。
        """
        from exchange.capabilities import positions_refresh_seconds

        api_key = str(api_info.get("api_key") or "")
        exchange = api_info.get("exchange")
        interval = float(positions_refresh_seconds(exchange))
        rounds = 0
        try:
            while self._running and api_key in self._connections:
                await asyncio.sleep(interval)
                # 缓存里没有持仓 ⇒ **跳过本轮**（省请求）：开仓那一刻由 WS 事件推送先落缓存
                _acct = self._cache.get(api_key) or {}
                _hold = _acct.get("hold_data") if isinstance(_acct, dict) else None
                if not _hold:
                    logger.debug("PrivateWs [%s] 持仓定时刷新: 缓存无持仓，跳过本轮", api_key[:8])
                    continue
                rounds += 1
                try:
                    from exchange.factory import create_exchange  # 铁律 10：只能走工厂
                    bot = create_exchange(api_info)
                    # ★ 2026-10-06：**必须 to_thread** —— 这是 5s 轮询循环，同步 REST 会
                    #   **阻塞事件循环**（同一 loop 上还有 WS 收帧与 `positions_push` 广播）
                    #   ⇒ 与同文件 `_reconcile_algo_cache` 的做法保持一致 ✓
                    res = await asyncio.to_thread(bot.custom_actions, {}, "get_positions")
                    code = str((res or {}).get("code"))
                    if code != "0":
                        # 不兜底：业务级失败 ⇒ 记 WARNING 跳过本轮（绝不用旧值冒充新值）
                        logger.warning(
                            "PrivateWs [%s] 持仓定时刷新第 %d 轮业务失败: code=%s msg=%s",
                            api_key[:8], rounds, code, (res or {}).get("msg"),
                        )
                        continue
                    data = (res or {}).get("data")
                    if not isinstance(data, list):
                        # 形状不符 ⇒ ERROR（不静默降级；`data` 一律 list 是适配层铁律 3）
                        logger.error(
                            "PrivateWs [%s] 持仓定时刷新第 %d 轮 data 非 list（%s）⇒ 丢弃本帧",
                            api_key[:8], rounds, type(data).__name__,
                        )
                        continue
                    logger.info(
                        "PrivateWs [%s] 持仓定时刷新第 %d 轮（权威全量·REST）: 缓存 %d 行 ⇒ 拉取 %d 行",
                        api_key[:8], rounds, len(_hold), len(data),
                    )
                    # ★ `authoritative=True` = 来源标识：这是**我们自己的 REST 权威全量**
                    #   ⇒ `_on_positions` 以它为准（空即空、清缓存、可产 close 事件），
                    #   与交易所 WS 帧（仍保留旧缓存）严格区分。`action` 仍是 "snapshot"
                    #   （形状确为全量），来源由关键字参数表达，不塞进 action（防帧内容伪造来源）。
                    await self._on_positions(api_key, data, "snapshot", authoritative=True)
                except Exception as e:  # noqa: BLE001 —— 单轮失败绝不牵连 WS 接收循环
                    logger.warning(
                        "PrivateWs [%s] 持仓定时刷新第 %d 轮异常（不影响 WS 接收循环）: %s",
                        api_key[:8], rounds, e,
                    )
        except asyncio.CancelledError:
            # 连接移除 / 管理器停止 ⇒ 属**预期**路径，必须原样抛出
            logger.debug("PrivateWs [%s] 持仓定时刷新任务已取消", api_key[:8])
            raise

    # ========================================================================
    # 单连接运行
    # ========================================================================

    async def _run_connection(self, api_key: str, secret_key: str, passphrase: str, ws_url: str,
                              exchange: str = "", uta: bool = False):
        """维持单条私有 WS 连接，自动重连。`uta=True` ⇒ Bitget UTA(v3)：订阅用 `topic` ✓。"""
        ws = None
        hb_task = None
        retry_count = 0
        base_delay = 1.0

        while self._running and api_key in self._connections:
            try:
                ws = await websockets.connect(
                    ws_url,
                    open_timeout=10,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=10,
                )
                logger.debug(f"PrivateWs [{api_key[:8]}] 已连接")

                # 登录
                if not await self._login(ws, api_key, secret_key, passphrase, exchange):
                    logger.warning(f"PrivateWs [{api_key[:8]}] 登录失败，重连")
                    await ws.close()
                    await asyncio.sleep(2)
                    continue

                retry_count = 0

                # 订阅
                await self._subscribe(ws, exchange, uta=uta)

                # 应用层心跳（仅 bitget 等能力表标记 ws_ping_mode=app 的所会起任务）
                hb_task = self._start_app_ping(ws, api_key, exchange)

                # 接收循环
                await self._receive_loop(ws, api_key, exchange, uta=uta)

            except asyncio.CancelledError:
                break
            except websockets.ConnectionClosed as e:
                logger.warning(f"PrivateWs [{api_key[:8]}] 连接关闭: {e}", exc_info=True)
            except Exception as e:
                logger.error(f"PrivateWs [{api_key[:8]}] 异常: {e}", exc_info=True)
            finally:
                # ★ 无论正常退出 / 异常 / 被取消（含上面的 break）都要**干净取消**心跳，
                #   且必须发生在 `ws.close()` **之前** —— 否则它可能在关闭后还发一次 ping。
                hb_task = await self._stop_app_ping(hb_task, api_key)

            # 关闭旧连接
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass
                ws = None

            # 重连延迟
            if self._running and api_key in self._connections:
                delay = base_delay * (2 ** min(retry_count, 6))
                logger.info(f"PrivateWs [{api_key[:8]}] {delay:.1f}s 后重连")
                await asyncio.sleep(delay)
                retry_count += 1

    async def _run_business_connection(self, api_key: str, secret_key: str, passphrase: str, ws_url: str,
                                       exchange: str = "", uta: bool = False):
        """维持单条业务 WS 连接（orders-algo 频道），自动重连。

        `uta=True` ⇒ Bitget UTA(v3)：业务订阅 topic 是 `strategy-order`（v2 叫 `orders-algo`）✓
        """
        ws = None
        hb_task = None
        retry_count = 0
        base_delay = 1.0

        while self._running and api_key in self._connections:
            try:
                ws = await websockets.connect(
                    ws_url,
                    open_timeout=10,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=10,
                )
                logger.debug(f"PrivateWs [{api_key[:8]}] 业务WS 已连接")

                if not await self._login(ws, api_key, secret_key, passphrase, exchange):
                    logger.warning(f"PrivateWs [{api_key[:8]}] 业务WS 登录失败，重连")
                    await ws.close()
                    await asyncio.sleep(2)
                    continue

                retry_count = 0
                await self._subscribe_business(ws, exchange, uta=uta)

                # 应用层心跳（与私有连接同一套判据；仅 bitget 会真的起任务）
                hb_task = self._start_app_ping(ws, api_key, exchange)

                await self._receive_loop(ws, api_key, exchange, uta=uta)

            except asyncio.CancelledError:
                break
            except websockets.ConnectionClosed as e:
                logger.warning(f"PrivateWs [{api_key[:8]}] 业务WS 连接关闭: {e}", exc_info=True)
            except Exception as e:
                logger.error(f"PrivateWs [{api_key[:8]}] 业务WS 异常: {e}", exc_info=True)
            finally:
                # ★ 与私有连接同理：退出（含 break / 异常 / 取消）前干净取消心跳任务
                hb_task = await self._stop_app_ping(hb_task, api_key)

            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass
                ws = None

            if self._running and api_key in self._connections:
                delay = base_delay * (2 ** min(retry_count, 6))
                logger.info(f"PrivateWs [{api_key[:8]}] 业务WS {delay:.1f}s 后重连")
                await asyncio.sleep(delay)
                retry_count += 1

    # ========================================================================
    # OKX 私有 WS 协议
    # ========================================================================

    async def _login(self, ws, api_key: str, secret_key: str, passphrase: str, exchange: str = "") -> bool:
        """私有 WS 登录（**签名路径按交易所不同**）。

        ★★ 真机实测（2026-10-03）：两所的签名串**不一样**，用错直接
           ``{"event":"error","code":30015,"msg":"Invalid sign"}`` ——
           而且会每 1~2 秒重连一次（刷日志 + 私有推送全废）：
               OKX    : ``{ts}GET/users/self/verify``
               Bitget : ``{ts}GET/user/verify``      ← **单数 user**
        """
        timestamp = str(int(time.time()))
        # ★ 登录签名路径**查能力表**（两所不同，见 exchange/capabilities.py）
        from exchange.capabilities import ws_login_path as _ws_login_path
        _sign_path = _ws_login_path(exchange)
        sign_msg = timestamp + "GET" + _sign_path
        sign = base64.b64encode(
            hmac.new(secret_key.encode(), sign_msg.encode(), "sha256").digest()
        ).decode()

        login_msg = {
            "op": "login",
            "args": [{
                "apiKey": api_key,
                "passphrase": passphrase,
                "timestamp": timestamp,
                "sign": sign,
            }]
        }
        await ws.send(json.dumps(login_msg))

        try:
            resp = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(resp)
            # ⚠️ Bitget 返回的 code 是**整数** 0，OKX 是**字符串** "0"（真机实测）
            #    ⇒ 必须统一成字符串比较，否则 Bitget 明明登录成功
            #    （event=login, code=0）也会被判成失败并**无限重连**。
            if data.get("event") == "login" and str(data.get("code")) == "0":
                logger.info(f"PrivateWs [{api_key[:8]}] 登录成功")
                return True
            logger.warning(f"PrivateWs [{api_key[:8]}] 登录响应: {data}")
            return False
        except asyncio.TimeoutError:
            logger.warning(f"PrivateWs [{api_key[:8]}] 登录超时")
            return False

    async def _subscribe(self, ws, exchange: str = "", uta: bool = False):
        """订阅私有频道。

        ⚠️ **关键差异：v3(UTA) 用 `topic`，v2 用 `channel`**（见 `ws_private_sub_args`）——
        拿 v2 的 `channel` 去 v3 订阅会**回执正常但没有数据** ✗。
        """
        # ★ 订阅帧**按所构造**（查能力表）—— 照 OKX 传 `instType:"SWAP"` 会被
        #   Bitget 拒：`30016 Param error`（真机实测 2026-10-03）
        from exchange.capabilities import ws_private_sub_args as _sub
        channels = [
            _sub(exchange, "positions", uta=uta),
            _sub(exchange, "orders", uta=uta),
        ]
        sub_msg = {"op": "subscribe", "args": channels}
        await ws.send(json.dumps(sub_msg))
        logger.info(
            "PrivateWs 已发送订阅: positions + orders (%s) args=%s",
            "UTA/v3 topic" if uta else "v2 channel", channels,
        )

    async def _subscribe_business(self, ws, exchange: str = "", uta: bool = False):
        """订阅业务频道（策略委托：v3 叫 `strategy-order`，v2 叫 `orders-algo`）。"""
        from exchange.capabilities import ws_private_sub_args as _sub
        channels = [_sub(exchange, "orders-algo", uta=uta)]
        sub_msg = {"op": "subscribe", "args": channels}
        await ws.send(json.dumps(sub_msg))
        logger.info(
            "PrivateWs 已发送业务订阅: orders-algo (%s) args=%s",
            "UTA/v3 topic" if uta else "v2 channel", channels,
        )

    async def _receive_loop(self, ws, api_key: str, exchange: str = "", uta: bool = False):
        """接收数据循环。"""
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # 事件消息：订阅确认 / 错误，记录日志后跳过
            if "event" in data:
                event = data.get("event", "")
                if event == "subscribe":
                    arg = data.get("arg", {})
                    # ⚠️ v3(UTA) 的回执里是 `topic`、v2 是 `channel` —— 日志两个都要认
                    #    （UTA 应打出 `订阅确认: position.UTA` ✓）
                    logger.info(
                        f"PrivateWs [{api_key[:8]}] 订阅确认: "
                        f"{arg.get('channel') or arg.get('topic') or '?'}.{arg.get('instType', '?')}"
                    )
                elif event == "error":
                    logger.error(
                        f"PrivateWs [{api_key[:8]}] WS 错误: "
                        f"code={data.get('code', '?')} msg={data.get('msg', '?')}"
                    )
                elif event == "login":
                    pass  # login 已在 _login 方法中处理
                else:
                    logger.debug(
                        f"PrivateWs [{api_key[:8]}] 事件: {event}"
                    )
                continue

            # ★★ **v3(UTA) 的帧用 `topic`、v2 的帧用 `channel`**（关键差异，2026-10-05）——
            #    原来只读 `channel` ⇒ UTA 的持仓帧 channel 恒为空 ⇒ 全部落到「未知频道」分支
            #    ⇒ 即使订阅对了也**不更新缓存/不广播** ✗ ⇒ 前端永远看不到 UTA 的持仓。
            #    故：先取 `channel`，缺失时用 `topic` 反查回内部频道名。
            _arg = data.get("arg", {}) or {}
            channel = str(_arg.get("channel") or "")
            if not channel:
                _topic = str(_arg.get("topic") or "")
                if _topic:
                    from exchange.capabilities import uta_topic_to_channel
                    channel = uta_topic_to_channel(_topic)
                    if not channel:
                        # 未登记的 topic ⇒ 记 WARNING 并跳过本帧（不猜、不静默降级）
                        logger.warning(
                            "PrivateWs [%s] 未登记的 UTA topic=%r ⇒ 跳过本帧（请补 "
                            "exchange/capabilities.py 的 _UTA_TOPIC_BY_CHANNEL）",
                            api_key[:8], _topic,
                        )
                        continue
            if channel == "account":
                # UTA 账户帧（`topic=account`）：当前不落缓存、不广播，
                # 但也**不能**拿 orders 的映射去套它 ⇒ 显式跳过并留痕。
                logger.info("PrivateWs [%s] 收到 UTA 账户帧 %s 行（暂不落缓存/广播）",
                            api_key[:8], len(data.get("data") or []))
                continue
            payload = data.get("data", [])
            # ★★ **WS 与 REST 是同一份数据，必须用同一套翻译**（2026-10-03 用户裁定）：
            #    Bitget 推的是 `holdSide` / `total` / `planStatus`，
            #    而下游 `_on_positions` / `_on_orders` 读的是 OKX 规范字段
            #    `posSide` / `pos` / `state` ⇒ 不翻译时
            #      · `p.get("pos")` 恒为 None ⇒ 所有持仓被判「已平仓」⇒ **持仓被清空**
            #      · 入场的 `states` 一直打印成 `?`
            #    前端之所以看起来正常，是因为 base_info 轮询 + 连接时的 json 快照兜底。
            #    映射复用 REST 的 `_fix_*`（单一真相，见 exchange/ws_translate.py）。
            from exchange.ws_translate import translate_ws_payload
            payload = translate_ws_payload(exchange, channel, payload)

            if channel == "positions":
                await self._on_positions(api_key, payload, data.get("action", "update"))
            elif channel in ("orders", "orders-algo"):
                # DEBUG: 确认业务 WS 是否收到数据
                if channel == "orders-algo":
                    states = [o.get("state", "?") for o in (payload if isinstance(payload, list) else [])]
                    logger.info(
                        f"PrivateWs [{api_key[:8]}] 收到 orders-algo: "
                        f"{len(payload)}条 states={states}"
                    )
                    # [TEMP DEBUG] 打印原始价格字段，定位「改价后 WS 负载是否带新价」
                    try:
                        _dbg = [
                            {k: o.get(k, "") for k in ("algoId", "state", "triggerPx", "ordPx", "px", "sz", "side", "posSide", "instId", "tdMode")}
                            for o in (payload if isinstance(payload, list) else []) if isinstance(o, dict)
                        ]
                        logger.info(f"PrivateWs [{api_key[:8]}] orders-algo RAW: {_dbg}")
                    except Exception as e:
                        logger.error("[静默异常] _receive_loop 失败: %s", e, exc_info=True)
                await self._on_orders(api_key, payload, channel=channel)
            else:
                logger.debug(
                    f"PrivateWs [{api_key[:8]}] 未知频道: {channel}"
                )

    # ========================================================================
    # 数据处理
    # ========================================================================

    async def _on_positions(self, api_key: str, payload: list, action: str = "update",
                            *, authoritative: bool = False):
        """处理持仓推送：本地兜底 + 按 WS 增量剔除平仓仓位。

        OKX positions 频道为增量推送（平仓时被平的仓位以 pos:"0" 出现，而非整个快照）。
        若仅以 WS 为唯一权威（过滤 pos>0），平仓瞬间推送的"全 0 中间态"会被误当成
        "清空持仓"广播，下一秒又推送真实剩余持仓，前端就是"先 0 后 3"的抖动。

        正确做法：以内存缓存 hold_data 为本地兜底基准（当前 N 个持仓），
        收到 WS 推送时，剔除其中 pos<=0 的已平仓位，再合并保留 pos>0 的存活持仓。
        这样本地 4 个 -> 剔除被平的 1 个 -> 剩 3 个，不会清零。

        ★★ 2026-10-04 真机 bug（Bitget）：**空 instId 的行不许顶掉好行** ——
        Bitget 私有 WS 的 `positions` 推送里**没有 `symbol` 字段**（`instId`
        恒为空串），而翻译层 `_fix_position` 是从 `symbol` 取 `instId` 的 ⇒
        推送行的键成了 `("", posSide)`，与缓存里的好行 `("BTC-USDT-SWAP", posSide)`
        不是同一个键：

          · `snapshot` 分支原来是**裸替换**（``merged = active``）⇒ 好行被整片顶掉
            ⇒ 前端持仓「一会有一会没有」、**图标（symbol）丢失**；
          · 下一次 REST `base_info` 轮询又把好行补回来 ⇒ 就是用户看到的抖动。
          · Bitget 私有 WS 周期重连（真机 464 次）每次重发 snapshot ⇒ 反复抖。

        本函数的约束（**对所有交易所生效**，不写成只对 bitget 生效）：
          ① 推送行 `instId` 为空时，先用 `posId` 从旧缓存找回正确的 `instId`；
          ② 仍找不回 ⇒ **保留旧行，绝不写入空 `instId` 的新行**（宁可用旧数据）；
          ③ `snapshot` 里 payload 为空（`[]`）⇒ **不清空**（保留旧缓存，靠 REST 校正）；
          ④ 含无法回填的行的 `snapshot` 视为**不可信**，退化为增量合并（同样不清空）。

        ★★ `authoritative` —— **来源标识**（2026-10-05 新增，只由 `_positions_poll_loop`
        这个「我们自己的 REST 权威全量」传入；**交易所 WS 路一律不传**）：
          · 为什么用**关键字参数**而不是新 `action` 值（如 `"rest_poll"`）：
            ① `action` 在 WS 分发处取自**交易所帧自身**（`data.get("action", "update")`）
               ⇒ 把"权威"塞进 `action` 等于让**帧内容**能伪造来源（坏帧/未来新 action
               名字撞车就会被当成权威全量 ⇒ 误清缓存+误报平仓 ✗）；
            ② `action` 表达的是**形状**（snapshot=全量 / update=增量），来源是**正交**的
               第二维度 ⇒ 用布尔参数不混淆两个语义，且默认 False 让**所有既有调用点
               （WS）行为一字不变** ✓。
          · 语义：**以 REST 为准** —— `merged = active`（存活行），**空就是空**
            （用户全平后 `get_positions` 返回 `[]` ⇒ 必须清缓存，并由 `_diff_positions`
            产出 `close` 事件 ⇒ 前端左下角平仓通知 + 持仓行消失）；
            但**不套用**「空快照保留旧缓存」的防抖（那条是给交易所重连重发空 snapshot 的）。
          · 仍然**不写入任何 instId 为空的行**（回填不上的坏行跳过 + 记日志）。
          · 其余（`_diff_positions` 产事件 / `_update_cache` / `_simplify_positions` / 广播）
            完全复用下面这条现成路径 —— 不另写一套 ✓。
        """
        try:
            # DEBUG: 记录原始 payload 结构与 action，确认 OKX 推送是全量(snapshot)还是增量(update)
            try:
                logger.info(
                    f"PrivateWs [{api_key[:8]}] positions action={action} "
                    f"payload={json.dumps(payload, ensure_ascii=False)[:1800]}"
                )
            except Exception as e:
                logger.error("[静默异常] _on_positions 失败: %s", e, exc_info=True)

            # ★ 权威全量的**形状硬校验**（不兜底）：适配层铁律 3 —— `data` 一律 list。
            #   形状不符 ⇒ 记 ERROR 并**丢弃本帧**（**绝不**当成"空持仓"处理 —— 否则一次坏形状
            #   就会误清缓存 + 给用户误弹"平仓"通知 ✗）。轮询循环里也有一道同样的守卫。
            if authoritative and not isinstance(payload, list):
                logger.error(
                    "PrivateWs [%s] 持仓定时刷新（权威全量·REST）payload 非 list（%s）"
                    "⇒ 丢弃本帧、不动缓存（不按空处理）",
                    api_key[:8], type(payload).__name__,
                )
                return

            # 本地兜底基准：内存缓存最近持仓（即"当前 N 个持仓"）
            old_hold = self._cache.get(api_key, {}).get("hold_data", [])
            old_rows = [h for h in old_hold if isinstance(h, dict)]
            # 缓存里若**已经**存着空 instId 的坏行（修复前被写进去的），就地清掉 ——
            # 否则它会被当成"好行"再推给前端（图标照样丢）。instId 是持仓行唯一的身份。
            bad_cached = [h for h in old_rows if not (h.get("instId") or "")]
            if bad_cached:
                old_rows = [h for h in old_rows if (h.get("instId") or "")]
                self._log_pos_fix(
                    api_key, "purged_empty_cache", "warning",
                    f"缓存中发现 {len(bad_cached)} 行空 instId（修复前写入的坏行），已清除并保留 "
                    f"{len(old_rows)} 行好行",
                )
            old_map = {(h.get("instId") or "", h.get("posSide") or ""): h
                       for h in old_rows}
            # posId → 旧行（旧行有正确的 instId）：空 instId 的推送行**只能**靠它找回合约。
            old_by_posid: Dict[str, dict] = {}
            for h in old_rows:
                _pid = h.get("posId")
                if _pid not in (None, "") and (h.get("instId") or ""):
                    old_by_posid.setdefault(str(_pid), h)

            def _num(v) -> float:
                """容错取数（原实现 ``float(p.get("pos","") or 0)`` 遇到非数字会整帧抛异常）。"""
                try:
                    return float(v or 0)
                except (TypeError, ValueError):
                    return 0.0

            # ── ① instId 回填（对所有交易所生效）──────────────────────────
            # 空 instId 的行参与合并会以键 ("", posSide) 顶掉好行 ("BTC-USDT-SWAP", posSide)。
            rows = [p for p in payload if isinstance(p, dict)] if isinstance(payload, list) else []
            fixed_by_posid = 0
            unresolved = []          # 仍无法确定 instId 的推送行 → 一律不进 merged
            normalized = []
            for p in rows:
                if p.get("instId"):
                    normalized.append(p)
                    continue
                src = (old_by_posid.get(str(p.get("posId")))
                       if p.get("posId") not in (None, "") else None)
                if src is not None:
                    p = dict(p)
                    p["instId"] = src.get("instId") or ""
                    if not p.get("symbol"):
                        p["symbol"] = p["instId"]
                    fixed_by_posid += 1
                    normalized.append(p)
                else:
                    unresolved.append(p)

            if fixed_by_posid:
                self._log_pos_fix(
                    api_key, "posid_recover", "info",
                    f"用 posId 从旧缓存找回 instId（本帧 {fixed_by_posid} 行）",
                )

            # 分离 WS 推送：存活(pos>0) 与 已平(pos<=0)
            active = [p for p in normalized if _num(p.get("pos")) > 0]
            closed_set = {(p.get("instId") or "", p.get("posSide") or "")
                          for p in normalized if _num(p.get("pos")) <= 0}

            if authoritative:
                # ★★ 来源标识：**我们自己的 REST 权威全量**（`_positions_poll_loop`，
                #    交易所 WS 帧不走这里）⇒ 日志一眼可辨，便于真机核对。
                logger.info(
                    "PrivateWs [%s] 持仓定时刷新（权威全量·REST）: payload %d 行 ⇒ 存活 %d 行、"
                    "已平 %d 行%s；缓存 %d 行 ⇒ **以 REST 为准**（空即空，不套用空快照防抖）",
                    api_key[:8], len(rows), len(active), len(closed_set),
                    f"、跳过 {len(unresolved)} 行无法确定 instId 的坏行" if unresolved else "",
                    len(old_rows),
                )

            if authoritative:
                # ⑤ **REST 权威全量**（我们主动拉取的全量，而非交易所推送）：
                #   · 以交易所当前持仓为准 ⇒ `merged = active`（存活行），**空就是空**。
                #     用户全平 ⇒ REST 返回 [] ⇒ 缓存清空 + `_diff_positions` 产出 close 事件
                #     ⇒ 前端左下角平仓通知 + 持仓行消失（本分支的存在理由）。
                #   · 无法确定 instId 的行（unresolved）**本就不在 active 里** ⇒ 绝不写入空 instId
                #     的坏行（这里只补一条日志，便于真机核对）。
                #   · ⚠️ 交易所 WS 路（`action="snapshot"`/`"update"`）**完全不经过本分支**
                #     ⇒ 空快照仍保留旧缓存、仍不产事件，行为一字不变 ✓。
                merged = list(active)
                if unresolved:
                    self._log_pos_fix(
                        api_key, "rest_poll_dropped_bad", "warning",
                        f"权威全量（REST）{len(unresolved)}/{len(rows)} 行 instId 为空且无法用 posId "
                        f"找回 ⇒ 跳过坏行、不写入空 instId 行；其余以 REST 为准，合并后 {len(merged)} 行",
                    )
            elif action == "snapshot":
                if not rows:
                    # ③ 空快照：Bitget 周期重连会重发 snapshot，空 payload 不代表持仓归零
                    #    ⇒ 保留旧缓存，真实变动由 REST（base_info 秒级轮询）校正。
                    merged = list(old_rows)
                    self._log_pos_fix(
                        api_key, "empty_snapshot", "warning",
                        f"收到空 snapshot（payload=[]），保留旧缓存 {len(old_rows)} 行、不清空",
                    )
                elif unresolved:
                    # ④ 有无法回填 instId 的行 ⇒ 该 snapshot **不可信**，退化为增量合并：
                    #    旧行兜底 - 剔除已平 + 合并存活。坏行一律跳过（保留旧数据）。
                    merged_map = {k: h for k, h in old_map.items() if k not in closed_set}
                    for p in active:
                        merged_map[(p.get("instId") or "", p.get("posSide") or "")] = p
                    merged = list(merged_map.values())
                    self._log_pos_fix(
                        api_key, "untrusted_snapshot", "warning",
                        f"snapshot 中 {len(unresolved)}/{len(rows)} 行 instId 为空且无法用 posId 找回，"
                        f"本帧按增量合并（保留旧缓存 {len(merged)} 行），不写入空 instId 的行",
                    )
                else:
                    # 全量且可信：以 active 为准直接替换（OKX 正常路径，行为不变）
                    merged = list(active)
            else:
                # 增量：本地兜底 - 剔除已平 + 合并存活（关键：不清零）
                merged_map = {}
                for k, h in old_map.items():
                    if k not in closed_set:
                        merged_map[k] = h
                for p in active:
                    merged_map[(p.get("instId") or "", p.get("posSide") or "")] = p
                merged = list(merged_map.values())
                if unresolved:
                    # ② 无法回填 instId 的增量行不写入（旧行已在兜底里保留）⇒ 不会多出空 symbol 行
                    self._log_pos_fix(
                        api_key, "dropped_empty_instid", "warning",
                        f"增量帧 {len(unresolved)}/{len(rows)} 行 instId 为空且无法用 posId 找回，"
                        f"跳过坏行、保留旧缓存 {len(merged)} 行",
                    )

            # 仓位动作识别：与「本帧合并前」的缓存基准比对（必须在 _update_cache 之前）。
            # 无基准时本帧只建立基准、不产事件，覆盖三种情况：
            #   ① 全新账户首次接入（json/data 快照缺失）；
            #   ② 进程重启（预填快照只作显示兜底、不建基准，见 _prefill_from_snapshot）；
            #   ③ 连接被移除后重新建立（_remove_connection 会清掉标记）。
            # 目的：不把「存量持仓」「停机期间早已发生的变动」误判为本帧实时动作。
            if self._pos_baseline.get(api_key):
                events = self._diff_positions(old_map, merged)
            else:
                events = []
                self._pos_baseline[api_key] = True

            self._update_cache(api_key, "hold_data", merged)
            # ★ 带上该账户所属交易所（不按 ticker 反查 —— 两所 443 个同名 instId）
            simplified = self._simplify_positions(
                merged, self._exchange_of_api_key(api_key), api_key)

            # 读取入场/离场（仅日志，不推送 — 避免与 orders_push 竞态）
            # 入场/离场数据由 _on_orders 独占推送，_on_positions 只推持仓
            entry_data, exit_data = self._read_entry_exit(api_key)
            push_payload = {
                "type": "positions_push",
                "api_key": api_key,
                "data": simplified,
            }
            # 动作事件挂在本帧持仓消息上同发（空则不带该字段，现有消费者可忽略）
            if events:
                push_payload["events"] = events

            logger.info(
                f"PrivateWs [{api_key[:8]}] positions_push: 持仓 {len(simplified)}个, "
                f"动作 {len(events)}条{self._format_events(events)}, "
                f"入场 {len(entry_data)}个(不入推), "
                f"离场 {len(exit_data)}个(不入推)"
            )
            await self._broadcast_to_frontend(api_key, push_payload)
        except Exception as e:
            logger.error(f"PrivateWs [{api_key[:8]}] 处理持仓异常: {e}", exc_info=True)

    def _log_pos_fix(self, api_key: str, reason: str, level: str, msg: str):
        """空 instId 兜底日志（**限流 60s/类**，避免 Bitget 重连风暴刷屏）。

        真机确认修复是否生效就看这几条：
          · ``posid_recover``          —— 用 posId 从旧缓存找回了 instId；
          · ``untrusted_snapshot``     —— snapshot 里有空 instId 行，已退化为增量合并（没顶掉好行）；
          · ``empty_snapshot``         —— 空 snapshot，保留旧缓存未清空；
          · ``dropped_empty_instid``   —— 增量帧里的空 instId 行被跳过、旧行保留；
          · ``rest_poll_dropped_bad``  —— **权威全量（REST 定时刷新）**里的空 instId 行被跳过
                                          （其余仍以 REST 为准，空即空）。
        同一 (api_key, 原因) 每 60s 最多一条，被折叠的条数在下次打印时一并给出。
        """
        key = (api_key, reason)
        now = time.time()
        if now - self._posfix_log.get(key, 0.0) < 60:
            self._posfix_skipped[key] = self._posfix_skipped.get(key, 0) + 1
            return
        folded = self._posfix_skipped.pop(key, 0)
        suffix = f"（此前 60s 内同类另有 {folded} 次已折叠）" if folded else ""
        text = f"PrivateWs [{api_key[:8]}] 持仓兜底[{reason}]: {msg}{suffix}"
        if str(level).lower() == "info":
            logger.info(text)
        else:
            logger.warning(text)
        self._posfix_log[key] = now

    @staticmethod
    def _diff_positions(old_map: dict, merged: list) -> list:
        """对比上一帧持仓缓存与本帧权威持仓，产出仓位动作事件（开仓/加仓/减仓/平仓）。

        判定基准：merged 是「本地兜底 + WS 增量」合并后的当前持仓——未被本帧推送的仓位
        保持旧值、被推送的为新值，因此 diff(old_map, merged) 恰好等价于「本帧真实变化」，
        无需再单独遍历 payload。

        以 (instId, posSide) 为 key、以 pos 数量为准判定（而非只比 key 存在性）：
        - 缓存无此 key           → open   开仓
        - 同 key 且 pos 变大      → add    加仓
        - 同 key 且 pos 变小未归零 → reduce 减仓
        - 同 key 且 pos 相同      → 跳过（浮盈/标记价波动帧，不产事件）
        - key 从 merged 消失      → close  平仓（merged 已剔 pos<=0，故必为真实平仓）

        cross + posSide 双向持仓模式下 pos 恒为正、方向由 posSide 表达，因此「反手」会自然
        拆成 close + open 两条事件，无需单独的 flip 分支。

        口径对齐：hold_data 缓存会混入逐仓仓位（_on_positions 未做 mgnMode 过滤），而预填来源
        swap_position_list 只保留全仓 cross。为避免两者口径不一致导致逐仓仓位被判为「开仓」，
        这里两侧统一排除明确的逐仓。字段缺失时按全仓处理，以防 WS payload 未携带 mgnMode 时
        事件全部静默失效（该情况在实盘被证实后会改为严格判定）。

        :param old_map: {(instId, posSide): 上一帧原始持仓条目}
        :param merged:  本帧合并后的权威持仓列表（精简前的原始条目）
        :return: 事件列表，元素含 instId / posSide / action / posBefore / posAfter
        """
        def _in_scope(item: dict) -> bool:
            # 只排除明确的逐仓；字段缺失时按全仓处理，避免 WS payload 未带 mgnMode 时
            # 事件全部静默失效。
            return (item.get("mgnMode") or "").lower() != "isolated"

        def _num(v) -> float:
            try:
                return float(v or 0)
            except (TypeError, ValueError):
                return 0.0

        new_map = {}
        for p in (merged if isinstance(merged, list) else []):
            if isinstance(p, dict) and p.get("instId") and _in_scope(p):
                new_map[(p.get("instId") or "", p.get("posSide") or "")] = p

        old_cross = {
            k: v for k, v in (old_map or {}).items()
            if isinstance(v, dict) and v.get("instId") and _in_scope(v)
        }

        events = []
        for key, new_item in new_map.items():
            new_sz = _num(new_item.get("pos"))
            if new_sz <= 0:
                continue
            old_item = old_cross.get(key)
            if old_item is None:
                action = "open"
            else:
                old_sz = _num(old_item.get("pos"))
                if new_sz > old_sz:
                    action = "add"
                elif new_sz < old_sz:
                    action = "reduce"
                else:
                    continue
            events.append({
                "instId": key[0],
                "posSide": key[1],
                "action": action,
                "posBefore": _num(old_item.get("pos")) if old_item else 0.0,
                "posAfter": new_sz,
            })

        for key, old_item in old_cross.items():
            if key in new_map:
                continue
            old_sz = _num(old_item.get("pos"))
            if old_sz <= 0:
                continue
            events.append({
                "instId": key[0],
                "posSide": key[1],
                "action": "close",
                "posBefore": old_sz,
                "posAfter": 0.0,
            })
        return events

    @staticmethod
    def _format_events(events: list) -> str:
        """把动作事件格式化成一行文本，仅供日志/调试阅读。"""
        if not events:
            return ""
        cn = {"open": "开仓", "add": "加仓", "reduce": "减仓", "close": "平仓"}
        parts = []
        for e in events:
            pos_side = e.get("posSide")
            direction = "多" if pos_side == "long" else ("空" if pos_side == "short" else (pos_side or ""))
            try:
                before, after = f"{float(e.get('posBefore')):g}", f"{float(e.get('posAfter')):g}"
            except (TypeError, ValueError):
                before, after = str(e.get("posBefore")), str(e.get("posAfter"))
            parts.append(
                f"{e.get('instId')}/{direction} {cn.get(e.get('action'), e.get('action'))} {before}→{after}"
            )
        return " [" + "; ".join(parts) + "]"

    async def _on_orders(self, api_key: str, payload: list, *, channel: str = ""):
        """处理订单推送：更新内存缓存并转发前端（有变化时才携带入场/离场数据）。

        orders-algo 是增量推送（一张一张推），所以必须按 algoId/ordId 合并而非替换。

        ★★ 2026-10-16（入场列表 `state=filled` 幽灵行）：**按频道分桶**，`channel` 由
          `_receive_loop` 传入（已翻译成内部口径 ✓）——
          `orders` ⇒ `limit_data`（按 `ordId` 合并）、`orders-algo` ⇒ `algo_data`（按 `algoId` 合并）。
          旧实现按「有无 `algoId`」分桶，而 `orders` 频道**会推带 `algoId` 的策略子单** ⇒
          子单按 `algoId` 覆盖父计划单（`effective`→`filled`）⇒ 幽灵行 ✗✗。
          频道未知 ⇒ **不分桶、记 WARNING 丢弃本帧**（绝不按有无 `algoId` 猜 ✗）。
        ⚠️ 也别改用 `ordType` 判断（历史 bug）：入场计划委托的 `ordType` 是 `trigger`、
          离场 OCO 是 `oco` ⇒ 会被误分进 limit 桶、并因缺 `ordId` 被 `_merge_by_key` 丢弃 ✗。
        """
        try:
            _ch = str(channel or "").strip().lower()
            bucket = _ORDER_BUCKET_BY_CHANNEL.get(_ch)
            if bucket is None:
                logger.warning(
                    "PrivateWs [%s] orders 帧频道未知（channel=%r）⇒ 丢弃本帧、不分桶"
                    "（按频道分流是唯一口径，绝不按有无 algoId 猜 ✗）",
                    api_key[:8], channel,
                )
                return
            # 同一帧只会来自**一个**频道 ⇒ 只进一个桶，另一个桶本帧无新数据 ✓
            _rows = [o for o in payload if isinstance(o, dict)] if isinstance(payload, list) else []
            if bucket == "algo_data":
                new_algo, new_limit = _rows, []
            else:
                new_algo, new_limit = [], _rows

            # 按 algoId/ordId 合并到现有缓存（增量推送不能直接覆盖）
            limit_data = self._merge_by_key(
                self._cache.get(api_key, {}).get("limit_data", []),
                new_limit,
                "ordId"
            )
            algo_data = self._merge_by_key(
                self._cache.get(api_key, {}).get("algo_data", []),
                new_algo,
                "algoId"
            )
            self._update_cache(api_key, "limit_data", limit_data)
            self._update_cache(api_key, "algo_data", algo_data)
            # ★★ 2026-11-01 用户裁定（原话）：
            #    「ws 就不应该参与对账，对账统一走数据循环的拉取」
            #    「它可以内存中整理，但不应该去处理交易所的任何数据」
            #   ⇒ 这里**只做内存合并**（`_merge_by_key` 按 algoId/ordId 覆盖，天然去重 ✓）
            #     ＋**推送显示**；**不查交易所、不剪任何落盘数据** ✗。
            #   「被替换/静默消失的旧单」由**数据循环**每轮用自己拉到的权威数据清理
            #   （`apply_in_memory_algo_rows`，见 `_data_worker`）。
            # ★ 同上：入场/离场单也要能标出所属所
            simplified = self._simplify_orders(
                payload, self._exchange_of_api_key(api_key), api_key)

            # 缓存已更新，重新计算入场/离场，有变化时附带推送
            entry_data, exit_data = self._read_entry_exit(api_key)
            push_payload = {
                "type": "orders_push",
                "api_key": api_key,
                "data": simplified,
            }

            cache = self._push_cache.get(api_key, {})

            # 有订单推送但 _read_entry_exit 过滤后为空（如全部 canceled）
            # 清除 hash 强制推送空数据，确保前端清理对应数据
            if payload:
                if not entry_data:
                    cache.pop("limit_hash", None)
                if not exit_data:
                    cache.pop("close_hash", None)

            new_limit_hash = self._hash_data(entry_data)
            new_close_hash = self._hash_data(exit_data)

            if new_limit_hash != cache.get("limit_hash"):
                push_payload["limit_data"] = entry_data
                cache["limit_hash"] = new_limit_hash
            if new_close_hash != cache.get("close_hash"):
                push_payload["close_data"] = exit_data
                cache["close_hash"] = new_close_hash
            self._push_cache[api_key] = cache

            logger.info(
                f"PrivateWs [{api_key[:8]}] orders_push: 订单 {len(simplified)}个(limit={len(limit_data)} algo={len(algo_data)}), "
                f"入场 {len(entry_data)}个({'变更' if 'limit_data' in push_payload else '不变'}), "
                f"离场 {len(exit_data)}个({'变更' if 'close_data' in push_payload else '不变'})"
            )
            # [TEMP DEBUG] 入场明细价格，定位「指纹不变」原因
            try:
                _edbg = [{k: it.get(k) for k in ("type", "algoId", "ordId", "px", "triggerPx", "ordPx", "sz", "state")} for it in entry_data]
                logger.info(f"PrivateWs [{api_key[:8]}] entry_data 调试: {_edbg}")
            except Exception as e:
                logger.error("[静默异常] _on_orders 失败: %s", e, exc_info=True)
            await self._broadcast_to_frontend(api_key, push_payload)
        except Exception as e:
            logger.error(f"PrivateWs [{api_key[:8]}] 处理订单异常: {e}", exc_info=True)

    def apply_in_memory_algo_rows(self, api_key: str, authoritative: list) -> dict:
        """用调用方（**数据循环**）传进来的权威列表，**只整理内存缓存**。

        ★★ 2026-11-01 用户裁定（逐字）：
           「ws 就不应该参与对账，对账统一走数据循环的拉取」
           「订阅的是 positions / orders / orders-algo **只负责看、转发、整理内存缓存中的数据**，
             **不应该修改本地的文件，以及交易所的数据**」
        ⇒ 本方法严格限定为**纯内存**：
          · **不发任何 REST**（交易所数据一律不碰 ✗）
          · **不写任何本地文件**（close_mode / 分账户快照的落盘由数据循环自己做 ✓）
          · 只把内存里的 `algo_data` 按权威列表剔旧 + 去重（`_merge_by_key` 之外的第二道 ✓）
        返回 `{"kept": n, "dropped": n}`；异常不外抛（不能让显示层拖累数据循环）。
        """
        from src.close_plan_reconcile import reconcile_algo_rows

        try:
            rows = [r for r in (authoritative or []) if isinstance(r, dict)]
            cached = self._cache.get(api_key, {}).get("algo_data", [])
            cached = cached if isinstance(cached, list) else []
            kept, dropped = reconcile_algo_rows(cached, rows, scope=api_key)
            self._update_cache(api_key, "algo_data", kept)
            return {"kept": len(kept), "dropped": len(cached) - len(kept)}
        except Exception as e:  # noqa: BLE001 —— 内存整理失败不影响推送
            logger.warning(
                "PrivateWs [%s] 内存整理策略单缓存失败（已忽略）: %s", api_key[:8], e, exc_info=True
            )
            return {"kept": 0, "dropped": 0}

    # ========================================================================
    # 缓存读取（入场/离场）
    # ========================================================================

    @staticmethod
    def _hash_data(data: list) -> str:
        """对数据列表做轻量变化指纹（含价格/数量/状态，排序后拼接），用于变化检测。

        必须包含价格/数量/状态字段：否则原地改价（amend，订单 ID 不变）不会改变指纹，
        limit_data/close_data 不会下发，前端价格会一直是旧值（本地 WS 不刷新）。
        """
        if not data:
            return ""

        def _attach_sig(d: dict) -> str:
            """附加止盈/止损（限价单的 tp/sl 在 attachAlgoOrds 内）也纳入指纹。"""
            arr = d.get("attachAlgoOrds")
            if not isinstance(arr, list):
                return ""
            return ",".join(
                f"{a.get('tpTriggerPx', '')}/{a.get('slTriggerPx', '')}/"
                f"{a.get('tpOrdPx', '')}/{a.get('slOrdPx', '')}"
                for a in arr if isinstance(a, dict)
            )

        def _sig(d: dict) -> str:
            oid = d.get("ordId") or d.get("algoId") or d.get("clOrdId") or ""
            fields = (
                oid, d.get("px"), d.get("sz"), d.get("state"), d.get("accFillSz"),
                d.get("triggerPx"), d.get("orderPx"), d.get("closeFraction"),
                d.get("tpTriggerPx"), d.get("slTriggerPx"),
                d.get("callbackSpread"), d.get("callbackRatio"), d.get("activePx"),
            )
            return "|".join(str(x or "") for x in fields) + "|" + _attach_sig(d)

        return "||".join(sorted(_sig(d) for d in data if isinstance(d, dict)))

    @staticmethod
    def _merge_by_key(existing: list, incoming: list, key: str) -> list:
        """按指定 key 合并订单列表，incoming 覆盖 existing 中同 key 的条目。

        orders-algo 是增量推送（每次只推变化的订单），不能直接整体替换。
        """
        if not isinstance(existing, list):
            existing = []
        merged = {d.get(key): d for d in existing if isinstance(d, dict) and d.get(key)}
        for d in (incoming if isinstance(incoming, list) else []):
            if isinstance(d, dict) and d.get(key):
                merged[d[key]] = d
        return list(merged.values())

    @staticmethod
    def _classify_entry_exit(limit_data: list, algo_data: list):
        """从订单列表分类出入场单与离场单。

        分类规则与 base_service.py 保持一致：
        - 入场：limit_data 中 side 匹配开仓方向 + algo_data 中 side 匹配开仓方向
        - 离场：algo_data 中 side 匹配平仓方向
        - 均过滤 tdMode != "cross"（仅全仓模式）
        - limit 行过滤非活跃 state（canceled/filled）；algo 行过滤 `_ALGO_TERMINAL_STATES`
          （canceled/effective/order_failed/partially_failed/filled）✓

        ★★ 2026-10-06（前端复测 H6「入场表跨账号串号 / 自己的单消失」）：
          Bitget 的**纯触发计划委托**行**不返回方向**（真机 27 个键里 `posSide`/`side`/
          `holdSide`/`tradeSide` **全都没有**，见缺陷 #8）⇒ 旧实现在这里 `pos_side and …`
          判空 ⇒ **整类被丢弃** ⇒ 该账户"入场 0 个"（真机日志实证：`limit=49 algo=39, 入场 0个`），
          前端表格里剩下的就只有**别的账号**（有方向的 OKX）的行 ✗✗ —— 用户看到的就是"串号"。
          ⇒ 新口径：**方向未知（缺 posSide/side）的活跃行一律保留在「入场」**（并打 WARNING），
            宁可让用户看见自己的单（可撤、可查），也**绝不静默隐藏** ✓。
            已知方向的行分类**一字不变** ✓（离场仍严格按平仓方向）。

        注意：WS 仅订阅 SWAP 频道，现货数据不会进入本缓存，现货展示由 REST 轮询负责。
        """
        # 开仓方向映射: long → buy, short → sell
        # 平仓方向映射: long → sell, short → buy
        open_side = {"long": "buy", "short": "sell"}
        close_side = {"long": "sell", "short": "buy"}

        # 入场 = limit单(入场方向) + algo单(入场方向)
        entry = []
        for o in (limit_data if isinstance(limit_data, list) else []):
            pos_side = o.get("posSide", "")
            side = o.get("side", "")
            # ⚠️ **不能写死 `!= "cross"`**（真机 2026-10-03）：
            #    Bitget 的策略委托常常**不返回**保证金模式（tdMode/mgnMode/marginMode 全空），
            #    写死比较会把所有合法离场单都过滤掉 ⇒ 前端「离场」永远为空。
            #    且 Bitget 的写法是 `crossed`（不是 OKX 的 `cross`）。
            #    ⇒ 只在**明确**是逐仓时过滤，空值保留。
            _td = str(o.get("tdMode") or o.get("mgnMode") or o.get("marginMode") or "").strip().lower()
            state = o.get("state", "")
            if _td and _td not in ("cross", "crossed"):
                continue
            if state in ("canceled", "filled"):
                continue
            if pos_side and side and side == open_side.get(pos_side):
                item = {"type": "limit", **o}
                item["symbol"] = o.get("instId", "")
                entry.append(item)
            elif not pos_side or not side:
                # ★ 方向未知（历史单/交易所不返回）⇒ 保留在入场并标记，绝不静默隐藏 ✓
                item = {"type": "limit", **o, "_side_unknown": True}
                item["symbol"] = o.get("instId", "")
                entry.append(item)
                logger.warning(
                    "[ENTRY-CLASSIFY] 限价/挂单行缺少方向（posSide=%r side=%r instId=%s ordId=%s）"
                    "⇒ 保留在「入场」并标记 _side_unknown（不隐藏用户自己的单 ✓）",
                    pos_side, side, o.get("instId"), o.get("ordId"),
                )
        for o in (algo_data if isinstance(algo_data, list) else []):
            pos_side = o.get("posSide", "")
            side = o.get("side", "")
            # ⚠️ **不能写死 `!= "cross"`**（真机 2026-10-03）：
            #    Bitget 的策略委托常常**不返回**保证金模式（tdMode/mgnMode/marginMode 全空），
            #    写死比较会把所有合法离场单都过滤掉 ⇒ 前端「离场」永远为空。
            #    且 Bitget 的写法是 `crossed`（不是 OKX 的 `cross`）。
            #    ⇒ 只在**明确**是逐仓时过滤，空值保留。
            _td = str(o.get("tdMode") or o.get("mgnMode") or o.get("marginMode") or "").strip().lower()
            state = o.get("state", "")
            if _td and _td not in ("cross", "crossed"):
                continue
            if state in _ALGO_TERMINAL_STATES:
                continue
            if pos_side and side and side == open_side.get(pos_side):
                item = {"type": "algo", **o}
                item["symbol"] = o.get("instId", "")
                entry.append(item)
            elif not pos_side or not side:
                # ★★ 纯触发计划委托（Bitget）就是这一类 ⇒ **必须保留**（否则用户看不见自己的单 ✗✗）
                item = {"type": "algo", **o, "_side_unknown": True}
                item["symbol"] = o.get("instId", "")
                entry.append(item)
                logger.warning(
                    "[ENTRY-CLASSIFY] 计划委托行缺少方向（posSide=%r side=%r instId=%s algoId=%s）"
                    "⇒ 保留在「入场」并标记 _side_unknown（欠缺陷 #8 记录册时会走到这里 ✓）",
                    pos_side, side, o.get("instId"), o.get("algoId"),
                )

        # 离场 = algo单(平仓方向)
        exit_data = []
        for o in (algo_data if isinstance(algo_data, list) else []):
            pos_side = o.get("posSide", "")
            side = o.get("side", "")
            # ⚠️ **不能写死 `!= "cross"`**（真机 2026-10-03）：
            #    Bitget 的策略委托常常**不返回**保证金模式（tdMode/mgnMode/marginMode 全空），
            #    写死比较会把所有合法离场单都过滤掉 ⇒ 前端「离场」永远为空。
            #    且 Bitget 的写法是 `crossed`（不是 OKX 的 `cross`）。
            #    ⇒ 只在**明确**是逐仓时过滤，空值保留。
            _td = str(o.get("tdMode") or o.get("mgnMode") or o.get("marginMode") or "").strip().lower()
            state = o.get("state", "")
            if _td and _td not in ("cross", "crossed"):
                continue
            if state in _ALGO_TERMINAL_STATES:
                continue
            if pos_side and side and side == close_side.get(pos_side):
                item = dict(o)
                item["symbol"] = o.get("instId", "")
                exit_data.append(item)

        return entry, exit_data

    @staticmethod
    def _fill_sides_from_store(api_key: str, rows):
        """用**我们自己的下单记录册**回填缺失方向（缺陷 #8 的**推送侧**）。

        为什么需要：Bitget 纯触发计划委托行不带 `posSide`/`side` ⇒ 分类器无从判断
        入场/离场（旧实现直接丢弃 ⇒ 用户"自己的单看不见"，前端复测 H6）。
        REST 列表路径已由适配层回填 ✓；推送走的是 WS 帧/进程内缓存 ⇒ 这里补一次。
        **只在字段为空时填、查不到就留空**（不编造 ✓），失败只 WARNING（不挡推送 ✓）。
        """
        try:
            from exchange import algo_side_store
        except Exception:                                            # noqa: BLE001
            return
        for r in (rows if isinstance(rows, list) else []):
            if not isinstance(r, dict) or (r.get("posSide") and r.get("side")):
                continue
            oid = r.get("algoId") or r.get("ordId") or r.get("orderId")
            if not oid:
                continue
            rec = algo_side_store.lookup(api_key, str(oid))
            if not rec:
                continue
            if not r.get("posSide") and rec.get("posSide"):
                r["posSide"] = rec["posSide"]
            if not r.get("side") and rec.get("side"):
                r["side"] = rec["side"]

    def _classify_with_sides(self, api_key: str, limit_data, algo_data):
        """**分类的唯一入口**：先按记录册回填方向，再分类。

        ⚠️ 为什么必须收成一个入口（2026-10-06 真机踩到）：两条路径都要分类 ——
          ① 实时推送 `_read_entry_exit`（订单事件）
          ② **前端连接时的初始快照** `push_initial_snapshot_to_client`（即"刷新页面"）
        我只在 ① 加了回填 ⇒ 用户**刷新页面**时 ② 仍按无方向分类 ⇒ 方向变回 `--`
        （真机日志实证：22:19:32 仍在为已回填的两笔打 `ENTRY-CLASSIFY` 告警 ✗）。
        ⇒ 收成这一个方法，两条路径共用 ⇒ 不会再漏 ✓。
        """
        self._fill_sides_from_store(api_key, limit_data)
        self._fill_sides_from_store(api_key, algo_data)
        return self._classify_entry_exit(limit_data, algo_data)

    def _read_entry_exit(self, api_key: str):
        """从进程内内存缓存读取该账户的入场单和离场单。"""
        try:
            account = self._cache.get(api_key, {})
            if not isinstance(account, dict):
                return [], []
            return self._classify_with_sides(
                api_key, account.get("limit_data", []), account.get("algo_data", []))
        except Exception as e:
            logger.error("PrivateWs 读取入场/离场异常: %s", e, exc_info=True)
            return [], []

    # ========================================================================
    # 缓存更新
    # ========================================================================

    def _update_cache(self, api_key: str, field: str, data: list):
        """更新进程内内存缓存中的指定字段（不写文件）。

        WS 只负责接收、计算、转发，不承担持久化职责。
        对 hold_data 的每条记录补充 symbol 字段（= instId），
        保持与 bot_utils.swap_position_list 的数据格式一致。

        ⚠️ **每条记录都要带上所属交易所**（2026-10-03）：
           入场 / 离场（`limit_data` / `algo_data`）的推送数据是**从本缓存派生**的
           （`_read_entry_exit` → `_classify_entry_exit` 用 `{**o}` 展开），
           缓存里没有 exchange 时前端拿不到所属所 ⇒ 两所同名 instId
           （真机 443 个）下会**串标/串价**。
           这里统一注入，同时也让「连上前端时的入场/离场快照」
           （`push_initial_snapshot_to_client`）带上所。
        """
        try:
            if api_key not in self._cache:
                self._cache[api_key] = {}
            _ex = self._exchange_of_api_key(api_key)
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    # 给 hold_data 补充 symbol 字段
                    if field == "hold_data" and not item.get("symbol"):
                        item["symbol"] = item.get("instId", "")
                    # ★★ 所有字段（hold_data / limit_data / algo_data）统一补 exchange
                    if _ex and not item.get("exchange"):
                        item["exchange"] = _ex
            self._cache[api_key][field] = data
        except Exception as e:
            logger.error("PrivateWs 更新内存缓存.%s 异常: %s", field, e, exc_info=True)

    # ========================================================================
    # 前端推送
    # ========================================================================

    async def _broadcast_to_frontend(self, api_key: str, payload: dict):
        """通过 WebSocketManager 广播给所有前端客户端。"""
        if not self.ws_manager:
            return
        try:
            await self.ws_manager.broadcast(payload)
            logger.info(f"PrivateWs [{api_key[:8]}] 广播: {payload.get('type')} {len(payload.get('data',[]))}条")
        except Exception as e:
            logger.info(f"PrivateWs [{api_key[:8]}] 广播异常: {e}")

    async def push_initial_snapshot_to_client(self, ws_id: str):
        """前端 WS 连接建立后，读取 json/data 快照并定向推送初始兜底数据。

        背景：OKX positions/orders/orders-algo 为增量推送（只在变化时推），后端与 OKX 建连
        时前端可能尚未连上后端 WS。若只依赖 _broadcast（发给当时已连接客户端），前端连接
        后收不到任何兜底数据，直到 OKX 推送下一次增量。改为在前端 auth_success 注册进
        ws_manager.clients 后，读取 json/data 分账户快照，定向发送持仓/入场/离场给该连接，
        之后由实时推送 _on_positions/_on_orders 覆盖，数据不乱、不依赖 OKX 主动推存量。
        """
        try:
            if not self.ws_manager:
                return
            api_list = self._get_api_list()
            for api_info in (api_list if isinstance(api_list, list) else []):
                if not isinstance(api_info, dict):
                    continue
                api_key = api_info.get("api_key")
                if not api_key:
                    continue
                # ★★ 2026-10-05（用户报告「**刷新后前端还是多出来一堆离场**」）：
                #    本函数读的就是 `json/data/{api_key}.json` 快照 ⇒ 若快照里积了
                #    陈旧/重复的 algo 行（真机现场：2 笔被重复成 **6 行** ✗），
                #    每次前端连接/刷新都会被**原样重喂**一遍 ✗✗。
                #    ⇒ 推送前先用**权威 REST 列表**（GET 只读）对账缓存 + 计划表 + 快照；
                #      未命中能力表（如 OKX）⇒ 立即返回，行为一字不变 ✓；
                #      对账失败 ⇒ **不剪**（保留旧数据，不猜 ✓）。
                # ★ 2026-11-01 用户裁定：「ws 就不应该参与对账」⇒ 前端连接时**不再**跑对账，
                #   直接按分账户快照推送（快照由数据循环每轮刷新 + 对账 ✓）。
                snapshot = read_api_json(api_key)
                if not isinstance(snapshot, dict):
                    continue
                # 持仓兜底
                hold_data = snapshot.get("hold_data")
                simplified = None
                if isinstance(hold_data, list) and hold_data:
                    simplified = self._simplify_positions(
                        hold_data, self._exchange_of_api_key(api_key), api_key)
                    if simplified:
                        await self.ws_manager.send_to_client(
                            ws_id,
                            {"type": "positions_push", "api_key": api_key, "data": simplified},
                        )
                # 入场/离场兜底：直接由快照的 limit_data/algo_data 分类，不依赖进程内缓存
                #   ★ 必须走 `_classify_with_sides`（含记录册方向回填）—— 否则"刷新页面"
                #     这条路径会把方向丢掉（真机 2026-10-06 实证 ✗）
                entry_data, exit_data = self._classify_with_sides(
                    api_key, snapshot.get("limit_data"), snapshot.get("algo_data")
                )
                if entry_data or exit_data:
                    push_payload = {
                        "type": "orders_push",
                        "api_key": api_key,
                        "data": self._simplify_orders([], self._exchange_of_api_key(api_key), api_key),
                    }
                    if entry_data:
                        push_payload["limit_data"] = entry_data
                    if exit_data:
                        push_payload["close_data"] = exit_data
                    await self.ws_manager.send_to_client(ws_id, push_payload)
                logger.info(
                    f"PrivateWs [{api_key[:8]}] 前端连接兜底快照推送: "
                    f"持仓 {len(simplified or [])}个 入场 {len(entry_data)}个 离场 {len(exit_data)}个"
                )
        except Exception as e:
            logger.error("PrivateWs 前端连接推送兜底快照异常: %s", e, exc_info=True)

    # ========================================================================
    # 数据精简（减少推送体积）
    # ========================================================================

    @staticmethod
    def _exchange_of_api_key(api_key: str) -> str:
        """``api_key`` → 该账户所属交易所。

        ⚠️ 交易所是**账户的属性**（ADR-5 / 后端 ㉝）：两所的 ``instId`` 可以同名
        （真机实测 okx 与 bitget 有 **443** 个同名合约），**绝不能按 ticker 反查**。
        查不到返回空串（**不兜底成默认所** —— 前端据此不渲染标，暴露缺口）。
        """
        try:
            for item in (FileOperations().read_file_info("api_list") or []):
                if isinstance(item, dict) and str(item.get("api_key") or "") == str(api_key or ""):
                    return str(item.get("exchange") or "")
        except Exception as e:
            logger.warning("_exchange_of_api_key 读取 api_list 失败: %s", e, exc_info=True)
        return ""

    @staticmethod
    def _stamp_private(items: list, api_key: str, exchange: str) -> list:
        """给**私有数据**（持仓/订单/入场/离场）每条补身份。

        ⚠️ 补身份的规则（2026-10-03 用户裁定）—— **按数据性质区分**：

          · **行情类**（K 线 / ticker / 自选价）**只需 `exchange`**
            —— 行情是"**所的属性**"：同一交易所的所有账户看到的是同一份行情。
          · **私有类**（持仓 / 订单 / 入场 / 离场）**必须 `exchange` + `api_key`**
            —— 私有数据是"**账户的属性**"：同一交易所的不同账户各不相同
              （本机 okx 就有 5 个账户）。

        这就是"为什么持仓推送要带 api_key、而 K 线推送不需要"的原因。
        已有值不覆盖（交易所侧给了就以它为准）。
        """
        for it in items or []:
            if not isinstance(it, dict):
                continue
            if exchange and not it.get("exchange"):
                it["exchange"] = exchange
            if api_key and not it.get("api_key"):
                it["api_key"] = api_key
        return items

    @staticmethod
    def _simplify_positions(raw: list, exchange: str = "", api_key: str = "") -> list:
        """精简持仓字段，只保留前端需要的。"""
        # 注意：这里是 WS 推送路径的白名单，与 services/base_service.py 的 hold_data 白名单要保持一致，
        # 否则同一列会在"接口拉取"时正常、"WS 推一次"后变回空（字段被简化掉）。
        #
        # ★★ `exchange` 必须带上（2026-10-03 真机 bug）：
        #   持仓行上的交易所**只能由账户决定**。这里是 WS 推送路径 —— 漏了它，
        #   前端拿到的持仓行没有 exchange，只能按 ticker 反查 symbol_list，
        #   而两所 443 个同名 instId ⇒ **bitget 的持仓被标成 OKX**。
        #   ⚠️ 本函数的 raw（WS payload / 本地缓存）里**没有** exchange，
        #   所以由调用方按 api_key 查好传进来，这里只负责写进记录。
        keep = ["instId", "posSide", "pos", "avgPx", "upl", "uplRatio",
                "closeOrderAlgo",  # ★ 2026-10-26：持仓自带离场委托（保本标用，避免 orders_push 时序缺失 ✓）
                "markPx", "last", "liqPx", "lever", "margin", "imr", "bePx", "cTime",
                "notionalUsd"]
        result = []
        for p in raw:
            item = {k: p.get(k, "") for k in keep if k in p}
            item["symbol"] = p.get("instId", "")
            # 以账户所属为准；若 payload 自带 exchange（交易所侧给了）则以它为准
            item["exchange"] = p.get("exchange") or exchange or ""
            # ★ 2026-10-26：保本标（后端权威判定 ✓，前端只认布尔值）
            try:
                from src.breakeven import breakeven_flag
                item["breakeven"] = breakeven_flag(p)
            except Exception:
                item["breakeven"] = False
            result.append(item)
        # ★ 私有数据必须带 api_key（见 _stamp_private 的规则说明）
        return PrivateWsManager._stamp_private(result, api_key, exchange)

    @staticmethod
    def _simplify_orders(raw: list, exchange: str = "", api_key: str = "") -> list:
        """精简订单字段（入场/离场单）。"""
        # ★ 与 _simplify_positions 同理：入场/离场行也要能标出所属所，
        #   否则前端计划面板的「入场」「离场」页签会把 bitget 的单标成 OKX。
        keep = ["instId", "ordId", "clOrdId", "algoId", "posSide", "side",
                "sz", "px", "ordType", "state", "cTime", "triggerPx",
                "tpTriggerPx", "slTriggerPx", "lever"]
        result = []
        for o in raw:
            item = {k: o.get(k, "") for k in keep if k in o}
            item["symbol"] = o.get("instId", "")
            item["exchange"] = o.get("exchange") or exchange or ""
            result.append(item)
        # ★ 私有数据必须带 api_key（见 _stamp_private 的规则说明）
        return PrivateWsManager._stamp_private(result, api_key, exchange)

    # ========================================================================
    # 工具
    # ========================================================================

    def _get_api_list(self) -> list:
        """获取当前 API 列表。"""
        try:
            return FileOperations().read_file_info("api_list") or []
        except Exception as e:
            logger.warning("API 列表读取失败: %s", e, exc_info=True)
            return []

    def _get_sys_info(self) -> dict:
        """获取系统设置。"""
        try:
            sys_set = FileOperations().read_file_info("sys_set")
            return sys_set[0] if sys_set else {}
        except Exception as e:
            logger.warning("系统设置读取失败: %s", e, exc_info=True)
            return {}


# ========================================================================
# 单例
# ========================================================================

_private_ws_instance: Optional[PrivateWsManager] = None


def get_private_ws_manager(ws_manager=None) -> PrivateWsManager:
    """获取全局 PrivateWsManager 实例。"""
    global _private_ws_instance
    if _private_ws_instance is None:
        _private_ws_instance = PrivateWsManager(ws_manager)
    return _private_ws_instance
