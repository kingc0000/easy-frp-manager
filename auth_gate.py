#!/usr/bin/env python3
"""
auth_gate.py — frpc 侧应用层鉴权网关（独立进程）

设计定位
========
easy-frp-manager 的「鉴权网关」在 frpc 侧运行：外部请求经 frps 隧道到达 frpc，
frpc 按配置把流量打到本网关监听的 auth_gate_port，本网关完成应用层鉴权后，
再转发到真实后端 local_ip:local_port。这样「后端服务完全无需改动」即可获得
统一的访问控制层。

支持的鉴权方式（auth_mode，按需配置、可任选其一或组合）
========================================================
  basic        HTTP Basic Auth（用户名 + 密码，密码 PBKDF2 哈希落库，兼容
               请求头 Authorization: Basic base64(user:pass)）
  bearer       Bearer Token（Authorization: Bearer <token>）
  apikey       API Key（自定义请求头，默认 X-API-Key）
  url_token    URL 参数 Token（?token=xxx，参数名可配置，默认 token）
               —— 兼容不支持携带请求头的客户端（如 <img>、<script>、SSE、
               websocket 握手、某些静态资源请求等），必须支持
  any          任意一种命中即放行（token 列表 + basic + header + url 参数）
  none         不鉴权，直接转发（用于临时放行）

凭证来源（全部支持）
====================
  1. Authorization 头：Bearer <token> / Basic base64(user:pass)
  2. 自定义请求头（apikey 模式，可配置头名）
  3. URL 查询参数（url_token 模式，可配置参数名）—— 前端/第三方必须支持
  4. 请求体（application/json 或表单）中的 token 字段
  5. Cookie（可选，token 字段）
  6. Basic 模式的 URL 内嵌凭据（http://user:pass@host/...，用于不支持头的场景）

Token 过期（auth_token_expiry）
==============================
  规则级全局过期时间（YYYY-MM-DDTHH:MM:SS，可选）。到达后所有 token 失效；
  留空 = 永不过期。另支持每个 token 行内自带过期（token|YYYY-MM-DD HH:MM:SS）。

例外路径（auth_bypass_paths）
============================
  无需鉴权的路径前缀列表（如 /health、/public）。支持 * 通配（fnmatch）。

传输加密（frps 侧加密 → frpc 侧解密）
======================================
  本网关位于 frps↔frpc 隧道之后（frp 的 transport.useEncryption 已在隧道层
  加密）。本模块额外支持：
    backend_tls: bool   —— 到真实后端启用 TLS（frpc 侧解密后以 TLS 转发给后端）
    backend_tls_insecure: bool —— 跳过后端证书校验（自签名场景）
  这样实现「frps 侧加密（隧道 + 网关校验），frpc 侧解密验证（TLS/凭证）」。

进程模型
========
  由 frp_ops.start_auth_gate() 启动；SIGHUP 重载规则文件；SIGTERM/SIGINT 退出。
  规则文件：<FRPM_CONFIG_DIR>/auth-gate.json，结构 {proxy_id: rule}。
  审计日志：<FRPM_LOG_DIR>/auth-gate-audit.log（供 aggregate_stats 聚合）。
"""

import base64
import fnmatch
import hashlib
import hmac
import json
import os
import signal
import socket
import ssl
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, parse_qsl, unquote, urlparse
import http.client

# --------------------------------------------------------------------------
# 常量与环境
# --------------------------------------------------------------------------
CONFIG_DIR = Path(os.environ.get("FRPM_CONFIG_DIR", "/data/frpm-configs"))
LOG_DIR = Path(os.environ.get("FRPM_LOG_DIR", "/var/lib/frpm/logs"))
RULE_FILE = CONFIG_DIR / "auth-gate.json"
AUDIT_LOG = LOG_DIR / "auth-gate-audit.log"

MAX_BODY = 10 * 1024 * 1024  # 10MB 请求体上限（超过直接转发不做缓存）
PBKDF2_ITERATIONS = 100_000

