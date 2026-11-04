#!/usr/bin/env python3
"""
API 路由同步工具 -- 从后端代码动态提取路由，生成 nginx 配置

支持同时读取 trade_assistant 和 TakeoverManageServer 的路由，
生成统一的 nginx 配置文件，共用一个 listen 80 端口。

用法：
  python3 sync_nginx_api_routes.py                       # 仅 trade_assistant 路由
  MANAGER_APP_ROOT=../TakeoverManageServer python3 ...    # 合并两个项目的路由
  python3 sync_nginx_api_routes.py --routes               # 仅打印路由列表
  python3 sync_nginx_api_routes.py --check                # 仅检查是否需要更新
"""

import ast
import re
import sys
import os


# ── 配置 ───────────────────────────────────────────────────────────

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
BACKEND_APP_FILE = os.path.join(APP_ROOT, "take_over_order_app.py")
NGINX_CONF_PATH = "/etc/nginx/conf.d/takeoverorder_static.conf"
BACKUP_DIR = "/etc/nginx/conf.d/backups"

# 环境变量：TakeoverManageServer 所在目录（可选，设置后合并路由）
MANAGER_APP_ROOT_ENV = os.environ.get("MANAGER_APP_ROOT", "")

# trade_assistant 特殊路由
# /download_kline 已停用（K 线不再从其他服务器拉取），不再生成下载路由
ORDER_SPECIAL_ROUTES = {
    "/ws/market": "websocket",
}
ORDER_CORS_ROUTES = {"/risk_preview"}


# ── 路由提取 ──────────────────────────────────────────────────────


def extract_routes_from_app(app_file):
    """从 FastAPI 应用文件提取所有路由"""
    if not os.path.exists(app_file):
        return []

    with open(app_file, "r") as f:
        source = f.read()

    tree = ast.parse(source)
    routes = []

    def _get_str_value(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if hasattr(ast, "Str") and isinstance(node, ast.Str):
            return node.s
        return None

    for node in ast.walk(tree):
        # ── 优先查找 REGISTERED_ROUTES 常量（精确匹配）──
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "REGISTERED_ROUTES":
                    if isinstance(node.value, ast.Dict):
                        for key_node, val_node in zip(node.value.keys, node.value.values):
                            method = _get_str_value(key_node)
                            if method and isinstance(val_node, ast.List):
                                for elt in val_node.elts:
                                    route = _get_str_value(elt)
                                    if route and route.startswith("/"):
                                        routes.append(route)

        # ── app.post("/path") / app.get("/path") ──
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (node.func.attr in ("post", "get", "websocket", "put", "delete", "patch") and
                isinstance(node.func.value, ast.Name) and
                node.func.value.id == "app"):
                if node.args:
                    route = _get_str_value(node.args[0])
                    if route and route.startswith("/"):
                        routes.append(route)

        # ── app.post("/path")(handler) (嵌套调用) ──
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Call):
            inner = node.func
            if (isinstance(inner.func, ast.Attribute) and
                inner.func.attr in ("post", "get", "websocket") and
                isinstance(inner.func.value, ast.Name) and
                inner.func.value.id == "app"):
                if inner.args:
                    route = _get_str_value(inner.args[0])
                    if route and route.startswith("/"):
                        routes.append(route)

        # ── 路由列表元组 ("/path", handler) ──
        elif isinstance(node, ast.Tuple) and len(node.elts) >= 1:
            route = _get_str_value(node.elts[0])
            if route and route.startswith("/"):
                routes.append(route)

    return sorted(set(routes))


# ── nginx 配置生成 ────────────────────────────────────────────────


