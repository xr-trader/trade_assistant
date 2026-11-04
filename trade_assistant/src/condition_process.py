# -*- coding: utf-8 -*-
# cython: language_level=3
"""
条件触发监控进程：WS 订阅 tickers → 价格满足 condition → 分发到对应动作执行。

跨进程同步机制（与 shadow_sl 的 configs.xlsx mtime 模式一致）：
  - 计划写入 configs/condition.xlsx（backend 的 set/cancel、condition_mode 清理）
  - 本进程 1s tick 检测文件 mtime，变化则全量重载 + 订阅 diff
  - 触发状态更新写回同一文件（atomic_update 锁内原子，防并发丢更新）

触发判定说明：参考价由 calc_trigger_price 动态计算（source=price 用固定价，
ma/trendline 每轮拉 K 线算动态价），再与 WS 推送的最新价比较 condition 是否穿越。

WS 限速说明：公共行情频道服务端推送，接收不计入限速；订阅/退订请求
每连接 480 次/小时，本进程仅在计划变化时重发订阅，用量极小。
"""

import sys; sys.path.insert(0, ".")  # 入口脚本需将项目根目录加入 sys.path（同其他 worker）
import asyncio
import json
import os
import time

import websockets

from exchange.endpoint_registry import ws_endpoint
from exchange.capabilities import to_ws_symbol as _to_ws_symbol, to_internal_symbol as _to_internal_symbol

from src.plan_exchange import row_exchange as _row_ex
from src.condition import (
    CONDITION_FILE,
    CONDITION_PATH,
    PLAN_TYPE_TRIGGER,
    SOURCE_PRICE,
    SOURCE_MA,
    SOURCE_TRENDLINE,
    auto_fill_condition,
    execute_trigger,
    _fmt_price,
    _notify,
    _read_plans,
    _remove_plan,
    _safe_loads,
    _tidy_price,
)
from src.symbol_key import bind_exchange
from src.condition_mode import calc_trigger_price
from src.chain_fields import activate_chain
from src.plan_err_mark import (
    ERR_ACCOUNT_UNUSABLE,
    ERR_FIELD_MISSING,
    ERR_PRICE_UNAVAILABLE,
    clear_err,
    mark_err,
)
from src.read_edit import FileOperations
from src.utils_tool import Tool
from src.emergency_stop import check_and_notify
from src.user_logger import get_logger

logger = get_logger()
# ★ 2026-10-05：原来这里有 `CONDITION_EXCHANGE = "okx"` + 两个 okx 快照 URL ✗
#   ⇒ 本进程**只连 okx**（Bitget 条件单不会触发 ✗）。按所拆连接见功能对齐计划表；
#   本文件内 WS 地址一律走 `_ws_url(exchange)` ✓（不再保留任何 okx 快照常量）。