# 进程级状态：gate_port -> rule
_rules: dict = {}
_rules_lock = threading.Lock()
# 内存统计（进程内实时），持久化到审计日志
_stats_lock = threading.Lock()
_stats = {}
_shutting_down = False


# --------------------------------------------------------------------------
# 规则加载
# --------------------------------------------------------------------------
def _load_rule_file() -> dict:
    """读取 auth-gate.json，返回 {proxy_id: rule}；异常返回 {}。"""
    try:
        if not RULE_FILE.exists():
            return {}
        with open(RULE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def reload_rules() -> None:
    """重新加载规则（SIGHUP / 启动时调用）。"""
    global _rules
    data = _load_rule_file()
    # 只保留有效规则
    valid = {}
    for proxy_id, rule in data.items():
        if isinstance(rule, dict) and rule.get("gate_port"):
            try:
                valid[int(proxy_id)] = rule
            except (TypeError, ValueError):
                continue
    with _rules_lock:
        _rules = valid
    print(f"[auth-gate] 已加载 {len(valid)} 条规则", flush=True)


def get_rules() -> dict:
    with _rules_lock:
        return dict(_rules)


# --------------------------------------------------------------------------
# 密码哈希（与 auth.py 一致，独立实现避免耦合）
# --------------------------------------------------------------------------
def hash_password(password: str, salt_hex: str = None) -> str:
    """PBKDF2-SHA256 哈希，返回 hex。"""
    if salt_hex is None:
        salt = os.urandom(16)
    else:
        salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return dk.hex()


def verify_password(password: str, password_hash: str, salt_hex: str) -> bool:
    try:
        computed = hash_password(password, salt_hex)
        return hmac.compare_digest(computed, password_hash)
    except Exception:
        return False


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def _parse_expiry(value) -> float:
    """解析过期时间字符串为 epoch 秒；无法解析返回 None。"""
    if not value:
        return None
    s = str(value).strip()
    # 支持 ISO 与常用格式
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    try:
        return float(s)
    except ValueError:
        return None


def _token_row_valid(token_row: str, now: float, global_expiry: float) -> bool:
    """校验单行 token 是否有效（支持 'token|expiry' 每行自带过期）。"""
    token = token_row
    local_expiry = None
    if "|" in token_row:
        token, _, exp = token_row.partition("|")
        local_expiry = _parse_expiry(exp.strip())
    if not token:
        return False
    if local_expiry is not None and now > local_expiry:
        return False
    if global_expiry is not None and now > global_expiry:
        return False
    return True


def _token_has_expiry(token_row: str) -> bool:
    return "|" in token_row


def _path_matches(path: str, patterns) -> bool:
    """路径是否匹配任意一个 bypass 模式（前缀或 fnmatch 通配）。"""
    if not patterns:
        return False
    p = path or "/"
    for pat in patterns:
        pat = (pat or "").strip()
        if not pat:
            continue
        if pat.startswith("/") and p.startswith(pat):
            return True
        if "*" in pat and fnmatch.fnmatch(p, pat):
            return True
    return False


def _safe_compare(a: str, b: str) -> bool:
    """常量时间字符串比较。"""
    if a is None or b is None:
        return False
    return hmac.compare_digest(str(a).encode(), str(b).encode())


def _token_in_list(token: str, token_list, now: float, global_expiry: float):
    """在 token 列表中查找匹配项；返回 (matched_row, expired?) 或 (None, False)。"""
    if not token:
        return None, False
    for row in token_list or []:
        row = str(row).strip()
        if not row:
            continue
        if not _token_row_valid(row, now, global_expiry):
            # 该行过期：若 token 本身相等，返回过期标志
            if _safe_compare(token, row.split("|")[0]):
                return row, True
            continue
        if _safe_compare(token, row.split("|")[0]):
            return row, False
    return None, False


# --------------------------------------------------------------------------
# 凭证提取
# --------------------------------------------------------------------------
def extract_credentials(headers, query, body, path):
    """从请求中提取候选凭证，返回 dict。

    headers: dict（大小写不敏感访问）
    query:   dict（parse_qs 结果）
    body:    原始 bytes 或已解析 dict（None 表示无）
    path:    URL 路径（不含查询串）
    """
    creds = {
        "token": "",
        "basic_user": "",
        "basic_pass": "",
        "header_token": "",
        "url_token": "",
        "body_token": "",
    }
    # --- Authorization 头 ---
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if auth.startswith("Bearer "):
        creds["header_token"] = auth[7:].strip()
        creds["token"] = creds["token"] or creds["header_token"]
    elif auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:].strip()).decode("utf-8", "ignore")
            user, _, pw = decoded.partition(":")
            creds["basic_user"] = user
            creds["basic_pass"] = pw
        except Exception:
            pass

    # --- URL 内嵌凭据 (http://user:pass@host) ---
    # 由调用方预处理后传入 query 的 _auth_url_user/_auth_url_pass

    # --- 自定义头 / cookie（在调用处按规则补充）---
    return creds


