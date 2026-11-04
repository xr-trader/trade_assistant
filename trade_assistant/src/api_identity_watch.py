# -*- coding: utf-8 -*-
"""`api_list` 身份字段看门狗 —— `(api_key, uid, mainUid)` 指纹比对 + 按**交易所真值**自动纠正。

用户 2026-10-11 设计定稿（讨论见对话 ✓）：

* **主键 = `api_key`** ✓（与全项目既有口径一致：`set_api_info` 的 add/edit、面板、授权同步都按它匹配 ✓）
* 正常情况下 `api_key` / `uid` / `mainUid` **永远不会变** ✓
  ⇒ **一变就说明"文件被动过 / 被手插了行"** ✓
  ⇒ 所以只需**每轮比一次指纹**（读表走 mtime 缓存 ⇒ 几乎零成本 ✓），
    **只在变化时**才付一次交易所调用 ✓ —— 平时零请求、零写盘 ✓
* 变化 ⇒ 对**涉及的账户**重取一次 `get_account_config` ⇒ 用**交易所真值**写回 ✓
  ⚠️ **不能**拿"进程内缓存里的旧值"当权威 ✗ —— 用户合法换 key/重加账号时旧值已过期 ✓
     （缓存只用来判断"变没变" ✓，真值必须来自交易所 ✓）
* `api_key` 消失 ⇒ 只丢掉缓存条目 ✓（行已被用户删掉 ✓，**不去"恢复"** ✓）
* 取不到（key 无效 / 网络异常）⇒ WARNING + **仍然更新指纹** ✓
  （否则每轮重试 = 打交易所 + 刷屏 ✗）
* **首轮（进程启动）全量重取一遍** ✓ ⇒ 连"停机期间被改 / 被插行"也一并纠正 ✓
  （⚠️ 纯内存基线在重启后为空，若不这么做，停机期间的篡改就永远发现不了 ✗）
* **本模块有两个写 `api_list` 的入口，都在同一进程内** ✓
  ① `ApiIdentityWatch.check()`（身份字段纠正）—— 唯一调用点是**业务循环**
     （`modify_order_monitor._biz_cycle_once`）
  ② `apply_uid_pairs_marks()`（`account_status` 打标，2026-10-11 T4）—— 唯一调用点是
     **授权心跳循环**（`modify_order_monitor._verify_account_inner` → `_apply_uid_pairs_reply`）
  —— `kline_bot` / 面板服务是**独立进程**，各自也有缓存 ⇒ 它们只读，避免并发写同一行 ✗
  （同进程两线程的并发由 `read_edit.atomic_update` 的 `flock` 文件锁串行化 ✓）

设计上的取舍（有意为之 ✓）：
* 只纠正 `uid` / `mainUid` / `main_uid` / `mainuid` / `account_uid` ✓
  —— **不动 `flow_uid`**（它另有语义，见 `trade_flow.py`，不在此处擅自改写 ✗）
* 纠正时写**审计**（`operation_type="config"`，action=`auto_fix_api_identity` ✓）+ WARNING 日志 ✓
  —— 默认**不发钉钉** ✓（避免打扰；要开在 `_notify` 处加一行即可 ✓）
* **不做防重复**（同一 `mainUid` 下多账号是否允许）—— 用户 2026-10-11 未裁定，留待后续 ✓
"""
from __future__ import annotations

import threading

from src.user_logger import get_logger
from src.read_edit import FileOperations
from src.audit_log import record_operation_log
from exchange.factory import create_exchange

logger = get_logger()

#: ★ 注册机邮箱（用户 2026-10-11）：**只有这台机器**才做"链接注册用户 UID 校验"。
#: 判定来源 = `sys_set.email`（`api_list` 里**没有** email 列 ✓）
REGISTER_MACHINE_EMAIL = "registered@registered.com"

