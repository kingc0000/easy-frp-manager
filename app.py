"""FRP Manager - Web 管理工具主应用。

使用自写的 stdlib HTTP 服务器,零外部依赖,部署即用。

提供:
- Dashboard: 系统信息 + frp 安装状态
- 实例列表: CRUD + 启停/重启/日志(含 SSE 实时流)
- 配置编辑器: 可视化表单 -> 生成 TOML -> 创建/更新实例
"""

import os
import socket
import time
import urllib.request
import urllib.error
import base64
import tomllib
from pathlib import Path

import auth
import config_gen
import db
import frp_ops
from http_server import App

app = App(static_folder="static")

# === 启用认证:把所有非公开路径的 API 都受保护 ===
# 认证检查函数:传入 token,返回 session dict 或 None
app.router.auth_check = auth.get_session

# === 配置目录 ===
CONFIG_DIR = Path(os.environ.get("FRPM_CONFIG_DIR", "/data/frpm-configs"))
LOG_DIR = Path(os.environ.get("FRPM_LOG_DIR", "/var/lib/frpm/logs"))
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ============ Dashboard API ============

@app.get("/api/dashboard")
def api_dashboard(ctx):
    """系统概况 + frp 安装状态。"""
    frp_status = frp_ops.detect_frp()
    instances = db.list_instances()
    containers = frp_ops.list_frp_containers()

    system = {
        "hostname": os.uname().nodename,
        "platform": f"{os.uname().sysname} {os.uname().release}",
        "arch": os.uname().machine,
        "cpu_count": os.cpu_count(),
    }
    try:
        with open("/proc/uptime") as f:
            system["uptime_seconds"] = float(f.read().split()[0])
    except Exception:
        system["uptime_seconds"] = 0

    # 内存信息(从 /proc/meminfo)
    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    parts = v.split()
                    if parts and parts[0].endswith("kB"):
                        meminfo[k] = int(parts[0]) * 1024
        total = meminfo.get("MemTotal", 0)
        avail = meminfo.get("MemAvailable", 0)
        used = total - avail
        system["memory"] = {
            "total": total,
            "used": used,
            "free": meminfo.get("MemFree", 0),
            "available": avail,
            "percent": round(used / total * 100, 1) if total else 0,
        }
    except Exception:
        system["memory"] = None

    # 磁盘信息(用 df 命令)
    try:
        import subprocess
        r = subprocess.run(["df", "-B1", "/"], capture_output=True, text=True, timeout=3)
        parts = r.stdout.strip().splitlines()[1].split()
        system["disk"] = {
            "total": int(parts[1]),
            "used": int(parts[2]),
            "free": int(parts[3]),
            "percent": int(parts[4].rstrip("%")),
        }
    except Exception:
        system["disk"] = None

    # 负载
    try:
        with open("/proc/loadavg") as f:
            system["load_avg"] = [float(x) for x in f.read().split()[:3]]
    except Exception:
        system["load_avg"] = None

    return 200, {
        "system": system,
        "frp": {
            "frps_installed": frp_status.frps_installed,
            "frps_version": frp_status.frps_version,
            "frps_location": frp_status.frps_location,
            "frpc_installed": frp_status.frpc_installed,
            "frpc_version": frp_status.frpc_version,
            "frpc_location": frp_status.frpc_location,
            "docker_available": frp_status.docker_available,
            "docker_version": frp_status.docker_version,
            "mode": frp_status.mode,
        },
        "instances_count": len(instances),
        "containers": containers,
    }


# ============ 实例 API ============