def _apply_rule_headers(headers, query, body, rule, path):
    """按规则补充：自定义 API 头、URL 参数、Cookie、URL 内嵌凭据。"""
    creds = extract_credentials(headers, query, body, path)

    token_header = rule.get("token_header") or "X-API-Key"
    # 自定义头（大小写不敏感）
    hv = headers.get(token_header.lower()) or headers.get(token_header)
    if hv:
        creds["header_token"] = creds["header_token"] or hv
        creds["token"] = creds["token"] or hv

    # URL 参数
    token_param = rule.get("token_param") or "token"
    qv = query.get(token_param)
    if qv:
        creds["url_token"] = qv[0]
        creds["token"] = creds["token"] or qv[0]

    # URL 内嵌凭据（用户:密码 形式，兼容无头场景）
    url_user = query.get("_auth_url_user")
    url_pass = query.get("_auth_url_pass")
    if url_user:
        creds["basic_user"] = creds["basic_user"] or url_user[0]
        creds["basic_pass"] = creds["basic_pass"] or (url_pass[0] if url_pass else "")

    # Cookie
    cookie = headers.get("cookie") or headers.get("Cookie") or ""
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("token=") and not creds["token"]:
            creds["token"] = part[6:].strip()

    # 请求体（JSON 或表单）中的 token / basic
    if isinstance(body, dict):
        bt = body.get("token")
        if bt and not creds["token"]:
            creds["body_token"] = str(bt)
            creds["token"] = str(bt)
        bu = body.get("username") or body.get("user")
        bp = body.get("password") or body.get("pass")
        if bu and not creds["basic_user"]:
            creds["basic_user"] = str(bu)
            creds["basic_pass"] = str(bp or "")

    return creds


