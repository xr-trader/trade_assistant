#!/bin/bash
# kline_bot 每日重启（凌晨 3 点），由 cron 调用
# 放在 trade_assistant 目录下

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] kline_bot 每日定时重启开始"

bash "$SCRIPT_DIR/service.sh" restart kline_bot

echo "[$(date '+%Y-%m-%d %H:%M:%S')] kline_bot 每日定时重启完成"
