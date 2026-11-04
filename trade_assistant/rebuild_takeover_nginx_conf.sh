#!/bin/bash
# 重建 nginx 配置（委托给 sync_nginx_api_routes.py）
# 自动检测同级目录下的 TakeoverManageServer 并合并路由
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"

export APP_SERVER_NAME="${APP_SERVER_NAME:-}"
export NGINX_WEB_ROOT="${NGINX_WEB_ROOT:-/usr/share/nginx/takeoverorder}"
export BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
export BACKEND_PORT="${BACKEND_PORT:-8000}"
export NGINX_HTTP_PORT="${NGINX_HTTP_PORT:-80}"
export NGINX_CONF_PATH="${NGINX_CONF_PATH:-/etc/nginx/conf.d/takeoverorder_static.conf}"

# 检测同级目录下是否有 TakeoverManageServer，有则合并路由
MANAGER_DIR="$(cd "$SCRIPT_DIR/../TakeoverManageServer" 2>/dev/null && pwd || true)"
if [ -n "$MANAGER_DIR" ] && [ -f "$MANAGER_DIR/take_over_manager_app.py" ]; then
  export MANAGER_APP_ROOT="$MANAGER_DIR"
  echo "[rebuild] 检测到 TakeoverManageServer，正在合并路由"
fi

echo "[rebuild] 从后端代码提取 API 路由并生成 nginx 配置"
$PYTHON_BIN "$SCRIPT_DIR/sync_nginx_api_routes.py"

if [ "${1:-}" = "--reload" ] || [ "${1:-}" = "-r" ]; then
  echo "[rebuild] 重新加载 nginx"
  nginx -t && nginx -s reload 2>/dev/null || true
fi