# --------------------------------------------------------------------------
# 核心鉴权：check_token(rule, creds) —— 与 app.py 的契约一致
# --------------------------------------------------------------------------
def check_token(rule: dict, creds: dict):
    """校验凭证是否命中规则。

    返回 (ok: bool, reason: str, matched: str|None)
      ok     —— 是否放行
      reason —— 'ok' / 失败原因（用于统计与日志）
      matched—— 命中的凭证来源（basic/bearer/apikey/url_token/any/bypass）
    """
    if not rule:
        return False, "no_rule", None

    mode = rule.get("mode") or "none"
    realm = rule.get("realm") or "Restricted"
    tokens = rule.get("tokens") or []
    token_header = rule.get("token_header") or "X-API-Key"
    token_param = rule.get("token_param") or "token"
    global_expiry = _parse_expiry(rule.get("token_expiry"))
    now = time.time()

    # 例外路径（在调用方处理，但这里也兼容直接路径）
    path = creds.get("path") or "/"
    if _path_matches(path, rule.get("bypass_paths") or []):
        return True, "bypass", "bypass"

    basic = rule.get("basic") or {}
    basic_user = basic.get("user") or ""
    basic_hash = basic.get("pass_hash") or ""
    basic_salt = basic.get("salt") or ""

    # 提取的凭证（调用方已通过 _apply_rule_headers 补齐）
    token = creds.get("token") or ""
    header_token = creds.get("header_token") or ""
    url_token = creds.get("url_token") or ""
    body_token = creds.get("body_token") or ""
    b_user = creds.get("basic_user") or ""
    b_pass = creds.get("basic_pass") or ""

    # --- 按模式校验 ---
    if mode == "none":
        return True, "ok", "none"

    if mode == "basic":
        if b_user and basic_user and _safe_compare(b_user, basic_user) and \
           verify_password(b_pass, basic_hash, basic_salt):
            return True, "ok", "basic"
        return False, "basic_fail", None

    if mode == "bearer":
        # 支持 Authorization: Bearer 或 header_token
        cand = header_token or token
        matched_row, expired = _token_in_list(cand, tokens, now, global_expiry)
        if matched_row is not None:
            if expired:
                return False, "token_expired", None
            return True, "ok", "bearer"
        return False, "token_fail", None

    if mode == "apikey":
        cand = header_token
        matched_row, expired = _token_in_list(cand, tokens, now, global_expiry)
        if matched_row is not None:
            if expired:
                return False, "token_expired", None
            return True, "ok", "apikey"
        return False, "token_fail", None

    if mode == "url_token":
        cand = url_token or token or header_token
        matched_row, expired = _token_in_list(cand, tokens, now, global_expiry)
        if matched_row is not None:
            if expired:
                return False, "token_expired", None
            return True, "ok", "url_token"
        return False, "token_fail", None

    if mode == "any":
        # 任一命中即放行
        # 1) token（来自 header/url/body）
        cand = token or header_token or url_token or body_token
        matched_row, expired = _token_in_list(cand, tokens, now, global_expiry)
        if matched_row is not None:
            return (False, "token_expired", None) if expired else (True, "ok", "any:token")
        # 2) basic
        if b_user and basic_user and _safe_compare(b_user, basic_user) and \
           verify_password(b_pass, basic_hash, basic_salt):
            return True, "ok", "any:basic"
        # 3) apikey 头（若 token_header 单独提供）
        hv = creds.get("header_token")
        matched_row, expired = _token_in_list(hv, tokens, now, global_expiry)
        if matched_row is not None:
            return (False, "token_expired", None) if expired else (True, "ok", "any:apikey")
        return False, "any_fail", None

    # 未知模式：默认放行（保守起见不挡）
    return True, "ok", "none"


# --------------------------------------------------------------------------
# 审计统计
# --------------------------------------------------------------------------
def _record(gate_port: int, ok: bool, reason: str):
    """记录一次鉴权结果到内存 + 审计日志。"""
    ts = time.time()
    with _stats_lock:
        st = _stats.setdefault(str(gate_port), {"ok": 0, "fail": 0, "last_ts": ts, "reasons": {}})
        key = "ok" if ok else "fail"
        st[key] += 1
        st["last_ts"] = ts
        st["reasons"][reason] = st["reasons"].get(reason, 0) + 1
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps({"ts": int(ts), "port": gate_port, "ok": ok, "reason": reason}) + "\n")
    except Exception:
        pass


def aggregate_stats() -> dict:
    """从审计日志聚合统计（供 app.py /api/auth-gate/stats）。"""
    result = {}
    try:
        if AUDIT_LOG.exists():
            with open(AUDIT_LOG) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    port = str(rec.get("port", ""))
                    st = result.setdefault(port, {"ok": 0, "fail": 0, "last_ts": 0, "reasons": {}})
                    ok = bool(rec.get("ok"))
                    st["ok" if ok else "fail"] += 1
                    st["last_ts"] = max(st["last_ts"], int(rec.get("ts", 0)))
                    r = rec.get("reason", "unknown")
                    st["reasons"][r] = st["reasons"].get(r, 0) + 1
    except Exception:
        pass
    # 合并内存统计（最新）
    with _stats_lock:
        for port, st in _stats.items():
            r = result.setdefault(port, {"ok": 0, "fail": 0, "last_ts": 0, "reasons": {}})
            r["ok"] = max(r["ok"], st["ok"])
            r["fail"] = max(r["fail"], st["fail"])
            r["last_ts"] = max(r["last_ts"], st["last_ts"])
            for k, v in st["reasons"].items():
                r["reasons"][k] = max(r["reasons"].get(k, 0), v)
    return result