#: ★ 2026-10-11（用户授权：「你可以直接给 144 的打标，来测试双向」）：**显式测试后门** ——
#: `sys_set.mark_status_machine` 为真值时，**普通机器也允许打标** ✓
#: ⚠️ 默认**没有这一列** ⇒ 关门，规则 0 一字不变 ✗；只有**显式写 1** 的机器才开 ⇒ 生产不受影响 ✓
MARK_OVERRIDE_FIELD = "mark_status_machine"
_MARK_OVERRIDE_TRUTHY = ("1", "true", "yes", "on")


def is_register_machine() -> bool:
    """本机是否是那台"注册机"（`sys_set.email == registered@registered.com`）。

    ⚠️ 2026-10-11 修订后**不再用于心跳发送**（发送已改为日常内容 ✓，判定归授权后端 ✓）。
    ★ 2026-10-11 **已接线**：`apply_uid_pairs_marks()` 的**规则 0** 闸门 ——
      **只有注册机才允许写 `account_status`** ✓（用户原话：「打标还有个前提必须 email 是 registered」）。

    安全语义：读不到 / 异常 / email 为空 ⇒ **一律返回 False** ✓
    （失败安全：普通机器**绝不**因为读表失败就"变成注册机"去打标 ✗）。

    ★ 另有**显式测试后门**（2026-10-11 用户授权在 144 上验双向打标）：
      `sys_set.mark_status_machine` 为真值 ⇒ 普通机器也允许打标 ✓；默认无此列 ⇒ 关门 ✗。
      命中后门时**必打一条 WARNING 明确标注"不是注册机、是显式授权"**（日志不许冒充注册机 ✗）。
    """
    try:
        sys_set = FileOperations().read_file_info("sys_set")
        row = sys_set[0] if sys_set else {}
        email = str(row.get("email") or "").strip().lower()
        if email == REGISTER_MACHINE_EMAIL:
            return True
        override = str(row.get(MARK_OVERRIDE_FIELD) or "").strip().lower()
        if override in _MARK_OVERRIDE_TRUTHY:
            logger.warning(
                "[uid-pairs] ⚠️ 本机**不是**注册机（email=%s），但 sys_set.%s=%r ⇒ "
                "按**显式授权**允许打标（仅测试 ✓）",
                email or "(空)", MARK_OVERRIDE_FIELD, override,
            )
            return True
        return False
    except Exception as e:  # noqa: BLE001 — 读不到就当作"不是" ✓（不误伤普通机器）
        logger.error("[uid-pairs] 读取 sys_set.email 失败: %s", e)
        return False


def diff_uid_hits(local_map: dict, hit_map: dict) -> tuple:
    """比对"本机全量" vs "服务器回的命中" ⇒ `(命中数, 未命中清单)` ✓。

    ⚠️ **以本机请求集为准**（响应里多出来的 uid 一律忽略 ✓）
    ⚠️ 只做纯计算，**不写盘、不改任何状态** ✓
    """
    hit, miss = 0, []
    for ex, uids in (local_map or {}).items():
        got = {str(x) for x in ((hit_map or {}).get(ex) or [])}
        for uid in uids or []:
            if str(uid) in got:
                hit += 1
            else:
                miss.append(f"{ex}:{uid}")
    return hit, miss


#: 未命中 ⇒ 写进 `api_list.account_status` 的值（用户 2026-10-11 定稿 ✓）。
#: 命中 ⇒ **清空**（空串 ✓）—— 用户明确要求「双向命中」：解除必须真的发生 ✓
ACCOUNT_STATUS_STOP = "stop"