def _generate_order_locations(routes):
    """生成 trade_assistant 的 location 块"""
    ws_routes = [r for r in routes if r in ORDER_SPECIAL_ROUTES and ORDER_SPECIAL_ROUTES[r] == "websocket"]
    cors_routes = [r for r in routes if r in ORDER_CORS_ROUTES]
    normal_routes = [r for r in routes if r not in ORDER_SPECIAL_ROUTES and r not in ORDER_CORS_ROUTES]

    UPSTREAM = "takeover_order_backend"
    TPL = "\n        proxy_pass http://{};\n        proxy_http_version 1.1;\n        proxy_set_header Host $host;\n        proxy_set_header X-Real-IP $remote_addr;\n        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n        proxy_set_header X-Forwarded-Proto $scheme;"
    TIMEOUT_LONG = "\n        proxy_read_timeout 86400;\n        proxy_send_timeout 86400;"
    TIMEOUT_SHORT = "\n        proxy_read_timeout 300;\n        proxy_send_timeout 300;"

    out = ""

    # CORS 路由
    for route in cors_routes:
        out += f"""
    location = {route} {{
        if ($request_method = OPTIONS) {{
            add_header Access-Control-Allow-Origin $http_origin always;
            add_header Access-Control-Allow-Methods "GET, POST, OPTIONS" always;
            add_header Access-Control-Allow-Headers "Authorization, Content-Type, X-Requested-With" always;
            add_header Content-Length 0;
            add_header Content-Type text/plain;
            return 204;
        }}
{TPL.format(UPSTREAM)}{TIMEOUT_LONG}
    }}"""

    # WebSocket 路由
    for route in ws_routes:
        out += f"""
    location = {route} {{
{TPL.format(UPSTREAM)}{TIMEOUT_LONG}
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
    }}"""

    # 普通 API 路由
    if normal_routes:
        escaped = []
        for r in normal_routes:
            e = r.replace("/", r"\/").replace(".", r"\.").replace("-", r"\-")
            escaped.append(e)
        pattern = "|".join(escaped)
        out += f"""
    # trade_assistant API 路由（自动生成）
    location ~ ^({pattern})$ {{
{TPL.format(UPSTREAM)}{TIMEOUT_LONG}
    }}"""

    return out


def _generate_manager_locations(routes):
    """生成 TakeoverManageServer 的 location 块"""
    UPSTREAM = "takeover_manager_backend"

    # 分离路径参数路由和普通路由
    param_routes = [r for r in routes if re.search(r"\{[^}]+\}", r)]
    normal_routes = [r for r in routes if r not in param_routes]

    TPL = "\n        proxy_pass http://{};\n        proxy_http_version 1.1;\n        proxy_set_header Host $host;\n        proxy_set_header X-Real-IP $remote_addr;\n        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n        proxy_set_header X-Forwarded-Proto $scheme;"
    TIMEOUT_DEFAULT = "\n        proxy_read_timeout 60;\n        proxy_send_timeout 60;"
    TIMEOUT_LONG = "\n        proxy_read_timeout 300;\n        proxy_send_timeout 300;"

    out = ""

    # 路径参数路由
    for route in param_routes:
        regex = route
        regex = re.sub(r"\{[^:}]+:path\}", r"(.+)", regex)
        regex = re.sub(r"\{[^}]+\}", r"([^/]+)", regex)
        out += f"""
    location ~ ^{regex}$ {{
{TPL.format(UPSTREAM)}{TIMEOUT_LONG}
    }}"""

    # 普通路由
    if normal_routes:
        escaped = []
        for r in normal_routes:
            e = r.replace("/", r"\/").replace(".", r"\.").replace("-", r"\-")
            escaped.append(e)
        pattern = "|".join(escaped)
        out += f"""
    # TakeoverManageServer API 路由（自动生成）
    location ~ ^({pattern})$ {{
{TPL.format(UPSTREAM)}{TIMEOUT_DEFAULT}
    }}"""

    return out


