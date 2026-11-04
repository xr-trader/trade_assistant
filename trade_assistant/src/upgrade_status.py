# -*- coding: utf-8 -*-
"""
升级状态管理模块。

在 run/upgrade_status.json 中记录升级的每一步状态，
供 /health 端点返回给前端展示。
也可通过命令行直接调用：
  python3 -m src.upgrade_status <action> [args]
  action: write | mark_pending | mark_success | mark_failed
"""

import json
import os
import sys
import time
from pathlib import Path


def _status_file():
    """获取升级状态文件路径（与调用方无关，固定在项目根 run/ 下）。"""
    project_root = Path(__file__).resolve().parent.parent
    return project_root / "run" / "upgrade_status.json"


def write_status(status: str, error: str = "", step: str = "", version: str = ""):
    """
    写入升级状态。

    status: pending | downloading | deploying | restarting | success | failed
    step: 当前步骤描述（可选）
    error: 失败原因
    version: 升级后的版本号
    """
    sf = _status_file()
    sf.parent.mkdir(parents=True, exist_ok=True)
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    data = {
        "status": status,
        "step": step,
        "started_at": now,
        "completed_at": None,
        "version": version,
        "error": error,
    }
    sf.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_status():
    """读取升级状态。文件不存在返回 None。"""
    sf = _status_file()
    if not sf.is_file():
        return None
    try:
        return json.loads(sf.read_text(encoding="utf-8"))
    except Exception:
        return None


def clean_stale_status():
    """
    如果升级状态长时间停留在进行中且未完成，标记为失败。
    返回 True 表示已清理。
    """
    data = read_status()
    if not data:
        return False
    status = data.get("status", "")
    if status in ("success", "failed"):
        return False
    # 检查开始时间是否超过 30 分钟
    started = data.get("started_at", "")
    if not started:
        return False
    import time as _time
    try:
        t = _time.mktime(_time.strptime(started, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return False
    if _time.time() - t > 1800:  # 30 分钟
        mark_failed("升级超时未完成（超过30分钟），可能升级脚本中途退出")
        return True
    return False


def mark_pending():
    """标记升级已提交，等待执行。"""
    write_status("pending", step="升级任务已提交，等待执行")


def mark_started():
    """标记升级已开始。"""
    write_status("running", step="升级脚本已启动")


def mark_success(version: str = ""):
    """标记升级成功。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    sf = _status_file()
    data = read_status() or {}
    data["status"] = "success"
    data["step"] = "升级完成"
    data["completed_at"] = now
    if version:
        data["version"] = version
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def mark_failed(error: str = "未知错误"):
    """标记升级失败。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    sf = _status_file()
    data = read_status() or {}
    data["status"] = "failed"
    data["step"] = "升级失败"
    data["completed_at"] = now
    if error:
        data["error"] = error
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="升级状态管理")
    sub = parser.add_subparsers(dest="action", required=True)

    p_write = sub.add_parser("write")
    p_write.add_argument("status", help="状态值")
    p_write.add_argument("--step", default="")
    p_write.add_argument("--error", default="")
    p_write.add_argument("--version", default="")

    sub.add_parser("mark_pending")

    p_success = sub.add_parser("mark_success")
    p_success.add_argument("version", nargs="?", default="")

    p_failed = sub.add_parser("mark_failed")
    p_failed.add_argument("error", nargs="?", default="未知错误")

    args = parser.parse_args()
    if args.action == "write":
        write_status(args.status, step=args.step, error=args.error, version=args.version)
    elif args.action == "mark_pending":
        mark_pending()
    elif args.action == "mark_success":
        mark_success(args.version)
    elif args.action == "mark_failed":
        mark_failed(args.error)

