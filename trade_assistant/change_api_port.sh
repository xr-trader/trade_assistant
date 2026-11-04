#!/bin/bash
# change_api_port.sh — 修改 nginx API 端口

set -euo pipefail

CONF="/etc/nginx/conf.d/takeoverorder_static.conf"

if [ ! -f "$CONF" ]; then
  echo "错误: 配置文件不存在: $CONF"
  exit 1
fi

# 读取当前 API 端口（第二个 listen）
CURRENT_PORT=$(grep 'listen [0-9]' "$CONF" | grep -v 'listen 80;' | head -1 | grep -oE '[0-9]+' || true)
if [ -z "$CURRENT_PORT" ]; then
  echo "当前为单端口模式（网页和 API 共用）"
  CURRENT_PORT=$(grep -oE 'listen [0-9]+' "$CONF" | grep -oE '[0-9]+' | head -1 || true)
fi
echo "当前 API 端口: ${CURRENT_PORT:-未知}"
echo ""

read -r -p "请输入新的 API 端口: " NEW_PORT
[ -n "$NEW_PORT" ] || { echo "端口不能为空"; exit 1; }

cp "$CONF" "${CONF}.bak.$(date +%s)"

# 检查当前是单端口还是双端口模式
LISTEN_COUNT=$(grep -c 'listen [0-9]' "$CONF" || true)
if [ "$LISTEN_COUNT" -eq 1 ]; then
  # 单端口 → 双端口：新增一个 listen 行
  sed -i "/listen [0-9]\+;/a \    listen $NEW_PORT;  # API 端口: $NEW_PORT" "$CONF"
else
  # 双端口：修改第二个 listen（非 80 的那个）
  sed -i "/listen 80;/b; s/listen [0-9]\+;/listen $NEW_PORT;/" "$CONF"
fi

nginx -t && nginx -s reload && echo "✅ API 端口已改为: $NEW_PORT"