def apply_uid_pairs_marks(hit_map: dict) -> list:
    """按授权服务器回的命中清单，**双向**写 `api_list.account_status`（T4 ✓）。

    规则（用户 2026-10-11 定稿，六条缺一不可 ✓）
    -------------------------------------------
    0. ★ **只有「注册机」才允许打标** ✗✗（用户 2026-10-11 追加：
       「打标还有个前提必须 email 是 registered」）
       —— 判据 = `sys_set.email == registered@registered.com`（`is_register_machine()` ✓）。
       ⚠️ 放在**最前面**：这是**唯一咽喉**，任何调用方都绕不过 ✓
       ⚠️ 为什么必须有：普通机器（如 144，email=`247042033@qq.com`）若也打标，
          会被 `check_api_usable` 消费成「账号突然不可用」✗✗（2026-10-19 已接消费侧）
       ⚠️ 读不到/异常 ⇒ `is_register_machine()` 返回 False ⇒ **不打标**（失败安全 ✓）
    1. 只有 `status == "success"` **且**响应里**真的有** `uid_pairs` 时才判定 ✓
       —— 由**调用方**把关（这里再兜一层：非 dict ⇒ 直接不判定 ✓）
    2. 响应里 `{}` ⇒ 视为"真·全部未命中" ✓
    3. **以本机请求集为准** ✓ —— 过滤条件与 `collect_uid_map()` **逐字一致**
       （缺 `exchange` / 缺 `mainUid` 的行**根本没被发出去** ⇒ **绝不判定** ✗✗
        否则一行没同步 uid 的账号会被误标 `stop` ✓）
    4. **双向** ✓：命中 ⇒ 清空 `account_status`；未命中 ⇒ 写 `"stop"`
    5. **只在变化时写盘** ✓ + 每次真写盘都**写审计** ✓（照看门狗的指纹做法 ✓）

    ⚠️ 打标只落数据；消费收口在 `BotUtils.check_api_usable`，且**只在注册身份**下生效
       （2026-10-19 用户裁定：`account_status` 仅在 `sys_set.email == registered@registered.com` 时有效 ✓）。

    Returns:
        真正被改动的 `api_key` 列表（值没变的**不算** ✓，便于日志/测试断言 ✓）
    """
    # ★ 规则 0：非注册机 ⇒ 一律不打标（放在读表/判定之前，最省事也最安全 ✓）
    if not is_register_machine():
        logger.info(
            "[uid-pairs] 本机不是注册机（sys_set.email != %s）⇒ 按规则 0 **不打标** ✓",
            REGISTER_MACHINE_EMAIL,
        )
        return []
    if not isinstance(hit_map, dict):
        return []          # 规则 1 兜底：形状不对 ⇒ 不判定 ✓
    try:
        rows = FileOperations().read_file_info("api_list") or []
    except Exception as e:  # noqa: BLE001 — 读表失败不影响心跳 ✓
        logger.error("[uid-pairs] 打标前读取 api_list 失败: %s", e, exc_info=True)
        return []
    changed: list = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        key = str(r.get("api_key") or "").strip()
        ex = str(r.get("exchange") or "").strip().lower()
        main = str(
            r.get("mainUid") or r.get("main_uid") or r.get("mainuid") or ""
        ).strip()
        if not key or not ex or not main:      # 规则 3：没发出去 ⇒ 不判定 ✓
            continue
        got = {str(x) for x in ((hit_map.get(ex) or []))}
        desired = "" if main in got else ACCOUNT_STATUS_STOP
        if str(r.get("account_status") or "").strip() == desired:
            continue                            # 规则 5：值不变 ⇒ 零写盘 ✓
        try:
            FileOperations().edit_file_info(
                "api_list", {"api_key": key}, "write", {"account_status": desired}
            )
        except Exception as e:  # noqa: BLE001 — 单个账号写失败不影响其他账号 ✓
            logger.error("[uid-pairs] 打标写盘失败（%s）: %s", key[:8], e, exc_info=True)
            continue
        changed.append(key)
        msg = (
            "授权服务器命中清单未包含本账号 ⇒ 标记 account_status=stop，已停止使用"
            if desired else
            "授权服务器命中清单已包含本账号 ⇒ 已清空 account_status（恢复可用）"
        )
        logger.warning("[uid-pairs] %s | %s", str(r.get("account_name") or "").strip() or key[:8], msg)
        try:
            record_operation_log(
                "config",
                {"api_key": key, "account_name": r.get("account_name", ""), "exchange": ex},
                {"status": "success", "msg": msg},
                action="mark_account_status",
            )
        except Exception as e:  # noqa: BLE001 — 审计失败不能影响打标本身 ✓
            logger.error("[uid-pairs] 写审计失败: %s", e, exc_info=True)
    return changed


