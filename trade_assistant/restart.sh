#!/bin/bash

set -euo pipefail

PYTHON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 启动来源：restart.sh 属**计划内重启** ⇒ 后端不生成/不推送应急刷新密钥
# （用户 2026-10-28：只有被保活/开机拉起时才推送；用 :- 保留升级脚本传下来的 upgrade）
export XIAOER_START_REASON="${XIAOER_START_REASON:-restart}"

bash "$PYTHON_DIR/stop.sh" "${1:-all}"
bash "$PYTHON_DIR/boot.sh" "${1:-all}"