# --------------------------------------------------------------------------
# 后端转发（HTTP 代理）
# --------------------------------------------------------------------------
def _forward_request(rule: dict, method: str, path: str, headers: dict, body: bytes,
                     strip_params: list = None, inject_upstream: bool = True) -> tuple:
    """把请求转发到真实后端，返回 (status, reason_phrase, resp_headers, resp_body)。

    strip_params: 需要从 query 中剥离的参数名（如 url_token 的 token）。
    inject_upstream: 是否注入上游凭证（cookie 透传放行时为 False，
        因为后端已用会话 cookie 认证，无需再注入 launch token，否则后端会走 token 分支）。
    """
    backend_host = rule.get("backend_host") or "127.0.0.1"
    backend_port = int(rule.get("backend_port") or 0)
    if not backend_port:
        return 502, "Bad Gateway", {}, b"no backend_port"

    # 重组路径（剥离 token 参数）。
    # 注意：不能用 parse_qs 重组 query——DSH 等后端的 combo loader 用 "??" 前缀
    # （如 /plugins/??a.js,b.js&rev=xxx）承载模块列表，parse_qs 会把 "?a.js,b.js"
    # 当作键解析，重组时多出 "="，破坏 URL。因此保留原始 query 字符串，
    # 剥离/注入都用字符串操作。
    parsed = urlparse(path)
    raw_query = parsed.query  # 原始 query（不含 '?'），如 "token=abc" 或 "?@deepseek-ai/a.js&rev=x"
    # 检测 combo 格式：query 以 "?" 开头（即 URL 含 "??"）
    is_combo = raw_query.startswith("?")
    # 用 parse_qsl 做剥离/注入（keep_blank_values 保留空值）
    q_items = parse_qsl(raw_query, keep_blank_values=True)
    for p in strip_params or []:
        q_items = [(k, v) for k, v in q_items if k != p]

    # === 上游凭证注入（后端自带鉴权场景，如 DSH 需要 ?token=xxx 或 Bearer 头）===
    up_mode = rule.get("auth_upstream_mode") or "none"
    up_token = rule.get("auth_upstream_token") or ""
    up_header = rule.get("auth_upstream_header") or "Authorization"
    up_param = rule.get("auth_upstream_param") or "token"
    if inject_upstream and up_mode != "none" and up_token:
        if up_mode == "bearer":
            if not (headers.get("Authorization") or headers.get("authorization")):
                headers = dict(headers)
                headers["Authorization"] = f"Bearer {up_token}"
        elif up_mode == "header":
            headers = dict(headers)
            headers[up_header] = up_token
        elif up_mode == "basic":
            import base64 as _b64
            up_user = rule.get("auth_upstream_user") or ""
            up_pass = rule.get("auth_upstream_pass") or ""
            cred = _b64.b64encode(f"{up_user}:{up_pass}".encode()).decode()
            headers = dict(headers)
            headers["Authorization"] = f"Basic {cred}"
        elif up_mode == "query":
            if up_param:
                # 注入：替换已有同名参数，否则追加
                replaced = False
                for i, (k, v) in enumerate(q_items):
                    if k == up_param:
                        q_items[i] = (up_param, up_token)
                        replaced = True
                        break
                if not replaced:
                    q_items.append((up_param, up_token))

    # 重组 query：保留原始结构。combo 格式用原始字符串透传（不注入 query 凭证，
    # 避免破坏 "??" 结构）；普通格式用 parse_qsl 重组。
    if is_combo and not (inject_upstream and up_mode == "query"):
        # combo URL：原样保留（仅剥离了 strip_params）
        if strip_params and q_items != parse_qsl(raw_query, keep_blank_values=True):
            new_query = "&".join(f"{k}={v}" for k, v in q_items)
            fwd_path = parsed.path + "?" + new_query
        else:
            fwd_path = parsed.path + ("?" + raw_query if raw_query else "")
    else:
        new_query = "&".join(f"{k}={v}" for k, v in q_items if v)
        fwd_path = parsed.path + (("?" + new_query) if new_query else "")

    # 构造转发头（过滤 hop-by-hop）
    hop = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
           "te", "trailers", "transfer-encoding", "upgrade", "content-length"}
    fwd_headers = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in hop:
            continue
        fwd_headers[k] = v

    # 保留浏览器原始 Host 头（关键：DSH 等后端做 CSRF/Origin 校验时，
    # 会比较 Host 与 Origin。若用 backend_host 设 Host，浏览器 Origin 不匹配 → 403；
    # 且 cookie 的 authority 也会错误 → 401。必须透传原始 Host）。
    orig_host = headers.get("Host") or headers.get("host") or ""
    if orig_host:
        fwd_headers = {k: v for k, v in fwd_headers.items() if k.lower() != "host"}
        fwd_headers["Host"] = orig_host

    use_tls = bool(rule.get("backend_tls"))
    conn_cls = http.client.HTTPSConnection if use_tls else http.client.HTTPConnection
    try:
        if use_tls:
            ctx = ssl.create_default_context()
            if rule.get("backend_tls_insecure"):
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            conn = conn_cls(backend_host, backend_port, timeout=30, context=ctx)
        else:
            conn = conn_cls(backend_host, backend_port, timeout=30)
        conn.request(method, fwd_path, body=body, headers=fwd_headers)
        resp = conn.getresponse()
        resp_body = resp.read()
        resp_headers = {k: v for k, v in resp.getheaders() if k.lower() not in hop}
        status = resp.status
        reason = resp.reason
        conn.close()
        return status, reason, resp_headers, resp_body
    except Exception as e:
        return 502, "Bad Gateway", {}, str(e).encode("utf-8", "ignore")


