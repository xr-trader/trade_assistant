# -*- coding: utf-8 -*-
# cython: language_level=3

# -*- coding: utf-8 -*-

# Author : 'hxc'

# Time: 2023/2/10 12:47 PM

# File_name: 'modify_order_monitor_api.py'
# 本程序用于启动接管系统，请勿更改
"""
Describe: this is a demo!
"""

import sys; sys.path.insert(0, ".")
import time
import signal
from src.single_instance import SingleInstance
SingleInstance("modify_order").acquire()

from src.user_logger import get_logger

logger = get_logger()
from src.modify_order_monitor import ModifyOrderMonitor

_stop = False


def _handle_signal(signum, frame):
    global _stop
    logger.info("收到信号 %s，准备优雅退出", signum)
    _stop = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

while not _stop:
    try:
        f = ModifyOrderMonitor()
        f.on_tick()
    except KeyboardInterrupt:
        break
    except Exception as e:
        if _stop:
            break
        logger.error("小二系统异常退出: %s，10秒后重试", str(e), exc_info=True)
        time.sleep(10)
