# -*- coding: utf-8 -*-
"""
服务启动/停止审计日志记录脚本。

供 service.sh 在 start_service / stop_service 成功后调用，
将服务启停事件写入 operation_audit_log 表。

用法:
  python3 -u src/audit_service_event.py <action> <service_name>
  例: python3 -u src/audit_service_event.py startup backend
       python3 -u src/audit_service_event.py shutdown kline_bot
"""

import os
import sys
import time
import uuid

# 以脚本方式启动时 sys.path[0] 是 src/，chdir 并不能让 import src.xxx 生效，
# 必须显式把项目根目录加入 sys.path（service.sh 从项目根调用本脚本）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.read_edit_sqlite import _get_connection

from src.user_logger import get_logger
logger = get_logger("audit_service")


SERVICE_LABELS = {
    "backend": "后端主程序",
    "kline_bot": "K线机器人",
    "modify_order": "订单修改监控",
    "signal_bot": "信号监控",
    "condition": "智能触发监控",
    "pusher": "消息推送服务",
}


def record_service_event(action: str, service_name: str):
    """
    记录服务启停事件到审计日志。

    Args:
        action: startup | shutdown
        service_name: backend | kline_bot | modify_order | signal_bot | condition | pusher
    """
    service_label = SERVICE_LABELS.get(service_name, service_name)

    if action == "startup":
        status = "success"
        reason = f"{service_label} 已启动"
    elif action == "shutdown":
        status = "success"
        reason = f"{service_label} 已停止"
    else:
        status = "fail"
        reason = f"{service_label} 未知动作: {action}"

    record_id = f"svc_{int(time.time())}_{uuid.uuid4().hex[:8]}"

    try:
        conn = _get_connection()
        conn.execute(
            """INSERT INTO operation_audit_log
               (id, api_key, operation_type, action, status, error_msg, reason,
                symbol, side, request, response, computed_sz, computed_price,
                ord_ids, algo_ids, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record_id,
                "system",
                "service",
                action,
                status,
                "",
                reason,
                # symbol 是审计页的展示字段（界面列「币对」），服务事件没有币对，
                # 写中文服务名更可读；原始 service_name 仍保留在 request JSON 里可查。
                service_label,
                "",
                '{"action":"%s","service":"%s"}' % (action, service_name),
                '{"status":"%s","reason":"%s"}' % (status, reason),
                "",
                "",
                "[]",
                "[]",
                # created_at 必须与审计表其余记录一致用毫秒：
                # 清理任务按 created_at < now_ms - retention_days*86400*1000 删除，
                # 写成秒会让服务事件被当成"远古日志"立刻清掉。
                int(time.time() * 1000),
            ),
        )
        conn.commit()
        logger.info("已记录服务事件: %s", reason)
    except Exception as e:
        logger.error("记录服务事件失败: %s", e, exc_info=True)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"用法: {sys.argv[0]} <action> <service_name>", file=sys.stderr)
        print("  action: startup | shutdown", file=sys.stderr)
        print("  service_name: " + " | ".join(SERVICE_LABELS), file=sys.stderr)
        sys.exit(1)

    action = sys.argv[1]
    service_name = sys.argv[2]

    if action not in ("startup", "shutdown"):
        print(f"无效 action: {action}", file=sys.stderr)
        sys.exit(1)

    if service_name not in SERVICE_LABELS:
        print(f"无效 service_name: {service_name}", file=sys.stderr)
        sys.exit(1)

    record_service_event(action, service_name)
