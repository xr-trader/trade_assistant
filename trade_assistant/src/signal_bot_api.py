# -*- coding: utf-8 -*-
# cython: language_level=3

# -*- coding: utf-8 -*-

# Author : 'hxc'

# Time: 2023/2/10 12:47 PM

# File_name: 'signal_bot_api.py'

"""
Describe: 信号检测常驻服务入口（**独立进程** `signal_bot`）✓
"""


import sys; sys.path.insert(0, ".")
import os
import time

# 信号检测强依赖 OKX 的 UTC+8（东八区）日界：
#   - schedule 定时（每根 K 线收盘后 30s 触发）按进程本地时间计算，必须锁定为 UTC+8；
#   - 与 kline 合并锚点（UTC+8 00:00 日界）保持一致。
# 仅作用于本进程，不影响 kline_bot / backend / pusher 等其它常驻服务。
os.environ["TZ"] = "Asia/Shanghai"
try:
    time.tzset()
except AttributeError:  # 非 POSIX 平台无 tzset
    pass

from src.single_instance import SingleInstance
# ★ 2026-10-14 更名：服务名 `signal_monitor` → `signal_bot`（进程名/单实例锁/日志名三者同源 ✓）
SingleInstance("signal_bot").acquire()

from src.user_logger import get_logger

logger = get_logger()
# ⚠️ 引擎模块 `src/signal_monitor_program.py` **有意不改名**（改动面太大：前端数据表
#    `signal_monitor_program`、护栏基线、20+ 处回归都引用它）⇒ 连引擎一起改名是**另一轮** ✓
from src.signal_monitor_program import SignalMonitorProgram


def main():
    # NL-05：进程级兜底重试——run() 内部异常不再让进程直接退出，
    # 避免依赖 crontab 最多 1 分钟的拉起窗口导致信号丢失
    #
    # ★★ 2026-10-14 架构回归（用户裁定）：「signal_monitor 改回来，然后改名为 signal_bot」
    #   ⇒ 信号检测**不再寄生在 kline_bot 的进程内线程**，重新成为**独立进程** ✓
    #   K 线新鲜度改由**跨进程 JSON 协调**保证（`src/signal_refresh_bridge.py`）：
    #   本进程写请求 → kline_bot 执行刷新（owner=monitor ⇒ 最新行情第 1 档）→ 回执确认 ✓
    while True:
        try:
            f = SignalMonitorProgram()
            f.run()
        except Exception as e:
            logger.error("signal_bot 异常退出: %s，10秒后重试", str(e), exc_info=True)
            time.sleep(10)
        else:
            logger.warning("signal_bot run() 返回，3秒后重启")
            time.sleep(3)


if __name__ == "__main__":
    main()