def collect_uid_map() -> dict:
    """收集 `{exchange: [mainUid, ...]}`（**按所分组、组内去重** ✓）—— **心跳的日常内容** ✓。

    形状按用户 2026-10-11 口径（原话）：
    > 「`uid_pairs` 不要是一个 list，是一个 **dict** 就好了，根据交易所建立两个可以，
    >   然后每个交易的 uid 在一个 list 中过去不就行了么」
    ⇒ 与"成对 list"相比**体积小一倍以上**（键名只出现两次 ✓）
    ⇒ 服务端拿到的就是 `for ex, uids in uid_pairs.items()`，天然按所遍历 ✓

    ⚠️ 组内去重但仍**保留 `api_list` 的行序**（稳定、便于人看 ✓）；空所不出现 ✓；全空返回 `{}` ✓

    用户 2026-10-11 口径（第二次修订）：
    > 「这个根据交易所去重的 uid 信息，可以作为心跳信号的**日常内容**，
    >   授权后端也只会检测**特定账号**」
    ⇒ 本机**不再**用 `sys_set.email` 做门槛 ✗（心跳里本就带 `email` ✓，
      由**授权后端**判断"是不是那台注册机 / 该处理哪些账号" ✓✓）
    ⇒ 每次都发；没有可用字段时返回 `[]`（心跳载荷一字不变 ✓）

    ⚠️ 判定键**必须带 exchange** —— 两所 uid 空间独立，数字相同也毫无关系 ✗
    ⚠️ 规模：本清单会**挂在每个账户的心跳上**（心跳是按账户发的 ✓）⇒ 账户很多时
      载荷会被重复 N 次（见 tests 里的规模断言与注释 ✓）
    """
    out: dict = {}
    seen: dict = {}
    for r in FileOperations().read_file_info("api_list") or []:
        if not isinstance(r, dict):
            continue
        ex = str(r.get("exchange") or "").strip().lower()
        main = str(
            r.get("mainUid") or r.get("main_uid") or r.get("mainuid") or ""
        ).strip()
        if not ex or not main:
            continue
        marks = seen.setdefault(ex, set())
        if main in marks:          # 组内去重 ✓
            continue
        marks.add(main)
        out.setdefault(ex, []).append(main)
    return out

#: 会被自动纠正的身份字段（⚠️ 不含 `flow_uid` —— 它另有语义，别在此处改写 ✗）
_FIX_FIELDS = ("uid", "mainUid", "main_uid", "mainuid", "account_uid")


