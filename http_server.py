"""轻量 HTTP 服务器(基于 stdlib,无外部依赖)。

支持:
- GET/POST/PUT/DELETE
- JSON 请求/响应
- 静态文件服务
- 路由参数 (/api/instances/<id>)
- SSE 流式响应
- 认证中间件(可选)
"""

import json
import mimetypes
import os
import re
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


# 不需要认证的路径(登录接口 + 静态资源)
PUBLIC_PATHS = {"/api/auth/login", "/api/auth/check", "/api/auth/config"}
STATIC_EXTENSIONS = {".html", ".css", ".js", ".ico", ".png", ".jpg", ".svg", ".woff", ".woff2", ".ttf"}


def is_public_path(path: str) -> bool:
    """判断路径是否公开(无需认证)。"""
    if path in PUBLIC_PATHS:
        return True
    # 静态文件
    ext = os.path.splitext(path)[1].lower()
    if ext in STATIC_EXTENSIONS:
        return True
    if path == "/" or path.endswith("/index.html"):
        return True
    return False


class Router:
    """简单 HTTP 路由分发器。"""

    def __init__(self):
        self.routes = {}  # (method, pattern) -> handler
        self.static_dir = None
        self.auth_check = None  # 认证检查函数: (token) -> session_dict | None


    def add_route(self, method, path, handler):
        """注册路由。path 用 :param 表示参数,如 /api/instances/:id"""
        # 转换 :param 为正则
        pattern = re.sub(r":([a-zA-Z_][a-zA-Z0-9_]*)", r"(?P<\1>[^/]+)", path)
        pattern = f"^{pattern}$"
        self.routes[(method.upper(), pattern)] = handler

    def get(self, path):
        def deco(fn):
            self.add_route("GET", path, fn)
            return fn
        return deco

    def post(self, path):
        def deco(fn):
            self.add_route("POST", path, fn)
            return fn
        return deco

    def put(self, path):
        def deco(fn):
            self.add_route("PUT", path, fn)
            return fn
        return deco

    def delete(self, path):
        def deco(fn):
            self.add_route("DELETE", path, fn)
            return fn
        return deco

    def match(self, method, path):
        """匹配路由,返回 (handler, params) 或 (None, None)。"""
        for (m, pattern), handler in self.routes.items():
            if m != method.upper():
                continue
            m2 = re.match(pattern, path)
            if m2:
                return handler, m2.groupdict()
        return None, None

    def static(self, directory):
        self.static_dir = directory

    def serve_static(self, request_path):
        """尝试服务静态文件,返回 (status, content_type, body) 或 None。"""
        if not self.static_dir:
            return None
        # 去掉前导 /
        rel = request_path.lstrip("/")
        if not rel or rel == "index.html":
            rel = "index.html"
        full = os.path.join(self.static_dir, rel)
        # 安全检查:防路径穿越
        if not os.path.abspath(full).startswith(os.path.abspath(self.static_dir)):
            return (403, "text/plain", b"Forbidden")
        if not os.path.isfile(full):
            return None
        mime = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            return (200, mime, f.read())


# ===== HTTP Handler =====

class FRPMRequestHandler(BaseHTTPRequestHandler):
    """处理所有 HTTP 请求。"""

    router: Router = None  # 由 App 类注入

    def log_message(self, fmt, *args):
        # 简化日志
        return

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return None
        return self.rfile.read(length)

    def _send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # 处理 OPTIONS (CORS)
        if method == "OPTIONS":
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Cookie")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.end_headers()
            return

        # === 认证检查(可被 auth.enabled 关闭)===
        auth_check = getattr(self.router, "auth_check", None)
        if auth_check and not is_public_path(path):
            # 提取 token
            headers = dict(self.headers)
            auth_header = headers.get("Authorization") or headers.get("authorization") or ""
            token = ""
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()
            else:
                cookie_header = headers.get("Cookie") or headers.get("cookie") or ""
                for part in cookie_header.split(";"):
                    part = part.strip()
                    if part.startswith("session="):
                        token = part[8:].strip()
                        break
            session = auth_check(token)
            if not session:
                # 未认证,返回 401 + 前端友好提示
                body = json.dumps({
                    "error": "未登录或会话已过期",
                    "login_required": True,
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(401)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Credentials", "true")
                self.end_headers()
                self.wfile.write(body)
                return

        # 匹配路由
        handler, params = self.router.match(method, path)
        if not handler:
            # 尝试静态文件
            static_result = self.router.serve_static(path)
            if static_result:
                status, ct, body = static_result
                self._send_bytes(status, ct, body)
                return
            self._send_json(404, {"error": "not found"})
            return

        # 解析请求体
        body_bytes = self._read_body()
        try:
            json_body = json.loads(body_bytes) if body_bytes else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            json_body = body_bytes

        # 构造请求上下文
        ctx = {
            "path": path,
            "query": query,
            "body": json_body,
            "params": params,
            "headers": dict(self.headers),
            "method": method,
        }

        # 调用 handler
        try:
            result = handler(ctx)
            if isinstance(result, tuple):
                # (status, data) 或 (status, {"data": ..., "stream": True})
                status, data = result
                if isinstance(data, dict) and data.get("stream"):
                    # SSE 流式响应
                    self._send_sse(data["generator"])
                else:
                    self._send_json(status, data)
            else:
                self._send_json(200, result)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

    def _send_sse(self, generator):
        """发送 Server-Sent Events 流。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            for chunk in generator:
                try:
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
        except Exception:
            pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_OPTIONS(self):
        self._handle("OPTIONS")


class ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """线程池 HTTP 服务器。"""
    allow_reuse_address = True
    daemon_threads = True


class App:
    """Flask 风格的极简替代品。"""

    def __init__(self, static_folder=None):
        self.router = Router()
        if static_folder:
            self.router.static(static_folder)
        # 把 router 注入到 handler
        FRPMRequestHandler.router = self.router

    def get(self, path):
        return self.router.get(path)

    def post(self, path):
        return self.router.post(path)

    def put(self, path):
        return self.router.put(path)

    def delete(self, path):
        return self.router.delete(path)

    def run(self, host="0.0.0.0", port=8080):
        server = ThreadingHTTPServer((host, port), FRPMRequestHandler)
        print(f"[*] FRP Manager 启动中... 监听 {host}:{port}")
        print(f"[*] 静态目录: {self.router.static_dir}")
        print(f"[*] 数据库: {os.environ.get('FRPM_DB', '/data/frpm.sqlite')}")
        server.serve_forever()