# --------------------------------------------------------------------------
# HTTP Handler（每 gate 端口一个 server，共享状态）
# --------------------------------------------------------------------------
class GateHandler(BaseHTTPRequestHandler):
    rule: dict = None   # 由 server 注入
    gate_port: int = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_401(self, rule):
        realm = rule.get("realm") or "Protected"
        # Basic 挑战（对浏览器弹窗友好）
        body = json.dumps({"error": "Unauthorized", "realm": realm}, ensure_ascii=False).encode()
        self.send_response(401)
        self.send_header("WWW-Authenticate", f'Basic realm="{realm}", charset="UTF-8"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        rule = self.rule
        if not rule:
            self._send_json(503, {"error": "gate not configured"})
            return
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # 读取 body（有大小限制）
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = None
        if length > 0:
            if length > MAX_BODY:
                body = self.rfile.read(MAX_BODY)
            else:
                body = self.rfile.read(length)
        # 尝试解析 JSON 表单（用于 body token）
        parsed_body = None
        if body:
            ctype = (self.headers.get("Content-Type") or "").lower()
            if "application/json" in ctype:
                try:
                    parsed_body = json.loads(body.decode("utf-8"))
                except Exception:
                    parsed_body = None
            elif "application/x-www-form-urlencoded" in ctype:
                try:
                    parsed_body = parse_qs(body.decode("utf-8"))
                except Exception:
                    parsed_body = None

        headers = {k: v for k, v in self.headers.items()}
        headers_lower = {k.lower(): v for k, v in headers.items()}

        # 例外路径 → 直接转发
        if _path_matches(path, rule.get("bypass_paths") or []):
            self._record_gate(True, "bypass")
            self._forward_and_reply(rule, method, self.path, headers_lower, body, [])
            return

        # 提取凭证（参数顺序：headers, query, body, rule, path）
        creds = _apply_rule_headers(headers_lower, query, parsed_body, rule, path)
        creds["path"] = path

        # Cookie 透传放行：后端有自带会话 cookie（如 DSH 的 dsh-auth-* 会话）时，
        # 浏览器首次用 URL token 换到 cookie 后，后续请求带 cookie 而非 token。
        # auth-gate 无法校验后端的 cookie 签名，放行让后端自行校验（伪造的会被后端拒）。
        # 注意：必须剥离外部 URL 里的网关 token 参数——若原样转发，后端（DSH）会
        # 把网关 token 当作自己的 launch token 校验 → 401。
        cookie_hdr = headers_lower.get("cookie") or ""
        if rule.get("auth_upstream_cookie") and cookie_hdr:
            self._record_gate(True, "cookie")
            strip = []
            if rule.get("token_param"):
                strip.append(rule.get("token_param"))
            self._forward_and_reply(rule, method, self.path, headers_lower, body, strip,
                                    inject_upstream=False)
            return

        ok, reason, matched = check_token(rule, creds)
        self._record_gate(ok, reason)

        if not ok:
            self._send_401(rule)
            return

        # 放行：url_token 模式默认剥离 token 参数
        strip_params = []
        if rule.get("mode") == "url_token" and rule.get("strip_token"):
            strip_params.append(rule.get("token_param") or "token")
        self._forward_and_reply(rule, method, self.path, headers_lower, body, strip_params)

    def _record_gate(self, ok, reason):
        try:
            _record(self.gate_port, ok, reason)
        except Exception:
            pass

    def _forward_and_reply(self, rule, method, path, headers_lower, body, strip_params,
                           inject_upstream=True):
        # WebSocket 升级请求：http.client 不支持 Upgrade（101 后是双向字节流），
        # 改用原始 TCP 隧道透传。DSH 的 /api/remote.mux 等 WebSocket 端点依赖此。
        conn_hdr = (headers_lower.get("connection") or "").lower()
        upg_hdr = (headers_lower.get("upgrade") or "").lower()
        if "upgrade" in conn_hdr and "websocket" in upg_hdr:
            self._proxy_websocket(rule, headers_lower, body)
            return
        # 重建原始头（大小写敏感传给后端用原始，但转发逻辑用 lower 键）
        orig_headers = {k: v for k, v in self.headers.items()}
        # 剥离 Authorization 中 Basic 内嵌？保留。剥离 host/content-length 由转发层处理。
        status, reason, resp_headers, resp_body = _forward_request(
            rule, method, path, orig_headers, body, strip_params,
            inject_upstream=inject_upstream)
        self.send_response(status)
        for k, v in resp_headers.items():
            lk = k.lower()
            if lk in ("transfer-encoding", "connection"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def _proxy_websocket(self, rule, headers_lower, body):
        """WebSocket 升级透传：把原始请求字节发给后端，原样转 101 响应，
        之后双向泵字节（client ↔ backend），直到任一方关闭。"""
        backend_host = rule.get("backend_host") or "127.0.0.1"
        backend_port = int(rule.get("backend_port") or 0)
        if not backend_port:
            self._send_json(502, {"error": "no backend_port"})
            return
        try:
            up = socket.create_connection((backend_host, backend_port), timeout=30)
        except Exception as e:
            self._send_json(502, {"error": f"backend unreachable: {e}"})
            return
        try:
            # 重建原始请求字节（请求行 + 头 + body），发给后端。
            # 用 self.headers 保持原始大小写与顺序；Host 透传浏览器原始值。
            req_line = f"{self.command} {self.path} HTTP/1.1\r\n".encode("latin-1", "ignore")
            head_lines = []
            for k, v in self.headers.items():
                # 跳过 hop-by-hop（除 Upgrade/Connection 外）与 content-length（body 已重发）
                lk = k.lower()
                if lk in ("content-length",):
                    continue
                head_lines.append(f"{k}: {v}\r\n".encode("latin-1", "ignore"))
            head_blob = req_line + b"".join(head_lines) + b"\r\n"
            up.sendall(head_blob)
            if body:
                up.sendall(body)
            # 原样把后端响应字节转发回客户端 socket（含 101 + 头 + 后续 WebSocket 帧）。
            # 不再由 BaseHTTPRequestHandler 写响应——直接操作底层 socket。
            sock_a = self.connection  # 客户端 socket
            self._pump_sockets(sock_a, up)
        except Exception:
            pass
        finally:
            try: up.close()
            except Exception: pass

    @staticmethod
    def _pump_sockets(a, b):
        """双向泵字节：a↔b，任一方 EOF 或错误则关闭两者。"""
        import select as _sel
        socks = [a, b]
        try:
            while True:
                r, _, _ = _sel.select(socks, [], [], 300)
                if not r:
                    break  # 5 分钟无数据，结束（WebSocket 一般长连，但有保活）
                for s in r:
                    try:
                        data = s.recv(65536)
                    except Exception:
                        return
                    if not data:
                        return  # EOF
                    other = b if s is a else a
                    try:
                        other.sendall(data)
                    except Exception:
                        return
        finally:
            for s in socks:
                try: s.shutdown(socket.SHUT_RDWR)
                except Exception: pass
                try: s.close()
                except Exception: pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_OPTIONS(self):
        # 预检直接放行（简单场景）
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, PATCH, OPTIONS")
        self.end_headers()


class GateServer(ThreadingHTTPServer):
    """每 gate 端口一个线程化 HTTP 服务器。"""
    allow_reuse_address = True
    daemon_threads = True


# --------------------------------------------------------------------------
# 多端口监听（每 gate_port 一个 server）
# --------------------------------------------------------------------------
_servers = []
_servers_lock = threading.Lock()


def _start_port_server(port: int, rule: dict):
    """在指定端口启动一个鉴权 server。"""
    try:
        handler = type(f"GateHandler_{port}", (GateHandler,), {
            "rule": rule,
            "gate_port": port,
        })
        srv = GateServer(("0.0.0.0", port), handler)
        with _servers_lock:
            _servers.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"[auth-gate] 端口 {port} 鉴权启动 (mode={rule.get('mode')}, backend={rule.get('backend_host')}:{rule.get('backend_port')})", flush=True)
        return True
    except Exception as e:
        print(f"[auth-gate] 端口 {port} 启动失败: {e}", flush=True)
        return False


def sync_servers():
    """根据当前规则启停端口 server（幂等）。"""
    rules = get_rules()
    # 监听端口 = 规则的 gate_port（不是 proxy_id）
    desired = {}
    for pid, r in rules.items():
        try:
            port = int(r.get("gate_port") or 0)
        except (TypeError, ValueError):
            continue
        if port:
            desired[port] = r

    with _servers_lock:
        # 停止多余端口
        active = {s.server_address[1]: s for s in _servers}
        for port, srv in list(active.items()):
            if port not in desired:
                print(f"[auth-gate] 停止端口 {port}", flush=True)
                threading.Thread(target=srv.shutdown, daemon=True).start()
                _servers.remove(srv)

    # 启动新端口 / 更新已存在（简单：若端口存在则忽略，规则变更由 SIGHUP 重载）
    for port, rule in desired.items():
        exists = any(s.server_address[1] == port for s in _servers)
        if not exists:
            _start_port_server(port, rule)


# --------------------------------------------------------------------------
# 信号处理
# --------------------------------------------------------------------------
def _signal_handler(signum, frame):
    global _shutting_down
    if signum == signal.SIGHUP:
        print("[auth-gate] SIGHUP 重载规则...", flush=True)
        reload_rules()
        sync_servers()
        return
    _shutting_down = True
    print(f"[auth-gate] 收到信号 {signum},退出...", flush=True)
    for srv in list(_servers):
        try:
            srv.shutdown()
        except Exception:
            pass
    sys.exit(0)


def main():
    global _shutting_down
    print(f"[auth-gate] 启动 (rules={RULE_FILE})", flush=True)
    # 确保目录
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    reload_rules()
    sync_servers()

    signal.signal(signal.SIGHUP, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # 主线程保活
    while not _shutting_down:
        time.sleep(1)


if __name__ == "__main__":
    main()