@app.get("/api/instances/:id/frps-status")
def api_frps_status(ctx):
    """代理 frps dashboard API 数据(serverinfo + 各类代理)。

    从实例配置 TOML 读 webServer.addr/port/user/password,
    后端发起 HTTP 请求调用 frps 内置 dashboard API,聚合返回。
    """
    instance_id = int(ctx["params"]["id"])
    inst = db.get_instance(instance_id)
    if not inst:
        return 404, {"error": "not found"}
    if inst["type"] != "frps":
        return 400, {"error": "not frps instance"}

    # 解析实例配置 TOML
    try:
        with open(inst["config_path"], "rb") as f:
            cfg = tomllib.load(f)
    except Exception as e:
        return 500, {"error": f"配置读取失败: {e}"}

    ws = cfg.get("webServer") or {}
    if not ws.get("addr") or not ws.get("port"):
        return 400, {"error": "实例未启用 webServer,请先在配置里加 webServer.addr/port"}

    base_url = f"http://{ws['addr']}:{ws['port']}"
    if base_url.startswith("http://0.0.0.0"):
        base_url = base_url.replace("0.0.0.0", "127.0.0.1")

    # Basic Auth 头
    auth_header = {}
    if ws.get("user") and ws.get("password"):
        cred = f"{ws['user']}:{ws['password']}"
        auth_header["Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()

    def fetch(path):
        req = urllib.request.Request(base_url + path, headers=auth_header)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                import json as _json
                return _json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return {"_error": f"HTTP {e.code}: {e.reason}"}
        except urllib.error.URLError as e:
            return {"_error": f"连接失败: {e.reason}"}
        except Exception as e:
            return {"_error": str(e)}

    # 聚合 serverinfo + 各类代理
    serverinfo = fetch("/api/serverinfo")
    proxies = []
    for ptype in ["tcp", "udp", "http", "https", "tcpmux", "stcp", "sudp"]:
        data = fetch(f"/api/proxy/{ptype}")
        for p in (data.get("proxies") or []):
            p["_type"] = ptype
            proxies.append(p)

    return 200, {
        "web_server_url": base_url,
        "serverinfo": serverinfo,
        "proxies": proxies,
    }

@app.get("/api/instances")
def api_list_instances(ctx):
    """列出所有实例(数据库 + 容器/服务实时状态)。"""
    instances = db.list_instances()
    for inst in instances:
        if inst["deploy_mode"] == "binary":
            status = frp_ops.get_binary_status(inst["name"], inst["config_path"])
        else:
            status = frp_ops.get_container_status(inst["name"])
        inst["status"] = status
        inst["config_exists"] = os.path.exists(inst["config_path"])
    return 200, instances


@app.get("/api/instances/:id")
def api_get_instance(ctx):
    instance_id = int(ctx["params"]["id"])
    inst = db.get_instance(instance_id)
    if not inst:
        return 404, {"error": "not found"}
    if inst["deploy_mode"] == "binary":
        status = frp_ops.get_binary_status(inst["name"], inst["config_path"])
    else:
        status = frp_ops.get_container_status(inst["name"])
    inst["status"] = status
    inst["config_exists"] = os.path.exists(inst["config_path"])
    if os.path.exists(inst["config_path"]):
        with open(inst["config_path"]) as f:
            inst["config_content"] = f.read()
    return 200, inst


@app.post("/api/instances")
def api_create_instance(ctx):
    """创建新实例。"""
    data = ctx["body"] or {}
    name = (data.get("name") or "").strip()
    instance_type = (data.get("type") or "").strip()
    deploy_mode = data.get("deploy_mode", "binary")
    config_data = data.get("config") or {}

    if not name or not instance_type:
        return 400, {"error": "name and type required"}
    if instance_type not in ("frps", "frpc"):
        return 400, {"error": "type must be frps or frpc"}
    if deploy_mode != "binary":
        return 400, {"error": "deploy_mode must be binary (集成模式,frp进程在frpm容器内运行)"}
    if db.get_instance_by_name(name):
        return 409, {"error": f"实例名重复: {name}"}

    # 前置校验:binary 模式需要 frp 二进制可用(内置 bin/ 已自带,无需单独安装)
    if deploy_mode == "binary":
        bin_path = frp_ops.resolve_binary(instance_type)
        if not bin_path:
            return 400, {"error": f"未找到 frp 二进制({instance_type}),请检查 bin/ 目录"}

    # 端口冲突检查(防止 frps bind_port 被占用或与其它实例重复)
    conflict = _check_port_conflict(instance_type, config_data, exclude_name=name)
    if conflict:
        return 400, {"error": f"端口冲突: {conflict}"}

    # 生成配置文件
    config_path = str(CONFIG_DIR / f"{name}.toml")
    try:
        if instance_type == "frps":
            cfg = config_gen.FrpsConfig(**_pick_frps_fields(config_data))
        else:
            cfg = config_gen.FrpcConfig(**_pick_frpc_fields(config_data))
        with open(config_path, "w") as f:
            f.write(cfg.to_toml())
    except Exception as e:
        return 400, {"error": f"配置生成失败: {e}"}

    # 创建数据库记录
    try:
        instance_id = db.create_instance(name, instance_type, deploy_mode, config_path)
    except ValueError as e:
        if os.path.exists(config_path):
            os.remove(config_path)
        return 409, {"error": str(e)}

    result = {"instance_id": instance_id, "config_path": config_path}

    # 集成模式:frp进程在frpm容器内直接运行(binary模式)
    binary_result = frp_ops.create_binary_instance(
        instance_name=name,
        instance_type=instance_type,
        config_path=config_path,
        bin_path=frp_ops.resolve_binary(instance_type),
    )
    result["service"] = binary_result
    if not binary_result.get("success"):
        return 200, result
    return 201, result


@app.put("/api/instances/:id")
def api_update_instance(ctx):
    instance_id = int(ctx["params"]["id"])
    inst = db.get_instance(instance_id)
    if not inst:
        return 404, {"error": "not found"}
    data = ctx["body"] or {}
    restart = data.get("restart", True)

    try:
        if "raw_config" in data:
            raw = data["raw_config"]
            if not raw.strip() or "=" not in raw:
                return 400, {"error": "配置为空或格式错误"}
            with open(inst["config_path"], "w") as f:
                f.write(raw)
        else:
            config_data = data.get("config") or {}
            if inst["type"] == "frps":
                cfg = config_gen.FrpsConfig(**_pick_frps_fields(config_data))
            else:
                cfg = config_gen.FrpcConfig(**_pick_frpc_fields(config_data))
            with open(inst["config_path"], "w") as f:
                f.write(cfg.to_toml())
    except Exception as e:
        return 400, {"error": f"配置更新失败: {e}"}

    db.update_instance(instance_id, {"updated_at": int(time.time())})

    result = {"success": True}
    if restart:
        if inst["deploy_mode"] == "binary":
            restart_result = frp_ops.restart_binary_instance(inst["name"], inst["config_path"])
        else:
            restart_result = frp_ops.restart_instance(inst["name"])
        result["restart"] = restart_result
    return 200, result


@app.delete("/api/instances/:id")
def api_delete_instance(ctx):
    instance_id = int(ctx["params"]["id"])
    inst = db.get_instance(instance_id)
    if not inst:
        return 404, {"error": "not found"}

    result = {"deleted": {}}
    if inst["deploy_mode"] == "binary":
        result["service"] = frp_ops.delete_binary_instance(inst["name"], inst["config_path"])
    else:
        result["container"] = frp_ops.delete_instance_container(inst["name"])
    try:
        if os.path.exists(inst["config_path"]):
            os.remove(inst["config_path"])
            result["deleted"]["config"] = inst["config_path"]
    except Exception as e:
        result["config_error"] = str(e)
    if db.delete_instance(instance_id):
        result["deleted"]["db"] = instance_id
    return 200, result


@app.post("/api/instances/:id/start")
def api_start_instance(ctx):
    inst = db.get_instance(int(ctx["params"]["id"]))
    if not inst:
        return 404, {"error": "not found"}
    if inst["deploy_mode"] == "binary":
        return 200, frp_ops.start_binary_instance(inst["name"], inst["config_path"])
    return 200, frp_ops.start_instance(inst["name"])


@app.post("/api/instances/:id/stop")
def api_stop_instance(ctx):
    inst = db.get_instance(int(ctx["params"]["id"]))
    if not inst:
        return 404, {"error": "not found"}
    if inst["deploy_mode"] == "binary":
        return 200, frp_ops.stop_binary_instance(inst["name"], inst["config_path"])
    return 200, frp_ops.stop_instance(inst["name"])


@app.post("/api/instances/:id/restart")
def api_restart_instance(ctx):
    inst = db.get_instance(int(ctx["params"]["id"]))
    if not inst:
        return 404, {"error": "not found"}
    if inst["deploy_mode"] == "binary":
        return 200, frp_ops.restart_binary_instance(inst["name"], inst["config_path"])
    return 200, frp_ops.restart_instance(inst["name"])


@app.get("/api/instances/:id/logs")
def api_instance_logs(ctx):
    inst = db.get_instance(int(ctx["params"]["id"]))
    if not inst:
        return 404, {"error": "not found"}
    lines = int((ctx["query"].get("lines") or ["200"])[0])
    stream = (ctx["query"].get("stream") or ["0"])[0] == "1"

    if inst["deploy_mode"] == "binary":
        return 200, {"logs": frp_ops.get_binary_logs(inst["name"], lines=lines)}

    if stream:
        def generate():
            for line in frp_ops.stream_container_logs(inst["name"]):
                yield f"data: {line}"
        return 200, {"stream": True, "generator": generate()}

    return 200, {"logs": frp_ops.get_container_logs(inst["name"], lines=lines)}


# ============ 配置模板 API ============

@app.get("/api/template/proxy/:type")
def api_proxy_template(ctx):
    return 200, config_gen.default_proxy_template(ctx["params"]["type"])


@app.get("/api/template/server")
def api_server_template(ctx):
    return 200, config_gen.default_server_template()


@app.get("/api/proxy-types")
def api_proxy_types(ctx):
    return 200, {
        "tcp": "TCP 端口转发(最常用)",
        "udp": "UDP 端口转发",
        "http": "HTTP 反向代理(需要 customDomains)",
        "https": "HTTPS 反向代理(需要 customDomains)",
        "stcp": "加密 TCP 隧道(点对点)",
        "xtcp": "加密 TCP 隧道(支持双向)",
    }


# ============ 安装/卸载 API (frp二进制内置于镜像,无需安装API) ============


@app.get("/api/install/versions")
def api_install_versions(ctx):
    return 200, {
        "versions": frp_ops.FRP_VERSIONS,
        "default": frp_ops.DEFAULT_VERSION,
    }


# ============ 认证 API ============

@app.get("/api/auth/config")
def api_auth_config(ctx):
    """获取认证配置信息(公开,不需要登录)。"""
    return 200, {
        "enabled": True,
        "has_users": True,
        "default_username": auth.DEFAULT_USERNAME,
    }


@app.get("/api/auth/check")
def api_auth_check(ctx):
    """检查当前会话状态(公开,前端用来判断是否已登录)。"""
    # 从请求里提取 token
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
    session = auth.get_session(token)
    if session:
        return 200, {
            "authenticated": True,
            "username": session.get("username"),
            "expires_in": session.get("expire_at", 0) - int(time.time()),
        }
    return 200, {"authenticated": False}


@app.post("/api/auth/login")
def api_auth_login(ctx):
    """登录接口(公开)。"""
    body = ctx.get("body") or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        return 400, {"error": "用户名和密码不能为空"}
    try:
        result = auth.login(username, password)
    except ValueError as e:
        return 401, {"error": str(e)}
    # 返回 token,前端会存到 localStorage 或 cookie
    return 200, {
        "token": result["token"],
        "username": result["username"],
        "expires_in": result["expires_in"],
    }


@app.post("/api/auth/logout")
def api_auth_logout(ctx):
    """登出(需要登录)。"""
    token = ctx.get("token")
    if token:
        auth.delete_session(token)
    return 200, {"message": "已登出"}


@app.post("/api/auth/change-password")
def api_auth_change_password(ctx):
    """修改密码(需要登录)。"""
    body = ctx.get("body") or {}
    old_password = body.get("old_password") or ""
    new_password = body.get("new_password") or ""
    confirm_password = body.get("confirm_password") or ""
    username = ctx.get("session", {}).get("username", "")

    if not old_password or not new_password:
        return 400, {"error": "旧密码和新密码不能为空"}
    if len(new_password) < 6:
        return 400, {"error": "新密码长度至少 6 位"}
    if new_password != confirm_password:
        return 400, {"error": "两次输入的新密码不一致"}
    if not username:
        return 401, {"error": "未登录"}

    try:
        result = auth.change_password(old_password, new_password, username)
    except ValueError as e:
        return 400, {"error": str(e)}
    return 200, result


@app.get("/api/auth/me")
def api_auth_me(ctx):
    """获取当前用户信息(需要登录)。"""
    session = ctx.get("session", {})
    return 200, {
        "username": session.get("username"),
        "expires_in": session.get("expire_at", 0) - int(time.time()),
    }


# ============ 工具函数:从表单数据构造 dataclass ============

def _is_port_free(port: int, host: str = "0.0.0.0") -> bool:
    """用 socket 绑定测试端口是否可用(True=空闲)。"""
    if port is None or not (1 <= port <= 65535):
        return True
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _check_port_conflict(instance_type: str, config_data: dict, exclude_name: str = None) -> str:
    """检查端口冲突,返回冲突描述或空字符串。

    检查两类:
    1. frps 的 bind_port / vhost 端口 / web_server_port 是否已被占用
    2. 是否与其他实例(数据库)的 frps bind_port 重复
    """
    # 收集本实例的 frps 监听端口
    ports = {}
    if instance_type == "frps":
        bp = config_data.get("bind_port")
        if bp:
            try:
                ports["bindPort"] = int(bp)
            except (ValueError, TypeError):
                pass
        kbp = config_data.get("kcp_bind_port")
        if kbp:
            try:
                ports["kcpBindPort"] = int(kbp)
            except (ValueError, TypeError):
                pass
        for vk in ("vhost_http_port", "vhost_https_port", "web_server_port"):
            v = config_data.get(vk)
            if v:
                try:
                    ports[vk] = int(v)
                except (ValueError, TypeError):
                    pass

    # 检查端口是否被系统占用
    for pname, p in ports.items():
        if not _is_port_free(p):
            return f"{pname} 端口 {p} 已被占用"

    # 检查是否与其他 frps 实例的 bind_port 重复
    if instance_type == "frps" and "bindPort" in ports:
        for inst in db.list_instances():
            if inst["name"] == exclude_name:
                continue
            if inst["type"] != "frps":
                continue
            try:
                with open(inst["config_path"]) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("bindPort") and "=" in line:
                            other_bp = int(line.split("=")[1].strip())
                            if other_bp == ports["bindPort"]:
                                return f"bindPort {ports['bindPort']} 与实例 {inst['name']} 冲突"
            except (OSError, ValueError):
                continue

    return ""


def _pick_frps_fields(data: dict) -> dict:
    """从表单数据构造 FrpsConfig 字段。

    做向后兼容:老前端字段名映射到新字段名。
    """
    # 别名映射:旧名 -> 新名
    alias_map = {
        'transport_heartbeat_interval': 'transport_heartbeat_timeout',  # frp v0.61.1 没这个字段,映射到 timeout
        'transport_heartbeat_ttl': 'transport_heartbeat_timeout',
        'allow_arbitrary_ports': None,  # 新版移除,直接丢弃
    }
    valid = {"bind_addr", "bind_port", "kcp_bind_port", "proxy_bind_addr",
             "vhost_http_port", "vhost_https_port", "auth_method", "auth_token",
             "transport_tcp_mux", "transport_heartbeat_timeout",
             "transport_tcp_keepalive", "transport_max_pool_count",
             "transport_tls_force", "transport_tls_cert_file", "transport_tls_key_file",
             "log_to", "log_level", "log_max_days",
             "web_server_addr", "web_server_port", "web_server_user", "web_server_password",
             "enable_prometheus", "user_conn_timeout", "detailed_errors_to_client",
             "allow_ports"}
    out = {}
    for k, v in data.items():
        if v is None:
            continue
        if k in alias_map:
            new_k = alias_map[k]
            if new_k:
                out[new_k] = v
            continue
        if k in valid:
            out[k] = v
    return out


def _pick_frpc_fields(data: dict) -> dict:
    """从表单数据构造 FrpcConfig 字段。

    做向后兼容:老前端用 `servers` 数组,后端只支持单 server。
    """
    # 别名映射
    alias_map = {
        'transport_heartbeat_interval': None,  # 新版移除
        'transport_heartbeat_ttl': None,       # 新版移除
        'transport_connect_timeout': 'transport_dial_server_timeout',
        'http2_enabled': 'transport_http2_enabled',
        'allow_arbitrary_ports': None,
    }
    valid = {"server_addr", "server_port", "auth_method", "auth_token", "user",
             "login_fail_exit", "transport_tcp_mux", "transport_dial_server_timeout",
             "transport_pool_count", "transport_http2_enabled",
             "transport_tls_enable", "transport_tls_server_name",
             "transport_tls_skip_verify", "transport_tls_ca_file",
             "transport_tls_cert_file", "transport_tls_key_file",
             "transport_bandwidth_limit",
             "log_to", "log_level", "log_max_days",
             "web_server_addr", "web_server_port", "web_server_user", "web_server_password"}
    out = {}
    for k, v in data.items():
        if v is None:
            continue
        if k in alias_map:
            new_k = alias_map[k]
            if new_k:
                out[new_k] = v
            continue
        if k in valid:
            out[k] = v

    # servers 数组 -> 取第一个作为顶层 server_addr/server_port
    if "servers" in data and isinstance(data["servers"], list) and data["servers"]:
        srv = data["servers"][0]
        # 兼容 servers[0] 用 serverAddr/serverPort (驼峰)
        if not out.get("server_addr"):
            out["server_addr"] = srv.get("serverAddr") or srv.get("server_addr")
        if not out.get("server_port"):
            out["server_port"] = srv.get("serverPort") or srv.get("server_port")
        if not out.get("auth_token") and srv.get("token"):
            out["auth_token"] = srv["token"]
        if not out.get("auth_token") and srv.get("auth_token"):
            out["auth_token"] = srv["auth_token"]

    # proxies
    proxies = []
    for p in (data.get("proxies") or []):
        try:
            proxies.append(config_gen.Proxy(
                name=p.get("name", "unnamed"),
                type=p.get("type", "tcp"),
                local_ip=p.get("local_ip", "127.0.0.1"),
                local_port=int(p.get("local_port", 0)),
                remote_port=p.get("remote_port"),
                custom_domains=p.get("custom_domains", []) if isinstance(p.get("custom_domains"), list)
                            else [d.strip() for d in (p.get("custom_domains") or "").split(",")],
                subdomain=p.get("subdomain"),
                secret_key=p.get("secret_key"),
                host_header_rewrite=p.get("host_header_rewrite"),
                locations=p.get("locations", []),
                http_user=p.get("http_user"),
                http_password=p.get("http_password") or p.get("http_pass"),
                plugin_name=p.get("plugin_name"),
                plugin_config=p.get("plugin_config"),
                plugin_local_addr=p.get("plugin_local_addr"),
                request_headers=p.get("request_headers", {}),
                response_headers=p.get("response_headers", {}),
                metadatas=p.get("metadatas", {}),
                annotations=p.get("annotations", {}),
                load_balancer_group=p.get("load_balancer_group"),
                load_balancer_group_key=p.get("load_balancer_group_key"),
                transport_bandwidth_limit=p.get("transport_bandwidth_limit"),
                transport_use_encryption=p.get("transport_use_encryption"),
                transport_use_compression=p.get("transport_use_compression"),
            ))
        except Exception as e:
            raise ValueError(f"代理配置错误 {p.get('name')}: {e}")
    out["proxies"] = proxies
    return out


def _auto_recover_instances():
    """启动时自动恢复所有 binary 模式的实例(容器重建后 frp 进程会丢失)。
    
    遍历数据库实例,对 deploy_mode='binary' 的实例检查:
    - 如果 status=running 但 PID 文件失效/进程不存在 → 自动重启
    - 这样容器重建后,frps/frpc 不需要手动重启
    """
    try:
        instances = db.list_instances()
    except Exception as e:
        print(f"[!] 自动恢复: 读取实例失败: {e}")
        return
    recovered = 0
    for inst in instances:
        if inst.get("deploy_mode") != "binary":
            continue
        name = inst.get("name")
        config_path = inst.get("config_path")
        if not name or not config_path:
            continue
        try:
            status = frp_ops.get_binary_status(name, config_path)
            # 之前是 running 但进程不在了 → 自动重启
            prev_running = inst.get("status", {}).get("running", False)
            if not status.get("running", False):
                print(f"[*] 自动恢复实例: {name} (pid 丢失)")
                frp_ops.start_binary_instance(name, config_path)
                # 更新数据库状态
                db.update_instance_status(name, {"running": True, "status": "running"})
                recovered += 1
            elif prev_running and not status.get("running"):
                # 之前 running 现在 not running,说明进程死了,重启
                print(f"[*] 自动重启实例: {name} (之前 running 现进程丢失)")
                frp_ops.start_binary_instance(name, config_path)
                db.update_instance_status(name, {"running": True, "status": "running"})
                recovered += 1
        except Exception as e:
            print(f"[!] 自动恢复 {name} 失败: {e}")
    if recovered:
        print(f"[*] 自动恢复完成: {recovered} 个实例已重启")


def main():
    db.init_db()
    port = int(os.environ.get("FRPM_PORT", "8080"))
    print(f"[*] FRP Manager 启动中... 监听 0.0.0.0:{port}")
    print(f"[*] 配置目录: {CONFIG_DIR}")
    print(f"[*] 数据库: {db.DB_PATH}")
    print(f"[*] 依赖: 仅使用 Python 标准库(无 Flask/psutil/docker)")
    # 启动时自动恢复 frps/frpc 实例(容器重建后进程会丢失)
    _auto_recover_instances()
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
