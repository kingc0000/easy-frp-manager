"""FRP Manager - 认证模块(零外部依赖)。

支持:
- 密码哈希(PBKDF2,来自 stdlib hashlib)
- Token 会话管理(内存字典,带过期时间)
- 默认凭证:admin/admin123
- 密码修改
- 认证中间件(装饰器)

存储:
- 凭证文件: /data/frpm-configs/auth.json(自动创建,存密码哈希+盐)
- 会话: 内存字典 {token: {username, expire_at}}
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from functools import wraps
from pathlib import Path
from typing import Optional


# === 默认凭证 ===
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin123"

# === 会话配置 ===
SESSION_TTL = 86400  # 24 小时
SESSION_SECRET = os.environ.get("FRPM_SESSION_SECRET", "") or secrets.token_hex(32)

# === 配置文件路径 ===
AUTH_CONFIG = Path(os.environ.get("FRPM_AUTH_FILE", "/data/frpm-configs/auth.json"))
AUTH_CONFIG.parent.mkdir(parents=True, exist_ok=True)


def _load_auth_config() -> dict:
    """加载凭证配置,不存在则用默认值初始化。"""
    if not AUTH_CONFIG.exists():
        # 首次启动,创建默认凭证
        # 先生成盐,再用这个盐 hash 密码,确保对应
        salt = os.urandom(16).hex()
        password_hash = hash_password(DEFAULT_PASSWORD, salt)
        cfg = {
            "username": DEFAULT_USERNAME,
            "password_hash": password_hash,
            "salt": salt,
            "created_at": int(time.time()),
        }
        _save_auth_config(cfg)
        print(f"[*] 已创建默认凭证文件: {AUTH_CONFIG}")
        print(f"[*] 默认用户名: {DEFAULT_USERNAME}, 默认密码: {DEFAULT_PASSWORD}")
        return cfg
    try:
        with open(AUTH_CONFIG, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[!] 加载凭证文件失败: {e},使用默认凭证")
        salt = os.urandom(16).hex()
        return {
            "username": DEFAULT_USERNAME,
            "password_hash": hash_password(DEFAULT_PASSWORD, salt),
            "salt": salt,
        }


def _save_auth_config(cfg: dict):
    """保存凭证配置。"""
    AUTH_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with open(AUTH_CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(AUTH_CONFIG, 0o600)  # 只允许 root 读写
    except Exception:
        pass


def hash_password(password: str, salt_hex: Optional[str] = None) -> str:
    """用 PBKDF2 哈希密码。返回 hex 字符串。
    
    salt_hex: 16 字节的盐的 hex 编码(32 个字符)。
    如果为 None,会生成新的随机盐,但只返回 hash(调用方需自己保留盐)。
    """
    if salt_hex is None:
        salt = os.urandom(16)
    else:
        salt = bytes.fromhex(salt_hex)
    iterations = 100000  # 足够慢,防暴力破解
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return dk.hex()


def verify_password(password: str, password_hash: str, salt_hex: str) -> bool:
    """验证密码。"""
    try:
        computed = hash_password(password, salt_hex)
        return hmac.compare_digest(computed, password_hash)
    except Exception:
        return False


# === 会话管理 ===
_sessions = {}  # {token: {"username": str, "expire_at": int}}
_sessions_lock = __import__("threading").Lock()


def create_session(username: str) -> str:
    """创建会话,返回 token。"""
    token = secrets.token_urlsafe(32)
    expire_at = int(time.time()) + SESSION_TTL
    with _sessions_lock:
        _sessions[token] = {"username": username, "expire_at": expire_at}
    return token


def get_session(token: str) -> Optional[dict]:
    """获取会话,过期或不存在返回 None。"""
    if not token:
        return None
    with _sessions_lock:
        session = _sessions.get(token)
        if not session:
            return None
        if time.time() > session["expire_at"]:
            # 过期,删除
            del _sessions[token]
            return None
        return session


def delete_session(token: str):
    """删除会话(登出)。"""
    if not token:
        return
    with _sessions_lock:
        _sessions.pop(token, None)


def clear_all_sessions():
    """清空所有会话(改密码后调用)。"""
    with _sessions_lock:
        _sessions.clear()


# === 认证中间件(装饰器) ===
def require_auth(handler):
    """装饰器:要求请求已认证。未认证返回 401。"""
    @wraps(handler)
    def wrapper(ctx):
        # 提取 token: 优先 Authorization: Bearer <token>,其次 Cookie: session=<token>
        headers = ctx.get("headers", {})
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
        session = get_session(token)
        if not session:
            return (401, {"error": "未登录或会话已过期", "login_required": True})
        ctx["session"] = session
        ctx["token"] = token
        return handler(ctx)
    return wrapper


# === 公共函数 ===
def get_current_credentials() -> dict:
    """获取当前凭证配置(返回只读副本)。"""
    cfg = _load_auth_config()
    return {
        "username": cfg["username"],
        "has_password": bool(cfg.get("password_hash")),
        "created_at": cfg.get("created_at"),
    }


def login(username: str, password: str) -> dict:
    """尝试登录。成功返回 token,失败抛出 ValueError。"""
    cfg = _load_auth_config()
    if username != cfg.get("username"):
        raise ValueError("用户名或密码错误")
    if not cfg.get("password_hash"):
        raise ValueError("凭证文件损坏,请删除 /data/frpm-configs/auth.json 后重启")
    if not verify_password(password, cfg["password_hash"], cfg.get("salt", "")):
        raise ValueError("用户名或密码错误")
    token = create_session(username)
    return {"token": token, "username": username, "expires_in": SESSION_TTL}


def change_password(old_password: str, new_password: str, username: str) -> dict:
    """修改密码。验证旧密码后更新。"""
    cfg = _load_auth_config()
    if username != cfg.get("username"):
        raise ValueError("用户名不匹配")
    if not verify_password(old_password, cfg["password_hash"], cfg.get("salt", "")):
        raise ValueError("旧密码错误")
    # 生成新盐和新哈希
    new_salt = os.urandom(16).hex()
    new_hash = hash_password(new_password, new_salt)
    cfg["password_hash"] = new_hash
    cfg["salt"] = new_salt
    cfg["updated_at"] = int(time.time())
    _save_auth_config(cfg)
    # 改密码后清空所有会话(强制重新登录)
    clear_all_sessions()
    return {"message": "密码修改成功,请重新登录"}


def change_username(new_username: str, old_password: str) -> dict:
    """修改用户名。必须验证当前密码,防止未授权篡改凭证。

    与改密码共用一个"验证旧密码"闸门:知道当前密码的人才能改用户名。
    这样不需要单独的 UI 信任级别,也不会被未登录的任何人调用。
    """
    new_username = (new_username or "").strip()
    if not new_username:
        raise ValueError("用户名不能为空")
    if len(new_username) < 3 or len(new_username) > 32:
        raise ValueError("用户名长度需在 3-32 个字符之间")
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", new_username):
        raise ValueError("用户名只能包含字母、数字、下划线和连字符")

    cfg = _load_auth_config()
    if not verify_password(old_password, cfg["password_hash"], cfg.get("salt", "")):
        raise ValueError("密码错误")
    if new_username == cfg.get("username"):
        raise ValueError("新用户名与当前用户名相同")

    cfg["username"] = new_username
    cfg["updated_at"] = int(time.time())
    _save_auth_config(cfg)
    # 改用户名后清空所有会话:旧会话 token 绑的是旧用户名,登录校验会失败
    clear_all_sessions()
    return {"message": f"用户名已改为 {new_username},请重新登录"}



def get_config_path() -> str:
    """返回凭证配置文件路径,方便用户排查。"""
    return str(AUTH_CONFIG)


def reset_auth_config() -> dict:
    """把凭证重置为默认 admin/admin123。

    仅在"忘记密码"时由管理员手动执行:
      docker exec frpm rm -f /data/frpm-configs/auth.json && docker restart frpm
    或者直接跑这一行:
      docker exec frpm python3 -c "import auth; print(auth.reset_auth_config())"

    安全考虑:不提供 /api 端点。重置凭证是个高危操作,必须人工在服务器上执行,
    不能给前端一个按钮点一下就重置(否则攻击者拿到登录页就能接管实例)。
    """
    cfg_path = str(AUTH_CONFIG)
    existed = AUTH_CONFIG.exists()
    if existed:
        AUTH_CONFIG.unlink()
    cfg = _load_auth_config()  # 文件不存在会重新创建默认凭证
    return {
        "message": "凭证已重置为默认值",
        "username": DEFAULT_USERNAME,
        "password": DEFAULT_PASSWORD,
        "previous_file_existed": existed,
        "path": cfg_path,
    }

