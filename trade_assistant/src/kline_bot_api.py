# -*- coding: utf-8 -*-
# cython: language_level=3

# -*- coding: utf-8 -*-

# Author : 'hxc'

# Time: 2023/2/10 12:47 PM

# File_name: 'kline_bot_api.py'

"""
Describe: K 线常驻服务入口（**独立进程** `kline_bot`）✓
"""


import sys; sys.path.insert(0, ".")
from src.single_instance import SingleInstance
SingleInstance("kline_bot").acquire()

from src.user_logger import get_logger

logger = get_logger()
from src.kline_bot import KlineUpdater


def main():
    # ★★ 2026-10-14 用户裁定（原话：「**signal_monitor 改回来，然后改名为 signal_bot**」；
    #   前文澄清：「signal_bot 可以导入 kline_bot 的功能，**他们本质上还是两个独立的**，
    #   因为 **K 线优先级是 JSON 文件的，是可行的**」）⇒ **恢复"独立进程"架构**：
    #
    #   ── 历史缘由（保留，别再走回头路 ✗）──────────────────────────────────
    #   2026-10-13 曾把信号检测搬进本进程（进程内线程 `run(signal_monitor=…)`），理由是：
    #   `SignalMonitorProgram.collect_update_pairs()` 只生成清单，**执行刷新要回调
    #   `KlineUpdater._monitor_updater`**（owner=`monitor` ⇒ 最新行情优先级第 1 档）；
    #   而回调**只能在 kline_bot 进程内注入** ⇒ 那时判断"跨进程只能提交申请、是否执行
    #   不可确定"。
    #   ⇒ **该判断已被用户否掉** ✓：跨进程同样能确认 —— 走 `kline_priority` 那套 JSON
    #     机制，由执行侧**回写回执**，信号侧**拿到回执才继续检测**（语义与"同步回调返回"
    #     等价 ✓）。实现见 `src/signal_refresh_bridge.py`：
    #       请求 `json/kline_refresh_request.json` → 本进程执行 → 回执 `json/kline_refresh_reply.json`
    #     （协议字段 / 超时 / 降级口径全在该模块 docstring，一处定义、两侧共用 ✓）
    #
    #   ⚠️ 因此本进程**不再注入** `SignalMonitorProgram._updater_callback`、**不再起**
    #      「信号检测」线程（那是双跑检测 ✗）；信号检测由**独立进程** `signal_bot`
    #      （`src/signal_bot_api.py`，见 `config.sh` 的 `PYTHON_SERVICES`）跑 ✓
    #      `KlineUpdater._monitor_updater` **保留** —— 它是跨进程请求通道的执行端 ✓
    updater = KlineUpdater()
    updater.run()


if __name__ == "__main__":
    main()
