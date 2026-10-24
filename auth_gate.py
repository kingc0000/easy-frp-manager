#!/usr/bin/env python3
"""Auth Gate — frpc 侧鉴权网关。

为每个 auth-enabled 代理在 127.0.0.1:<gate_port> 监听。
请求到达时：读 HTTP 首部 → 按规则鉴权 → 通过则转发到真实后端（socket 级双向 pipe，
天然支持 SSE / WebSocket Upgrade / 大文件流式）。

鉴权方式：
  none     — 不鉴权（等价于不开，仅做端口重映射）
  basic    — Authorization: Basic base64(user:pass)，失败 401+WWW-Authenticate
  bearer   — Authorization: Bearer <token>
  apikey   — 自定义头（默认 X-API-Key）
  url_token — ?<param>=<token> URL 参数（默认 param=token）
  any      — 依次尝试 basic/bearer/url_token，任一命中即放行；失败优先 basic 弹窗

规则文件 $FRPM_CONFIG_DIR/auth-gate.json：
  {"<proxy_id>": {"gate_port": 16384, "backend_host": "127.0.0.1",
                  "backend_port": 8080, "mode": "basic",
                  "basic": {"user": "admin", "pass_hash": "...", "salt": "..."},
                  "tokens": ["abc123"],
                  "token_header": "X-API-Key",
                  "token_param": "token",
                  "strip_token": true,
                  "bypass_paths": ["/health"],
                  "realm": "Restricted"}}
"""
import base64
import hmac
import json
import os
import select
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qsl, urlencode

# 复用 frpm 自身的密码哈希（PBKDF2-HMAC-SHA256 100k 迭代）
from auth import verify_password, hash_password

CONFIG_DIR = os.environ.get("FRPM_CONFIG_DIR", "/data/frpm-configs")
LOG_DIR = os.environ.get("FRPM_LOG_DIR", "/var/lib/frpm/logs")
RULES_FILE = os.path.join(CONFIG_DIR, "auth-gate.json")
AUDIT_FILE = os.path.join(LOG_DIR, "auth-gate-audit.jsonl")

GATE_HOST = "127.0.0.1"
GATE_PORT_MIN = 16384
GATE_PORT_MAX = 19999

# 限流：每 IP 每分钟失败超过此数 → 冷却
RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 60  # 秒
RATE_LIMIT_COOLDOWN = 60  # 秒

# 读首部上限（HTTP 请求头一般 < 16KB，给足 64KB）
MAX_HEADER = 65536
HEADER_TIMEOUT = 10  # 秒

# --- 全局状态 ---
_rules = {}          # {gate_port: rule_dict}
_listeners = {}      # {gate_port: socket}
_lock = threading.Lock()
_fail_counts = {}    # {ip: [(timestamp, ...)]}
_cooldowns = {}      # {ip: cooldown_until}
_audit_lock = threading.Lock()
_shutting_down = False


def _now():
    return time.time()


def _audit(event):
    """写审计日志（jsonl）。"""
    try:
        line = json.dumps(event, ensure_ascii=False, default=str)
        with _audit_lock:
            with open(AUDIT_FILE, "a") as f:
                f.write(line + "\n")
    except Exception:
        pass