def generate_nginx_config(order_routes, manager_routes, server_name="",
                          web_root="/usr/share/nginx/takeoverorder",
                          order_host="127.0.0.1", order_port="8000",
                          manager_host="127.0.0.1", manager_port="9000",
                          http_port="80", api_port=None,
                          enable_downloads=False, enable_trade=False):
    if api_port is None:
        api_port = http_port
    """生成统一的 nginx 配置"""

    config = f"""# trade_assistant + TakeoverManageServer 统一 Nginx 配置
# 最后生成时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
# trade_assistant 路由数: {len(order_routes)}
# TakeoverManageServer 路由数: {len(manager_routes)}
# 网页端口: {http_port}  |  API 端口: {api_port}
# 请勿手动修改，修改后会被覆盖

map $http_upgrade $connection_upgrade {{
    default upgrade;
    '' close;
}}

# ---------- 后端 upstream ----------
upstream takeover_order_backend {{
    server {order_host}:{order_port};
}}
"""

    if manager_routes:
        config += f"upstream takeover_manager_backend {{\n    server {manager_host}:{manager_port};\n}}\n"

    # ── 网页端口（纯静态文件） ──
    # ⚠️ 下面这个配置块是**普通（非 raw）f-string** ⇒ 块内正则转义必须写**双**反斜杠
    #    （`\\.` ⇒ 输出 `\.` ✓）；写单个 `\.` 会触发
    #    `SyntaxWarning: invalid escape sequence` ✗（2026-10-11 修复 ✓）。
    #    ⚠️ 说明文字**不要写进这个字符串内部** ✗ —— 字符串里的 `#` 不是注释，
    #    里面的反斜杠同样按转义处理 ⇒ 既报警告、又混进生成的 nginx 配置 ✓
    config += f"""
server {{
    listen {http_port};
    server_name {server_name or "_"};

    # 安全响应头
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;

    gzip on;
    gzip_static on;
    gzip_vary on;
    gzip_min_length 1024;
    gzip_comp_level 5;
    gzip_proxied any;
    gzip_types text/plain text/css text/xml text/javascript application/javascript application/json application/xml image/svg+xml font/woff2;

    root {web_root}/dist_temp;
    index index.html;
    client_max_body_size 50m;
    error_page 404 /index.html;

    # 屏蔽 API 文档路径
    location ~ ^/(docs|redoc|openapi\\.json)$ {{
        return 404;
    }}

    location = / {{
        try_files /index.html =404;
        # ⚠️ 2026-11-03 修（真机实测：`curl -I http://<host>/` **完全没有** Cache-Control）：
        #   `/` 命中这个精确匹配块 ⇒ 下面那条 `~* ^/(index\\.html|version\\.json)$` 正则**不生效** ✗
        #   ⇒ 浏览器会按"启发式缓存"（约 (Date − Last-Modified) 的 10%）自行缓存根 HTML，
        #     发布后客户端可能仍跑旧页面 ✗（与 2026-10-09「前端版本时不时丢了」同型问题）
        expires -1;
        add_header Cache-Control "no-store, must-revalidate";
    }}

    location ^~ /assets/ {{
        # ⚠️ 2026-11-03 修：必须是 `^~`（前缀匹配即止，不再尝试正则）。
        #   否则 `/assets/entry.xxxx.css` 会被后面 `\\.(ico|…|js|css)$` 那条**正则** location 接走
        #   ⇒ 实际只给 `expires 1d`，这里的 30d immutable 从未生效 ✗（线上实测：max-age=86400）
        try_files $uri =404;
        expires 30d;
        add_header Cache-Control "public, immutable";
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "SAMEORIGIN" always;
        access_log off;
    }}

    # ⚠️ index.html 与 version.json **绝不缓存**（用户 2026-10-09 反馈「前端版本时不时又丢了」）：
    #   · index.html 被缓存 ⇒ 发布后客户端仍在跑旧 HTML ⇒ 里面的 <meta name="app-version">
    #     是旧值/不存在 ⇒ 界面「前端版本」时有时无 ✗
    #   · version.json 被缓存 ⇒ 套壳/桌面的热更新判断读到**旧版本** ⇒ 该更新却不更新 ✗
    # 必须放在下面那条「通用扩展名」规则**之前**（nginx 按顺序匹配正则 location ✓）
    location ~* ^/(index\\.html|version\\.json)$ {{
        try_files $uri =404;
        expires -1;
        add_header Cache-Control "no-store, must-revalidate";
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "SAMEORIGIN" always;
    }}

    location ~* \\.(ico|txt|xml|svg|png|jpg|jpeg|gif|woff|woff2|ttf|eot|js|css)$ {{
        try_files $uri =404;
        expires 1d;
        add_header Cache-Control "public";
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "SAMEORIGIN" always;
    }}
"""

    if enable_downloads:
        config += f"""
    # /downloads/ 静态下载目录（仅分发服务器开启，见 .keep_downloads 标记）
    location ^~ /downloads/ {{
        alias {web_root}/downloads/;
        try_files $uri =404;
        expires 1d;
        add_header Cache-Control "public";
        add_header Content-Disposition "attachment";
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "SAMEORIGIN" always;
    }}
"""
    if enable_trade:
        config += f"""
    # ★ 2026-10-11：/trade/ 静态分发目录（升级包：trade_assistant.tar.gz / install.sh / upgrade.sh / version.json）
    #   与 /downloads/ 同范式，**仅分发服务器开启**（见 trade/.keep_trade 标记 ✓）
    #   为什么要它：机器人（小二端）的「检查更新/升级」按 manager_host 拼 `http://<host>:80/trade/...`
    #   ⇒ 144 接管 manager_host 后必须也能提供该目录，否则点升级取不到包 ✗
    location ^~ /trade/ {{
        # ⚠️ 2026-10-11 真机踩到：`alias` + `try_files` 组合在这台 nginx 上**不生效** ✗
        #   （请求落到 `location /` 的 SPA 回退 ⇒ 下载到的是 index.html ✗）
        #   ⇒ 改用 **root** 写法（前缀自动拼 web_root ✓，无需 try_files ✓ 最稳 ✓）
        root {web_root};
        add_header Cache-Control "no-store";
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "SAMEORIGIN" always;
    }}
"""
    if api_port == http_port:
        # 共用端口：API 路由放在网页 block 内
        config += f"""\
{_generate_order_locations(order_routes)}
{_generate_manager_locations(manager_routes)}
    location / {{
        try_files $uri $uri/ /index.html;
        # SPA 回退返回的也是 index.html ⇒ 同样不能缓存（否则深链刷新会拿到旧 HTML ✗）
        add_header Cache-Control "no-store, must-revalidate";
    }}
}}
"""
    else:
        # 网页 block 结束（不含 API 路由）
        config += f"""\
    location / {{
        try_files $uri $uri/ /index.html;
        # SPA 回退返回的也是 index.html ⇒ 同样不能缓存（否则深链刷新会拿到旧 HTML ✗）
        add_header Cache-Control "no-store, must-revalidate";
    }}
}}
"""
        # API 代理端口：纯反向代理，不提供静态文件
        config += f"""
server {{
    listen {api_port};
    server_name {server_name or "_"};

    client_max_body_size 50m;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "SAMEORIGIN" always;


{_generate_order_locations(order_routes)}
{_generate_manager_locations(manager_routes)}
    location / {{
        if ($request_method = OPTIONS) {{
            add_header Access-Control-Allow-Origin $http_origin always;
            add_header Access-Control-Allow-Methods "GET, POST, OPTIONS" always;
            add_header Access-Control-Allow-Headers "Authorization, Content-Type, X-Requested-With" always;
            add_header Content-Length 0;
            add_header Content-Type text/plain;
            return 204;
        }}
        return 404;
    }}
}}
"""
    return config