class ApiIdentityWatch:
    """按 `api_key` 主键看守 `(uid, mainUid)` 的进程内基线。

    用法（**只在业务循环里调** ✓）::

        _WATCH = ApiIdentityWatch()
        ...
        bot_infos = self._get_usable_bot_infos()
        _WATCH.check(bot_infos)          # 正常情况：一次 dict 比较后立即返回 ✓
    """

    def __init__(self) -> None:
        #: {api_key: (uid, mainUid)}；None = 还没建过基线（首轮 ⇒ 全量核对 ✓）
        self._baseline: dict[str, tuple[str, ...]] | None = None
        #: 统计（便于排查/测试断言 ✓）
        self.stats = {"fingerprint_checks": 0, "refetch": 0, "fixed": 0, "failed": 0}
        #: 本轮真正写盘纠正过的 api_key（供 check() 汇总返回 ✓）
        self._fixed_keys: set[str] = set()
        #: 仅业务循环单线程调用 ⇒ 不需要锁；加一把只为防御将来误用 ✓
        self._lock = threading.Lock()

    # ── 内部小工具 ───────────────────────────────────────────────
    #: 会被伪造/会被本模块纠正的全部字段（⚠️ 指纹必须覆盖**这五个** ✗）
    #   为什么不能用 (uid, mainUid) 两个当指纹（2026-10-11 自查实测踩到 ✗）：
    #   伪造者只改 `main_uid`/`mainuid`/`account_uid` 而让 `uid`/`mainUid` 保持真值
    #   ⇒ 两元指纹**没变** ⇒ check() 直接 return ⇒ 假值永远不被纠正 ✗✗
    #   —— 而 add 路径读 `mainuid`、check_api_usable 读 `main_uid`，正好都在别名里 ✗
    _FIELDS = ("uid", "mainUid", "main_uid", "mainuid", "account_uid")

    @classmethod
    def _fields_of(cls, row: dict) -> tuple[str, ...]:
        """一条行的五个身份字段原始值（字符串 ✓）—— 指纹与比对都用它 ✓"""
        return tuple(str(row.get(k) or "").strip() for k in cls._FIELDS)

    @classmethod
    def _truth_fields(cls, uid: str, main: str) -> tuple[str, ...]:
        """交易所真值对应的五元组（含三个别名归一 ✓）"""
        return (uid, main, main, main, uid)

    @staticmethod
    def _id_of(row: dict) -> tuple[str, str]:
        """取一条 api_list 行的 `(uid, mainUid)`（**字符串** ✓，兼容三个下划线别名 ✓）。
        仅用于日志/统计展示 ✓；**指纹与比对**请用 `_fields_of` ✓"""
        uid = str(row.get("uid") or "").strip()
        main = str(
            row.get("mainUid") or row.get("main_uid") or row.get("mainuid") or ""
        ).strip()
        return uid, main

    def fingerprint(self, rows) -> dict[str, tuple[str, ...]]:
        """算指纹：`{api_key: 五元组}`（无 `api_key` 的行跳过 ✓）。"""
        fp: dict[str, tuple[str, ...]] = {}
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            key = str(r.get("api_key") or "").strip()
            if not key:
                continue
            fp[key] = self._fields_of(r)
        return fp

    # ── 主入口 ───────────────────────────────────────────────────
    def check(self, rows=None) -> list[str]:
        """比一次指纹；有变化才对涉及的账户重取交易所真值并纠正。

        Args:
            rows: `api_list` 的行；**不给就自己读整表** ✓
                  ⚠️ 传 `_get_usable_bot_infos()` 会**漏掉不可用/被停用/手插的行** ✗
                     —— 2026-10-11 首轮实测只看到 2/7 行 ⇒ 现在调用方不传参数 ✓

        Returns:
            被纠正的 `api_key` 列表（便于日志/测试 ✓）
        """
        if rows is None:
            try:
                rows = FileOperations().read_file_info("api_list") or []
            except Exception as e:  # noqa: BLE001 — 读表失败不影响业务循环 ✓
                logger.error("[身份看门狗] 读取 api_list 失败: %s", e, exc_info=True)
                return []
        current = self.fingerprint(rows)
        by_key = {
            str(r.get("api_key") or "").strip(): r
            for r in (rows or [])
            if isinstance(r, dict) and str(r.get("api_key") or "").strip()
        }
        with self._lock:
            first = self._baseline is None
            if not first and current == self._baseline:
                # ★ 99.99% 的时间走这里：一次 dict 比较（读表本身走 mtime 缓存 ✓）
                return []
            self.stats["fingerprint_checks"] += 1
            prev = self._baseline or {}
            if first:
                targets = list(current.keys())  # 首轮全量 ✓（覆盖停机期间被改/被插行 ✓）
                logger.info("[身份看门狗] 首轮建立基线：核对 %d 个账户", len(targets))
            else:
                targets = [k for k in current if prev.get(k) != current[k]]
                removed = [k for k in prev if k not in current]
                if removed:
                    logger.info("[身份看门狗] %d 个账户已从 api_list 移除（只丢基线，不恢复）", len(removed))
                if targets:
                    logger.warning(
                        "[身份看门狗] 检测到 %d 个账户的身份字段变化：%s",
                        len(targets), [k[:8] for k in targets],
                    )
            # 基线更新为**真值**✓：取到交易所真值的用真值（下一轮不再误判 ✓），
            # 取不到的沿用当前值（含失败项 ✓）—— 否则每轮都"发现变化" ⇒ 写风暴 ✗
            new_baseline = dict(current)
            fixed = []
            for k in targets:
                truth = self._refetch_and_fix(by_key[k])
                if truth is not None:
                    # 落**真值五元组**（下一轮不再误判 ✓）
                    new_baseline[k] = self._truth_fields(*truth)
                if k in self._fixed_keys:
                    fixed.append(k)
            self._fixed_keys = set()
            self._baseline = new_baseline
        return fixed

    # ── 单个账户：重取 + 纠正 ────────────────────────────────────
    @classmethod
    def _needs_fix(cls, row: dict, uid: str, main: str) -> bool:
        """**五个字段全部**比对（⚠️ 只比 mainUid 会被绕过 ✗）：
        `uid` / `mainUid` / `main_uid` / `mainuid` / `account_uid`。

        为什么必须全比（2026-10-11 自查发现）：
          · add 路径的免费用户校验读的是 **`mainuid`** ✗
          · `check_api_usable` 读的是 **`main_uid`** ✗
          ⇒ 伪造者只要把假值藏在别名里（`mainUid` 保持真值）就能绕过 ✗✗
        """
        return cls._fields_of(row) != cls._truth_fields(uid, main)

    def _refetch_and_fix(self, row: dict):
        """重取交易所真值；需要就纠正。

        Returns:
            `(uid, mainUid)` 交易所**真值**（取到了就返回，供调用方落基线 ✓）；
            取不到（key 无效/网络/无 uid）⇒ `None`（保留原行，不重试 ✓）
        """
        key = str(row.get("api_key") or "").strip()
        label = str(row.get("account_name") or "").strip() or key[:8]
        self.stats["refetch"] += 1
        try:
            bot = create_exchange(row)
            res = bot.custom_actions({}, "get_account_config")
        except Exception as e:  # noqa: BLE001 — 任何异常都不该影响业务循环 ✓
            self.stats["failed"] += 1
            logger.warning("[身份看门狗] %s 取账户配置失败（保留原行，本轮不再重试）: %s", label, e)
            return None
        if str(res.get("code") or "") != "0" or not (res.get("data") or []):
            self.stats["failed"] += 1
            logger.warning(
                "[身份看门狗] %s 账户配置不可用：code=%s msg=%s（保留原行）",
                label, res.get("code"), str(res.get("msg"))[:80],
            )
            return None

        cfg = res["data"][0]
        uid = str(cfg.get("uid") or "").strip()
        # Bitget 没有母账户字段 ⇒ 回落自身 uid（与 _fix_account / bot_utils 同口径 ✓）
        main = str(cfg.get("mainUid") or "").strip() or uid
        if not uid:
            self.stats["failed"] += 1
            logger.warning("[身份看门狗] %s 交易所未返回 uid（保留原行，不编造 ✗）", label)
            return None

        if not self._needs_fix(row, uid, main):
            return (uid, main)  # 五个字段都一致 ⇒ 不写盘 ✓（基线照样落真值 ✓）

        try:
            FileOperations().edit_file_info(
                "api_list",
                {"api_key": key},
                "write",
                {"uid": uid, "mainUid": main, "main_uid": main, "mainuid": main, "account_uid": uid},
            )
        except Exception as e:  # noqa: BLE001
            self.stats["failed"] += 1
            logger.error("[身份看门狗] %s 纠正写盘失败: %s", label, e, exc_info=True)
            return None

        self.stats["fixed"] += 1
        self._fixed_keys.add(key)
        cur_uid, cur_main = self._id_of(row)
        msg = (
            f"api_list 身份字段与交易所不一致，已按交易所真值纠正: "
            f"uid {cur_uid or '(空)'}→{uid}, mainUid {cur_main or '(空)'}→{main}"
        )
        logger.warning("[身份看门狗] %s | %s", label, msg)
        # 审计（配置变更面 ✓）；审计内部会脱敏，且这里本就不含密钥 ✓
        try:
            record_operation_log(
                "config",
                {"api_key": key, "account_name": row.get("account_name", ""), "exchange": row.get("exchange", "")},
                {"status": "success", "msg": msg},
                action="auto_fix_api_identity",
            )
        except Exception as e:  # noqa: BLE001 — 审计失败不能影响纠正本身 ✓
            logger.error("[身份看门狗] 写审计失败: %s", e, exc_info=True)
        return (uid, main)