def load_rules():
    """从规则文件加载，返回 {gate_port: rule}。"""
    try:
        with open(RULES_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    rules = {}
    for pid, r in data.items():
        port = r.get("gate_port")
        if port and r.get("mode") and r.get("mode") != "none":
            try:
                rules[int(port)] = r
            except (ValueError, TypeError):
                continue
    return rules


def _close_listeners():
    """关闭所有监听 socket。"""
    with _lock:
        for port, sock in list(_listeners.items()):
            try:
                sock.close()
            except Exception:
                pass
            _listeners.pop(port, None)


def _open_listeners():
    """为每条规则打开监听 socket。"""
    global _rules
    rules = load_rules()
    with _lock:
        _rules = rules
        for port, rule in rules.items():
            if port in _listeners:
                continue
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((GATE_HOST, port))
                sock.listen(128)
                _listeners[port] = sock
                threading.Thread(
                    target=_accept_loop, args=(port, sock), daemon=True
                ).start()
                print(f"[auth-gate] listening on {GATE_HOST}:{port} -> "
                      f"{rule.get('backend_host')}:{rule.get('backend_port')} "
                      f"mode={rule.get('mode')}", flush=True)
            except OSError as e:
                print(f"[auth-gate] bind {port} failed: {e}", flush=True)


def _accept_loop(port, sock):
    """accept 循环（每端口一个线程）。"""
    while not _shutting_down:
        try:
            client, addr = sock.accept()
        except OSError:
            break
        threading.Thread(
            target=_handle, args=(client, addr, port), daemon=True
        ).start()


def _is_rate_limited(ip):
    """检查 IP 是否在冷却期。"""
    cd = _cooldowns.get(ip, 0)
    if cd and _now() < cd:
        return True
    if cd and _now() >= cd:
        _cooldowns.pop(ip, None)
        _fail_counts.pop(ip, None)
    return False


def _record_fail(ip, port):
    """记录一次失败，触发限流。"""
    now = _now()
    ts_list = _fail_counts.get(ip, [])
    ts_list = [t for t in ts_list if now - t < RATE_LIMIT_WINDOW]
    ts_list.append(now)
    _fail_counts[ip] = ts_list
    if len(ts_list) >= RATE_LIMIT_MAX:
        _cooldowns[ip] = now + RATE_LIMIT_COOLDOWN
        _audit({"ts": int(now), "ip": ip, "port": port,
                "event": "rate_limited", "cooldown": RATE_LIMIT_COOLDOWN})


def _check_basic(rule, auth_header):
    """Basic Auth 校验。返回 (ok: bool, reason: str)。"""
    basic = rule.get("basic") or {}
    user = basic.get("user")
    ph = basic.get("pass_hash")
    salt = basic.get("salt")
    if not user or not ph:
        return False, "basic_not_configured"
    if not auth_header or not auth_header.startswith("Basic "):
        return False, "no_basic_header"
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8", "replace")
        given_user, _, given_pass = decoded.partition(":")
    except Exception:
        return False, "bad_base64"
    if not hmac.compare_digest(given_user, user):
        return False, "user_mismatch"
    if not verify_password(given_pass, ph, salt):
        return False, "pass_mismatch"
    return True, "ok"


def _tokens_expired(rule):
    """token 是否已过期（token_expiry ISO 时间戳，空则不限制）。"""
    exp = rule.get("token_expiry")
    if not exp:
        return False
    try:
        dt = datetime.fromisoformat(exp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return time.time() > dt.timestamp()
    except (ValueError, TypeError):
        return False


def _check_bearer(rule, auth_header):
    """Bearer token 校验。"""
    tokens = rule.get("tokens") or []
    if not tokens:
        return False, "no_tokens"
    if _tokens_expired(rule):
        return False, "tokens_expired"
    if not auth_header or not auth_header.startswith("Bearer "):
        return False, "no_bearer_header"
    given = auth_header[7:].strip()
    for t in tokens:
        if t and hmac.compare_digest(given, t):
            return True, "ok"
    return False, "token_mismatch"


def _check_apikey(rule, headers):
    """自定义头 API Key 校验。"""
    tokens = rule.get("tokens") or []
    if not tokens:
        return False, "no_tokens"
    if _tokens_expired(rule):
        return False, "tokens_expired"
    hdr_name = rule.get("token_header") or "X-API-Key"
    given = headers.get(hdr_name.lower(), "")
    if not given:
        return False, "no_apikey_header"
    for t in tokens:
        if t and hmac.compare_digest(given, t):
            return True, "ok"
    return False, "apikey_mismatch"


def _check_url_token(rule, path):
    """URL token 校验（?token=xxx）。"""
    tokens = rule.get("tokens") or []
    if not tokens:
        return False, "no_tokens"
    if _tokens_expired(rule):
        return False, "tokens_expired"
    param = rule.get("token_param") or "token"
    sp = urlsplit(path)
    q = dict(parse_qsl(sp.query, keep_blank_values=True))
    given = q.get(param, "")
    if not given:
        return False, "no_token_param"
    for t in tokens:
        if t and hmac.compare_digest(given, t):
            return True, "ok"
    return False, "url_token_mismatch"


def _strip_token_from_path(path, rule):
    """从 URL query 剥离 token 参数。"""
    param = rule.get("token_param") or "token"
    sp = urlsplit(path)
    q = [(k, v) for k, v in parse_qsl(sp.query, keep_blank_values=True) if k != param]
    new_query = urlencode(q)
    new_path = sp.path
    if new_query:
        new_path += "?" + new_query
    return new_path


def _path_matches_bypass(path, bypass_paths):
    """检查路径是否在 bypass 列表。"""
    sp = urlsplit(path)
    clean = sp.path
    for bp in bypass_paths or []:
        if clean == bp or clean.startswith(bp.rstrip("/") + "/"):
            return True
    return False


def _check_auth(rule, method, path, headers):
    """按 mode 鉴权。返回 (ok: bool, reason: str, challenge_header: str|None)。"""
    mode = rule.get("mode", "none")
    realm = rule.get("realm") or "Restricted"
    challenge = f'Basic realm="{realm}"'

    if mode == "none":
        return True, "none", None

    if _path_matches_bypass(path, rule.get("bypass_paths")):
        return True, "bypass", None

    auth_header = headers.get("authorization", "")

    if mode == "basic":
        ok, reason = _check_basic(rule, auth_header)
        return ok, reason, challenge if not ok else None

    if mode == "bearer":
        ok, reason = _check_bearer(rule, auth_header)
        return ok, reason, None

    if mode == "apikey":
        ok, reason = _check_apikey(rule, headers)
        return ok, reason, None

    if mode == "url_token":
        ok, reason = _check_url_token(rule, path)
        return ok, reason, None

    if mode == "any":
        # 依次尝试 basic → bearer → apikey → url_token
        if auth_header.startswith("Basic "):
            ok, reason = _check_basic(rule, auth_header)
            if ok:
                return True, "basic", None
        if auth_header.startswith("Bearer "):
            ok, reason = _check_bearer(rule, auth_header)
            if ok:
                return True, "bearer", None
        hdr_name = rule.get("token_header") or "X-API-Key"
        if headers.get(hdr_name.lower()):
            ok, reason = _check_apikey(rule, headers)
            if ok:
                return True, "apikey", None
        ok, reason = _check_url_token(rule, path)
        if ok:
            return True, "url_token", None
        # 全部失败，优先 basic 弹窗
        return False, "any_failed", challenge

    return False, "unknown_mode", challenge


def _send_401(client, challenge, body_msg="Unauthorized"):
    """发送 401 响应。"""
    lines = [
        "HTTP/1.1 401 Unauthorized",
        "Content-Type: text/plain; charset=utf-8",
        "Content-Length: " + str(len(body_msg.encode())),
        "Connection: close",
    ]
    if challenge:
        lines.append(f'WWW-Authenticate: {challenge}')
    lines.append("")
    lines.append(body_msg)
    try:
        client.sendall("\r\n".join(lines).encode("utf-8"))
    except Exception:
        pass


def _send_429(client):
    body = "Too Many Authentication Failures"
    resp = (
        f"HTTP/1.1 429 Too Many Requests\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Retry-After: {RATE_LIMIT_COOLDOWN}\r\n"
        f"Connection: close\r\n\r\n{body}"
    )
    try:
        client.sendall(resp.encode("utf-8"))
    except Exception:
        pass


def check_token(rule, credentials):
    """预演鉴权：给规则和凭证，返回是否通过（不转发请求）。

    供 app.py /api/auth-gate/test-token 端点调用。
    credentials: {
        "token": "xxx",        # bearer/apikey/url_token 共用
        "basic_user": "admin", # basic 模式用户名
        "basic_pass": "secret", # basic 模式密码
        "path": "/api/test",   # 可选，默认 "/"
    }
    返回 (ok: bool, reason: str, matched: str|None)。
    """
    method = "GET"
    path = credentials.get("path") or "/"
    headers = {}
    token = credentials.get("token") or ""
    basic_user = credentials.get("basic_user") or ""
    basic_pass = credentials.get("basic_pass") or ""

    if basic_user and basic_pass:
        cred = f"{basic_user}:{basic_pass}"
        headers["authorization"] = "Basic " + base64.b64encode(
            cred.encode("utf-8")).decode("ascii")
    elif token:
        # 同时构造 bearer 头、apikey 头、url 参数，让 _check_auth 按 mode 自动匹配
        headers["authorization"] = "Bearer " + token
        hdr_name = rule.get("token_header") or "X-API-Key"
        headers[hdr_name.lower()] = token
        param = rule.get("token_param") or "token"
        sp = urlsplit(path)
        qs = parse_qsl(sp.query, keep_blank_values=True)
        qs.append((param, token))
        path = sp.path + "?" + urlencode(qs)

    ok, reason, _challenge = _check_auth(rule, method, path, headers)
    matched = None
    if ok:
        if reason in ("none", "bypass"):
            matched = reason
        elif reason == "ok":
            matched = rule.get("mode", "none")
        else:
            matched = reason  # any 模式返回 "basic"/"bearer"/"apikey"/"url_token"
    return ok, reason, matched


def aggregate_stats(limit=500):
    """读取审计日志，按端口聚合统计。

    供 app.py /api/auth-gate/stats 端点调用。
    返回 {port_str: {ok, fail, last_ts, reasons: {reason: count}}}。
    """
    stats = {}
    try:
        with open(AUDIT_FILE) as f:
            lines = f.readlines()[-limit:]
    except Exception:
        return {}
    for line in lines:
        try:
            ev = json.loads(line.strip())
        except Exception:
            continue
        port = str(ev.get("port", ""))
        if not port:
            continue
        if port not in stats:
            stats[port] = {"ok": 0, "fail": 0, "last_ts": 0, "reasons": {}}
        ts = ev.get("ts", 0)
        if ts > stats[port]["last_ts"]:
            stats[port]["last_ts"] = ts
        event = ev.get("event", "")
        if event == "auth_ok":
            stats[port]["ok"] += 1
        elif event in ("auth_failed", "blocked_rate_limited"):
            stats[port]["fail"] += 1
            reason = ev.get("reason") or event
            stats[port]["reasons"][reason] = stats[port]["reasons"].get(reason, 0) + 1
    return stats


def _parse_http_head(data):
    """解析 HTTP 请求首部。返回 (method, path, headers_dict, header_end_offset) 或 None。"""
    end = data.find(b"\r\n\r\n")
    if end < 0:
        return None
    head = data[:end].decode("iso-8859-1", "replace")
    lines = head.split("\r\n")
    if not lines:
        return None
    request_line = lines[0]
    parts = request_line.split(" ")
    if len(parts) < 2:
        return None
    method = parts[0]
    path = parts[1]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return method, path, headers, end + 4


def _pipe(c1, c2):
    """双向原始字节转发（支持 SSE/WebSocket/大文件）。"""
    socks = [c1, c2]
    try:
        while True:
            r, _, x = select.select(socks, [], socks, 300)
            if x:
                break
            if not r:
                break  # 超时
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                other = c2 if s is c1 else c1
                other.sendall(data)
    except Exception:
        pass
    finally:
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass


def _handle(client, addr, port):
    """处理一个客户端连接。"""
    client.settimeout(HEADER_TIMEOUT)
    ip = addr[0] if addr else "unknown"

    rule = _rules.get(port)
    if not rule:
        try:
            client.close()
        except Exception:
            pass
        return

    # 限流检查
    if _is_rate_limited(ip):
        _send_429(client)
        _audit({"ts": int(_now()), "ip": ip, "port": port,
                "event": "blocked_rate_limited"})
        client.close()
        return

    # 读请求首部
    buf = b""
    while b"\r\n\r\n" not in buf:
        try:
            chunk = client.recv(4096)
        except socket.timeout:
            _audit({"ts": int(_now()), "ip": ip, "port": port,
                    "event": "header_timeout"})
            client.close()
            return
        except Exception:
            client.close()
            return
        if not chunk:
            client.close()
            return
        buf += chunk
        if len(buf) > MAX_HEADER:
            _audit({"ts": int(_now()), "ip": ip, "port": port,
                    "event": "header_too_large"})
            client.close()
            return

    parsed = _parse_http_head(buf)
    if not parsed:
        _audit({"ts": int(_now()), "ip": ip, "port": port,
                "event": "bad_request"})
        client.close()
        return

    method, path, headers, header_end = parsed

    # 鉴权
    ok, reason, challenge = _check_auth(rule, method, path, headers)
    if not ok:
        _record_fail(ip, port)
        _audit({"ts": int(_now()), "ip": ip, "port": port, "method": method,
                "path": path, "event": "auth_failed", "reason": reason})
        _send_401(client, challenge, f"401 Unauthorized: {reason}")
        client.close()
        return

    # 通过 — 连接后端
    backend_host = rule.get("backend_host") or "127.0.0.1"
    backend_port = rule.get("backend_port")
    if not backend_port:
        _send_401(client, None, "502 Backend Not Configured")
        client.close()
        return

    try:
        backend = socket.create_connection((backend_host, int(backend_port)), timeout=10)
    except Exception as e:
        body = f"502 Bad Gateway: {e}"
        resp = (
            f"HTTP/1.1 502 Bad Gateway\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n{body}"
        )
        try:
            client.sendall(resp.encode("utf-8"))
        except Exception:
            pass
        client.close()
        return

    # 重新构造请求首部（如需剥离 token 或注入 X-Forwarded）
    body_part = buf[header_end:]
    new_head = buf[:header_end]

    # 剥离 URL token
    if rule.get("strip_token") and rule.get("tokens"):
        new_path = _strip_token_from_path(path, rule)
        if new_path != path:
            new_head = new_head.replace(
                f"{method} {path} ".encode("iso-8859-1"),
                f"{method} {new_path} ".encode("iso-8859-1"),
                1,
            )

    # 注入 X-Forwarded-For / Proto
    xf_lines = ""
    existing_xf = headers.get("x-forwarded-for", "")
    if existing_xf:
        xf_lines += f"X-Forwarded-For: {existing_xf}, {ip}\r\n"
    else:
        xf_lines += f"X-Forwarded-For: {ip}\r\n"
    xf_lines += "X-Forwarded-Proto: http\r\n"
    # 在首部末尾（\r\n\r\n 前）插入
    new_head = new_head[:-4] + xf_lines.encode("iso-8859-1") + new_head[-4:]

    # 强制 Connection: close（非 Upgrade 请求），防止 keep-alive 复用连接绕过鉴权
    # —— 成功请求后 _pipe 保持开放，客户端复用同连接发下一请求会直接透传不经鉴权
    if not headers.get("upgrade"):
        _head_str = new_head.decode("iso-8859-1", "replace")
        _lines = _head_str.split("\r\n")
        _kept = [_lines[0]]  # request line
        for _line in _lines[1:]:
            if not _line:
                continue
            _k = _line.split(":", 1)[0].strip().lower()
            if _k == "connection":
                continue  # 剔除原有 Connection 头
            _kept.append(_line)
        _kept.append("Connection: close")
        new_head = ("\r\n".join(_kept) + "\r\n\r\n").encode("iso-8859-1")

    # 发送请求到后端
    try:
        backend.sendall(new_head + body_part)
    except Exception:
        client.close()
        backend.close()
        return

    _audit({"ts": int(_now()), "ip": ip, "port": port, "method": method,
            "path": path, "event": "auth_ok", "reason": reason,
            "backend": f"{backend_host}:{backend_port}"})

    # 双向 pipe（取消超时，切原始字节流）
    client.settimeout(None)
    backend.settimeout(None)
    _pipe(client, backend)


def _sighup(signum, frame):
    """SIGHUP = 重新加载规则。"""
    print("[auth-gate] SIGHUP received, reloading rules...", flush=True)
    _close_listeners()
    _open_listeners()


def _sigterm(signum, frame):
    """SIGTERM = 优雅退出。"""
    global _shutting_down
    _shutting_down = True
    print("[auth-gate] shutting down...", flush=True)
    _close_listeners()
    sys.exit(0)


def main():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    signal.signal(signal.SIGHUP, _sighup)
    signal.signal(signal.SIGTERM, _sigterm)

    _open_listeners()

    if not _listeners:
        print("[auth-gate] no auth-enabled proxies, exiting", flush=True)
        sys.exit(0)

    print(f"[auth-gate] started, {_listeners.__len__()} listener(s)", flush=True)

    # 主线程保持运行
    while not _shutting_down:
        try:
            signal.pause() if hasattr(signal, "pause") else time.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            break
        except Exception:
            time.sleep(1)


if __name__ == "__main__":
    main()