def _execute_account_action(proc, plan):
    """触发链 · 账户动作命中 ⇒ 市价平仓（一次性 ✓）。

    · 作用域由 `condition_account_action.select_targets` 统一算（与 ConditionClean 同源 ✓）；
    · 复用 `orders.manage.close_all_positions`（含两所限速/退避/逐条汇总 ✓）；
    · 无目标 ⇒ 通知 + 删行；成功/部分失败/执行异常 ⇒ 一次性删行（不重试市价单 ✓）；
    · 取持仓失败/拿不到 bot ⇒ **保留行**，下一轮重试（与激活失败同口径 ✓）。
    """
    from src.condition_account_action import (
        close_side_for, format_trigger_notice, select_targets,
    )

    act = str((plan or {}).get("action_type") or "").strip().lower()
    symbol = str((plan or {}).get("symbol") or "")
    pid = (plan or {}).get("id")
    api_key = str((plan or {}).get("api_key") or "")
    bot = proc._get_bot(api_key)
    if bot is None:
        logger.error("触发链执行失败：拿不到交易实例 api_key=%s（保留重试）", api_key)
        proc._plans.append(plan)
        return
    try:
        pos_res = bot.accountAPI.custom_actions({"instType": "SWAP"}, "get_positions")
    except Exception as e:  # noqa: BLE001
        logger.error("触发链取持仓异常（保留重试）: %s", e, exc_info=True)
        proc._plans.append(plan)
        return
    if str(pos_res.get("code")) != "0":
        logger.error(
            "触发链取持仓失败（保留重试）: %s",
            pos_res.get("msg") or pos_res.get("code"),
        )
        proc._plans.append(plan)
        return
    positions = pos_res.get("data") or []
    targets = (plan.get("action_args") or {}).get("targets") or []
    if not select_targets(act, targets, positions):
        _notify(format_trigger_notice(
            symbol, act, "未执行（无可平仓位）", "账户当前无匹配持仓",
        ), plan=plan)
        _remove_plan(pid, reason="触发链命中但无可平仓位", reason_code="trigger_no_position")
        return

    from src.orders.manage import close_all_positions

    payload = {"close_side": close_side_for(act)}
    if act == "close_symbols":
        payload["targets"] = targets
    res = close_all_positions(bot, payload)
    ok = str(res.get("status") or "") == "success"
    _data = res.get("data") or {}
    _closed = _data.get("closed") or []
    _failed = _data.get("failed") or []
    _handling = (
        f"已平仓 {len(_closed)} 个"
        if ok
        else f"部分成功（成功 {len(_closed)} / 失败 {len(_failed)}）"
    )
    _notify(format_trigger_notice(symbol, act, _handling, "触发条件命中"), plan=plan)
    try:
        from src.audit_log import record_operation_log

        record_operation_log(
            "condition", plan, res,
            action=f"trigger_{act}", reason=f"触发链命中（{_handling}）",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("触发链审计写入失败: %s", e)
    if not ok:
        logger.error("触发链执行未全部成功（一次性删除，不重试）: %s", res.get("msg"))
    _remove_plan(pid, reason="触发链已执行（一次性）", reason_code="trigger_fired")


class ConditionProcess(object):
    """条件触发监控进程（独立进程，由 service.sh 管理）。"""

    def __init__(self):
        self._file_mtime = None
        self._plans = []        # active 计划缓存（重载时刷新，已归一化嵌套参数）
        self._symbols = set()   # 需订阅的 symbol 集合
        # ★ 2026-10-05：**按所拆 WS**（原来只连 okx ✗）；⚠️ 两所 instId 同名(443) ⇒ 必须按所隔离防跨所误触发 ✗
        self._symbols_by_ex = {}      # exchange → set(symbol)
        self._ws_by_ex = {}           # exchange → ws
        self._tasks_by_ex = {}        # exchange → Task（每所一条连接）
        self._sub_serial_by_ex = {}   # exchange → 订阅版本号
        self._sent_serial_by_ex = {}  # exchange → 已发送版本号
        self._last_prices = {}  # symbol → 最近一次推送的 last
        self._bots = {}         # api_key → 交易实例缓存（拉 K 线用）
        self._ws = None
        self._sub_serial = 0    # 订阅版本号，变化时重发订阅
        self._sent_serial = -1  # 已发送的订阅版本号
        self._stopped = False   # 紧急停机标记（tick 循环每 1s 刷新）

    # ── 工具 ──
    def _get_bot(self, api_key):
        """按 api_key 惰性创建交易实例并缓存。

        ⚠️ 两条返回路径都走 `bind_exchange`：**缓存命中时也必须绑定**交易所上下文，
        否则多账户交替处理计划时，B 的 bot 建好后 A 命中缓存 → 上下文仍是 B，
        下游 `tidy_price`/`tidy_sz` 会用错交易所的精度（ADR-8）。
        """
        if not api_key:
            return None
        if api_key in self._bots:
            return bind_exchange(self._bots[api_key])
        try:
            bot = Tool().create_bot(api_key)
        except Exception as e:
            logger.error("条件触发创建交易实例失败: %s", e, exc_info=True)
            return None
        self._bots[api_key] = bot
        return bind_exchange(bot)

    @staticmethod
    def _decode_plan(plan):
        """归一化计划：把 src_params/action_args/ref_price 从字符串转为 dict/float。"""
        plan["src_params"] = _safe_loads(plan.get("src_params", {}), {}) or {}
        plan["action_args"] = _safe_loads(plan.get("action_args", {}), {}) or {}
        return plan

    # ── 计划重载 ──
    def _reload_plans_if_changed(self):
        """mtime 变化则全量重载，返回是否有订阅集合变化。"""
        try:
            mtime = os.path.getmtime(CONDITION_PATH)
        except OSError:
            return False
        if mtime == self._file_mtime:
            return False
        self._file_mtime = mtime
        plans = _read_plans()
        # ★ 2026-10-20：status=limit 是挂单等待计划 ⇒ WS 不监控；
        #   其余（含 status 为空的老行）正常监控。status 变化会改 mtime ⇒ 下一轮自动重载。
        waiting = [
            p for p in plans
            if str(p.get("status") or "").strip().lower() == "limit"
        ]
        # ★★ 2026-10-25（用户裁定）：「只要方向是空的，就用现价与目标价格自动判断」
        #   ⇒ `plan_type=trigger` 的行**照常载入**（要补方向 ✓），但**不执行激活**（P2 再接 ✓）。
        self._plans = [
            self._decode_plan(p) for p in plans
            if str(p.get("status") or "").strip().lower() != "limit"
        ]
        new_by_ex = {}
        for _p in self._plans:
            _sym = _p.get("symbol") or ""
            if not _sym:
                continue
            _ex = str(_row_ex(_p) or "").strip().lower()
            if not _ex:
                logger.error("条件触发计划缺 exchange ⇒ 跳过（不猜，不订阅）: %s", _sym)
                continue
            new_by_ex.setdefault(_ex, set()).add(_sym)
        new_symbols = {_s for _v in new_by_ex.values() for _s in _v}
        if new_by_ex != getattr(self, "_symbols_by_ex", None) or new_symbols != self._symbols:
            self._symbols_by_ex = new_by_ex
            self._symbols = new_symbols
            for _ex in new_by_ex:
                self._sub_serial_by_ex[_ex] = self._sub_serial_by_ex.get(_ex, 0) + 1
            logger.info("条件触发计划重载: %d 条 active / %d 条 limit 等待，按所订阅 %s",
                        len(self._plans), len(waiting),
                        {k: len(v) for k, v in sorted(new_by_ex.items())})
            return True
        return False

    # ── WS 收发 ──
    def _ws_url(self, exchange="okx"):   # ★ 按所解析（默认 okx = 改造前的行为）
        """公共行情 WS 地址（按本进程的所解析）。

        ★ 固定走**实盘公共行情 WS**（``demo=False``）—— 用户 2026-10-05 裁定：
        公共行情必须与图表/自选看到的一致，不能用模拟环境的行情做触发判定；
        且主用数据源固定为**非模拟盘账户**（``is_data_api``，flag != "1" ⇒ 实盘 key）后，
        WS 域名不再随任何权限开关漂移。
        """
        return ws_endpoint(exchange, "public", demo=False)

    async def _send_subscriptions(self, ws, exchange):
        """发送当前订阅集合（tickers 频道，分批 ≤60KB/条）。"""
        symbols = sorted(self._symbols_by_ex.get(exchange, ()))
        if not symbols:
            return
        # ★ 2026-10-05：订阅报文必须用**该所**的 WS 拼法（Bitget 要 BTCUSDT ✗ 不能给 BTC-USDT-SWAP）
        args = [{"channel": "tickers", "instId": _to_ws_symbol(exchange, s)} for s in symbols]
        batch, size = [], 0
        for arg in args:
            item = json.dumps(arg)
            if size + len(item) > 60000 and batch:
                await ws.send(json.dumps({"op": "subscribe", "args": batch}))
                batch, size = [], 0
            batch.append(arg)
            size += len(item)
        if batch:
            await ws.send(json.dumps({"op": "subscribe", "args": batch}))

    async def _ws_loop(self, exchange):
        """WS 连接 + 收消息循环（断线 5s 后自动重连，重连后全量重订阅）。"""
        while True:
            try:
                url = self._ws_url(exchange)
                async with websockets.connect(
                    url, ping_interval=25, ping_timeout=20, max_size=2**22
                ) as ws:
                    self._ws_by_ex[exchange] = ws
                    self._sub_serial_by_ex[exchange] = self._sub_serial_by_ex.get(exchange, 0) + 1
                    logger.info("条件触发 WS 已连接[%s]: %s", exchange, url)
                    async for message in ws:
                        await self._handle_message(message, exchange)
            except Exception as e:
                logger.error("条件触发 WS[%s] 连接异常，5秒后重连: %s", exchange, e)
            self._ws_by_ex.pop(exchange, None)
            await asyncio.sleep(5)

    async def _handle_message(self, message, exchange):
        try:
            msg = json.loads(message)
        except Exception:
            return
        arg = msg.get("arg") or {}
        if arg.get("channel") != "tickers" or not msg.get("data"):
            return
        inst_id = arg.get("instId", "")
        # ★ 帧里的 instId 是该所格式（Bitget: BTCUSDT）⇒ 先归一成内部格式，否则计划永远匹配不上 ✗
        inst_id = _to_internal_symbol(exchange, inst_id)
        last = msg["data"][0].get("last", "")
        if not inst_id or not last:
            return
        try:
            last_price = float(last)
        except (TypeError, ValueError):
            return
        self._last_prices[inst_id] = last_price
        await self._check_triggers(inst_id, last_price, exchange)

    # ── 触发检查 ──
    @staticmethod
    def _persist_ref_price(plan_id, ref_price, timestamp):
        """动态参考价变化时，原子回写 xlsx 的 ref_price 与 timestamp（锁内读改写，防并发丢更新）。"""
        if not plan_id:
            return

        def update_func(current_data):
            for item in current_data:
                if str(item.get("id", "")) == str(plan_id):
                    # 定点化 + tick 对齐（幂等）：否则 PEPE 这类小额币会被 str(float) 写成 '4.573e-06'
                    item["ref_price"] = _tidy_price(
                        ref_price, item.get("symbol", ""), _row_ex(item)
                    )
                    item["timestamp"] = str(timestamp)
                    item["update_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    break
            return current_data

        try:
            FileOperations().atomic_update("condition", update_func)
        except Exception as e:
            logger.error("条件触发回写参考价失败: %s", e, exc_info=True)

    def _activate_chain(self, plan):
        """触发链命中 ⇒ 按 `chain_id` 把等待中的计划放行（用户 2026-10-25 ✓）。
        · 计划 `end_time` 已过 ⇒ **删计划** + 审计 `chain_abort` + 通知「未激活已过期」；
        · 未过期 ⇒ `chain_status: wait → live` + 审计 `chain_activate` + 通知「计划已激活」；
        · 条件行本身**一次性**：处理完即删（失败时保留，下一轮重试 ✓）。
        """
        # ★ 2026-10-26（用户裁定）：独立触发链（有 action_type）⇒ 执行账户动作，不激活计划 ✓
        if str((plan or {}).get("action_type") or "").strip():
            _execute_account_action(self, plan)
            return

        cid = str(plan.get("chain_id") or "").strip()
        label = f"{plan.get('symbol', '')} {plan.get('side', '')}".strip()
        if not cid:
            logger.error("链触发命中但缺少 chain_id，无法放行: id=%s", plan.get("id"))
            _remove_plan(plan.get("id"), reason="链触发缺少 chain_id，已作废", reason_code="chain_abort")
            return

        try:
            res = activate_chain(cid)
        except Exception as e:
            logger.error("链触发放行异常（保留条件行，下一轮重试）: %s", e, exc_info=True)
            self._plans.append(plan)  # 失败保留 ⇒ 价格再满足可重试 ✓
            return

        def _audit(action, row, msg):
            try:
                from src.audit_log import record_operation_log

                record_operation_log(
                    "condition",
                    {
                        "api_key": row.get("api_key", ""),
                        "symbol": row.get("symbol", ""),
                        "side": row.get("side", ""),
                        "plan_type": row.get("plan_type", ""),
                        "chain_id": row.get("chain_id", ""),
                    },
                    {"status": "success", "msg": msg},
                    action=action,
                    reason=msg,
                    trigger_by="engine",
                )
            except Exception as e:
                logger.warning("链触发审计写入失败: %s", e)

        activated = res.get("activated") or []
        expired = res.get("expired") or []
        for item in activated:
            row = item.get("row") or {}
            _notify(
                f"【智能触发·链触发】{row.get('symbol', label)} {row.get('side', '')} 条件成立，计划已激活",
                plan=row,
            )
            _audit("chain_activate", row, "链触发条件成立，计划已激活")
        for item in expired:
            row = item.get("row") or {}
            _notify(
                f"【智能触发·链触发】{row.get('symbol', label)} {row.get('side', '')} 未激活已过期，计划已删除",
                plan=row,
            )
            _audit("chain_abort", row, "链触发未激活已过期，计划已删除")
        if not activated and not expired:
            logger.warning("链触发命中但未找到等待中的计划（可能已手动删除/已放行）: chain_id=%s", cid)
        logger.warning(
            "链触发命中: %s chain_id=%s 放行=%d 过期删除=%d",
            label, cid, len(activated), len(expired),
        )
        # 条件行一次性 ⇒ 处理完删除（失败会保留并重试 ✓）
        _remove_plan(plan.get("id"), reason="链触发已激活（一次性）", reason_code="chain_activate")

    def _persist_condition(self, plan_id, condition):
        """把自动补全出来的方向（le/ge）落库（只更新这一列 ✓）。

        用户 2026-10-25：「只要方向是空的，就用现价与目标价格来自动判断」——
        补一次就固定，之后不再改（`auto_fill_condition` 对已有方向原样返回 ✓）。
        """
        _pid = str(plan_id or "").strip()
        if not _pid or not condition:
            return

        def update_func(rows):
            for row in rows or []:
                if str((row or {}).get("id") or "").strip() == _pid:
                    row["condition"] = condition
            return rows

        try:
            FileOperations().atomic_update(CONDITION_FILE, update_func)
        except Exception as e:
            logger.error("条件触发回写方向失败: %s", e, exc_info=True)

    async def _check_triggers(self, symbol, last_price, exchange):
        """比较该 symbol 下所有 active 计划，满足 condition 即触发。"""
        # ── 紧急停机：不触发任何条件触发计划 ──
        if self._stopped:
            return
        for plan in self._plans:
            if plan.get("symbol") != symbol:
                continue
            # ★ 按所隔离（两所 instId 同名 443 个 ⇒ 不隔离会跨所误触发 ✗）
            if str(_row_ex(plan) or "").strip().lower() != exchange:
                continue
            condition = plan.get("condition", "")
            source = plan.get("source", "")
            # 动态来源（ma/trendline，参考价随行情变化）才需 refresh_kline 降频重算；
            # price 固定价来源参考价即触发价，不重算、不回写（避免空 period 触发 refresh_kline 报错）。
            is_dynamic = source in (SOURCE_MA, SOURCE_TRENDLINE)
            if is_dynamic:
                # ── 降频：仅当超过 2 个周期（或无时间基准）才重算动态参考价，避免每 ticker 都拉 K 线 ──
                if not plan.get("timestamp") or Tool().refresh_kline(plan):
                    ref_price, ref_timestamp = calc_trigger_price(
                        plan, self._get_bot(plan.get("api_key", ""))
                    )
                    if ref_price is None:
                        # ★ 2026-10-15（看齐智能盯盘 ✓）：取价失败 ⇒ **保留 + 打标记**（不删 ✗）
                        #   —— 与"连续取价失败"同一类：`price_unavailable` ✓
                        mark_err(CONDITION_FILE, plan, ERR_PRICE_UNAVAILABLE,
                                 f"动态参考价不可用(source={source}, period={plan.get('period')})")
                        continue  # 无价格（K线不足/参数缺失）→ 跳过本轮，不比对、不误判
                    # 取价恢复正常 ⇒ 清掉历史标记（只在原来有值时写盘 ✓）
                    clear_err(CONDITION_FILE, plan)
                    # 参考价变化 → 动态回写本地记录（供展示/审计，值未变则跳过写入降频）
                    # ⚠️ 比较必须用与落库同一口径（定点 + tick 对齐）的字符串：
                    # 拿落库的 '0.000004573' 去比 str(float) 的 '4.573e-06' 会永不相等 ⇒ 每轮都回写。
                    new_ref_price = _tidy_price(
                        ref_price, plan.get("symbol", ""), _row_ex(plan)
                    )
                    if (
                        str(plan.get("ref_price", "")) != new_ref_price
                        or str(plan.get("timestamp", "")) != str(ref_timestamp)
                    ):
                        plan["ref_price"] = new_ref_price
                        plan["timestamp"] = str(ref_timestamp)
                        self._persist_ref_price(plan.get("id"), new_ref_price, ref_timestamp)
                else:
                    # 未到重算门槛：用上一轮落库的 ref_price 比对
                    try:
                        ref_price = float(plan.get("ref_price", ""))
                    except (TypeError, ValueError):
                        # ★ 2026-10-15：落库参考价缺失/坏值 ⇒ 保留 + 打标记（不删 ✗）
                        mark_err(CONDITION_FILE, plan, ERR_FIELD_MISSING,
                                 "计划缺少可用的 ref_price（动态参考价未落库）")
                        continue
            else:
                # price 固定价来源：参考价 = 触发价，直接比对
                ref_price = plan.get("trigger_price")
                if ref_price is None:
                    mark_err(CONDITION_FILE, plan, ERR_FIELD_MISSING, "计划缺少 trigger_price")
                    continue
                try:
                    ref_price = float(ref_price)
                except (TypeError, ValueError):
                    mark_err(CONDITION_FILE, plan, ERR_FIELD_MISSING,
                             f"trigger_price 不是合法数字: {plan.get('trigger_price')!r}")
                    continue
                # 固定价路径：参考价永远可算 ⇒ 恢复正常即清标记 ✓
                clear_err(CONDITION_FILE, plan)
            # ★★ 2026-10-25（用户裁定）：「只要方向是空的，就用现价与目标价格自动判断」✓
            #   ⇒ 在**同一个地方**补全并落库（各写入方只写计划、不判方向 ✓）。
            #   补不了（现价 == 目标价 / 无价）⇒ 本轮跳过，等价格离开再补 ✓。
            if str(plan.get("condition") or "").strip().lower() not in ("le", "ge"):
                _filled = auto_fill_condition(plan, last_price)
                if not _filled:
                    continue
                condition = _filled
                self._persist_condition(plan.get("id"), condition)
            hit = (condition == "le" and last_price <= ref_price) or (
                condition == "ge" and last_price >= ref_price
            )
            if not hit:
                continue
            logger.warning(
                "条件触发命中: %s %s type=%s last=%s ref=%s condition=%s",
                symbol, plan.get("side"), plan.get("plan_type"),
                _fmt_price(last_price), _fmt_price(ref_price), condition,
            )
            # 触发后立即从内存缓存摘除，防止重复触发
            self._plans = [p for p in self._plans if p.get("id") != plan.get("id")]
            # ★★ 2026-10-25：「触发链」命中 ⇒ 放行（wait → live），**不执行**平仓/保本/移损 ✓
            if str(plan.get("plan_type") or "").strip() == PLAN_TYPE_TRIGGER:
                self._activate_chain(plan)
                continue
            bot = self._get_bot(plan.get("api_key", ""))
            if bot is None:
                _notify(
                    f"【智能触发】{symbol} 触发但创建交易实例失败，计划保留待重试",
                    plan=plan,
                )
                # ★ 2026-10-15（看齐智能盯盘 ✓）：账户不可用 ⇒ **保留 + 打标记**（不删 ✗）
                mark_err(CONDITION_FILE, plan, ERR_ACCOUNT_UNUSABLE,
                         "创建交易实例失败（api_key 不存在/账户不可用）")
                self._plans.append(plan)  # 失败保留，价格再满足可再试
                continue
            plan["ref_price"] = _tidy_price(
                        ref_price, plan.get("symbol", ""), _row_ex(plan)
                    )
            # 线程池执行（不阻塞 WS 收包）
            asyncio.get_running_loop().run_in_executor(
                None, self._execute_trigger_sync, plan, bot
            )

    def _execute_trigger_sync(self, plan, bot):
        """同步执行触发（在线程池），记录结果。"""
        try:
            result = execute_trigger(plan, bot)
            logger.info("条件触发执行结果: %s", result)
        except Exception as e:
            logger.error("条件触发执行异常: %s", e, exc_info=True)

    # ── 主循环 ──
    async def _tick_loop(self):
        """1s tick：计划重载 + 订阅同步（与 ws_loop 同事件循环，send 线程安全）。"""
        while True:
            try:
                # ── 紧急停机：跳过计划重载与订阅同步（恢复后自动重订阅）──
                self._stopped = check_and_notify()
                if self._stopped:
                    await asyncio.sleep(1)
                    continue
                self._reload_plans_if_changed()
                # ★ 每所确保一条 WS 任务（新所自动补建 ✓）
                for _ex in list(self._symbols_by_ex):
                    _t = self._tasks_by_ex.get(_ex)
                    if _t is None or _t.done():
                        self._tasks_by_ex[_ex] = asyncio.create_task(self._ws_loop(_ex))
                # 按所重订阅（版本号变化时）
                for _ex, _ws in list(self._ws_by_ex.items()):
                    if _ws and self._sub_serial_by_ex.get(_ex, 0) != self._sent_serial_by_ex.get(_ex, -1):
                        self._sent_serial_by_ex[_ex] = self._sub_serial_by_ex.get(_ex, 0)
                        await self._send_subscriptions(_ws, _ex)
            except Exception as e:
                logger.error("条件触发 tick 异常: %s", e, exc_info=True)
            await asyncio.sleep(1)

    async def run(self):
        logger.info("条件触发监控进程启动")
        await self._tick_loop()


def main():
    try:
        asyncio.run(ConditionProcess().run())
    except KeyboardInterrupt:
        logger.info("条件触发监控进程退出")


if __name__ == "__main__":
    main()