# ── 备份与持久化 ──────────────────────────────────────────────────


def backup_current_config():
    if not os.path.exists(NGINX_CONF_PATH):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    import shutil
    timestamp = __import__('datetime').datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = os.path.join(BACKUP_DIR, f"takeoverorder_static.conf.bak.{timestamp}")
    shutil.copy2(NGINX_CONF_PATH, backup_path)
    print(f"[BACKUP] {backup_path}")


# ── 主入口 ────────────────────────────────────────────────────────


def main(args):
    # 读取 trade_assistant 路由
    order_routes = extract_routes_from_app(BACKEND_APP_FILE)

    # 读取 TakeoverManageServer 路由（可选）
    manager_routes = []
    manager_app_file = ""
    if MANAGER_APP_ROOT_ENV:
        manager_app_file = os.path.join(MANAGER_APP_ROOT_ENV, "take_over_manager_app.py")
        manager_source = extract_routes_from_app(manager_app_file)
        # 过滤掉 trade_assistant 中已存在的路由，避免冲突
        manager_routes = [r for r in manager_source if r not in order_routes]

    if "--routes" in args:
        print(f"trade_assistant: {len(order_routes)} 个路由:")
        for r in order_routes:
            print(f"  [order] {r}")
        if manager_routes:
            print(f"\nTakeoverManageServer: {len(manager_routes)} 个路由:")
            for r in manager_routes:
                print(f"  [manager] {r}")
        else:
            print("\nTakeoverManageServer: 未配置或没有独立路由")
        return 0

    server_name = os.environ.get("APP_SERVER_NAME", "")
    web_root = os.environ.get("NGINX_WEB_ROOT", "/usr/share/nginx/takeoverorder")
    order_host = os.environ.get("BACKEND_HOST", "127.0.0.1")
    order_port = os.environ.get("BACKEND_PORT", "8000")
    manager_host = os.environ.get("MANAGER_BACKEND_HOST", "127.0.0.1")
    manager_port = os.environ.get("MANAGER_BACKEND_PORT", "9000")
    http_port = os.environ.get("NGINX_HTTP_PORT", "80")
    api_port = os.environ.get("NGINX_API_PORT", http_port)
    nginx_conf = os.environ.get("NGINX_CONF_PATH", NGINX_CONF_PATH)

    # /downloads/ 静态目录开关（**2026-10-11 起默认关闭**：用户裁定「downloads 退出舞台」✓）
    #   ① **不再自动检测** `.keep_downloads` 标记 ✗ —— 旧线已退役，标记会把 /downloads/
    #      在每次重建 nginx 配置时又带回来（那是我们要根除的入口 ✗）
    #   ② 只在**显式**设 `NGINX_ENABLE_DOWNLOADS=1` 时才开（留给历史机器临时兜底 ✓）
    #   ⇒ 新生成/重建的配置默认不含 /downloads/ ✓
    enable_downloads = os.environ.get("NGINX_ENABLE_DOWNLOADS") == "1"

    # ★ 2026-10-11：/trade/ 静态分发（升级包）—— 与 /downloads/ 同两级判定 ✓
    #   为什么要它：机器人的「检查更新/升级」按 manager_host 拼 `http://<host>:80/trade/...`
    #   ⇒ 144 接管 manager_host 后必须也能提供该目录，否则点升级取不到包 ✗
    enable_trade_env = os.environ.get("NGINX_ENABLE_TRADE")
    if enable_trade_env is not None:
        enable_trade = enable_trade_env == "1"
    else:
        enable_trade = os.path.exists(os.path.join(web_root, "trade/.keep_trade"))

    # 从现有 nginx 配置中读取端口，避免升级时回退到默认值
    if not os.environ.get("NGINX_HTTP_PORT") or not os.environ.get("NGINX_API_PORT"):
        try:
            ports = set()
            with open(nginx_conf, "r") as f:
                for line in f:
                    m = re.search(r"^\s*listen\s+(\d+)", line)
                    if m:
                        ports.add(m.group(1))
            # 80 固定是网页端口，其他是 API 端口
            if "80" in ports:
                http_port = "80"
                ports.discard("80")
                if ports:
                    api_port = ports.pop()
        except (FileNotFoundError, IOError, OSError):
            pass

    config = generate_nginx_config(
        order_routes=order_routes,
        manager_routes=manager_routes,
        server_name=server_name,
        web_root=web_root,
        order_host=order_host,
        order_port=order_port,
        manager_host=manager_host,
        manager_port=manager_port,
        http_port=http_port,
        api_port=api_port,
        enable_downloads=enable_downloads,
        enable_trade=enable_trade,
    )

    if "--check" in args:
        if os.path.exists(nginx_conf):
            with open(nginx_conf, "r") as f:
                current = f.read()
            if current == config:
                print("[OK] nginx 配置已是最新")
                return 0
            else:
                print("[DIFF] nginx 配置需要更新")
                return 1
        else:
            print("[DIFF] nginx 配置不存在")
            return 1

    # 写入配置
    backup_current_config()
    os.makedirs(os.path.dirname(nginx_conf), exist_ok=True)
    with open(nginx_conf, "w") as f:
        f.write(config)
    print(f"[UPDATED] {nginx_conf}")
    print(f"  trade_assistant: {len(order_routes)} 个路由 → {order_host}:{order_port}")
    if manager_routes:
        print(f"  TakeoverManageServer: {len(manager_routes)} 个路由 → {manager_host}:{manager_port}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
