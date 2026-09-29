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

    # 先创建数据库记录,得到 instance_id
    config_path = str(CONFIG_DIR / f"{name}.toml")
    try:
        instance_id = db.create_instance(name, instance_type, deploy_mode, config_path)
    except ValueError as e:
        return 409, {"error": str(e)}

    # 把 frpc 的 proxies 数组写入数据库(便于后续单 proxy 启停)
    if instance_type == "frpc":
        proxies_data = config_data.get("proxies") or []
        for p in proxies_data:
            try:
                db.create_proxy(instance_id, p)
            except ValueError:
                pass  # 重复名等忽略
        # 移除 proxies 字段,避免生成 TOML 时重复处理
        config_data = {k: v for k, v in config_data.items() if k != "proxies"}

    # 生成配置文件(此时 config_data 已无 proxies,数据库里已存)
    try:
        if instance_type == "frps":
            cfg = config_gen.FrpsConfig(**_pick_frps_fields(config_data))
        else:
            fields = _pick_frpc_fields(config_data)
            fields["proxies"] = _build_enabled_proxies(instance_id)
            cfg = config_gen.FrpcConfig(**fields)
        with open(config_path, "w") as f:
            f.write(cfg.to_toml())
    except Exception as e:
        # 清理:删除数据库记录 + 配置
        db.delete_instance(instance_id)
        if os.path.exists(config_path):
            os.remove(config_path)
        return 400, {"error": f"配置生成失败: {e}"}

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
                fields = _pick_frpc_fields(config_data)
                fields["proxies"] = _build_enabled_proxies(instance_id)
                cfg = config_gen.FrpcConfig(**fields)
            with open(inst["config_path"], "w") as f:
                f.write(cfg.to_toml())
    except Exception as e:
        return 400, {"error": f"配置更新失败: {e}"}

    db.update_instance(instance_id, {"updated_at": int(time.time())})

    result = {"success": True}
    if restart:
        if inst["deploy_mode"] == "binary":
            restart_result = _restart_frpc_instance(inst)
        else:
            restart_result = frp_ops.restart_instance(inst["name"])
        result["restart"] = restart_result
    return 200, result


# ============ Proxy (代理规则) API ============
# 单 proxy 启停:改 enabled 字段 + 重新生成 TOML + 重启实例。
# 因为 frp v0.61.1 不支持运行时启停单个 proxy,必须改配置重启。

def _proxy_row_to_config_proxy(p):
    """把数据库 proxy 行(dict)转成 config_gen.Proxy 对象。

    统一规范化字段:local_ip 默认 127.0.0.1,local_port 强制 int,
    列表字段默认 [],字典字段默认 {}。
    """
    return config_gen.Proxy(
        name=p.get("name"),
        type=p.get("type") or "tcp",
        local_ip=p.get("local_ip") or "127.0.0.1",
        local_port=int(p.get("local_port") or 0),
        remote_port=p.get("remote_port"),
        subdomain=p.get("subdomain"),
        custom_domains=p.get("custom_domains") or [],
        secret_key=p.get("secret_key"),
        host_header_rewrite=p.get("host_header_rewrite"),
        locations=p.get("locations") or [],
        http_user=p.get("http_user"),
        http_password=p.get("http_password"),
        plugin_name=p.get("plugin_name"),
        plugin_config=p.get("plugin_config"),
        plugin_local_addr=p.get("plugin_local_addr"),
        request_headers=p.get("request_headers") or {},
        response_headers=p.get("response_headers") or {},
        metadatas=p.get("metadatas") or {},
        load_balancer_group=p.get("load_balancer_group"),
        load_balancer_group_key=p.get("load_balancer_group_key"),
        transport_bandwidth_limit=p.get("transport_bandwidth_limit"),
        transport_use_encryption=p.get("transport_use_encryption"),
        transport_use_compression=p.get("transport_use_compression"),
    )


def _build_enabled_proxies(instance_id):
    """从数据库读启用的代理并转成 Proxy 对象列表。"""
    rows = db.list_proxies(instance_id)
    return [_proxy_row_to_config_proxy(p) for p in rows if p.get("enabled")]


def _parse_frpc_base_fields(base_data):
    """从 TOML 顶层 dict(base_data)提取 FrpcConfig 需要的字段(不含 proxies)。

    base_data 是 tomllib 解析后的 TOML 顶层 dict,frpc 配置字段使用驼峰命名
    (serverAddr、loginFailExit 等),此函数映射成 FrpcConfig 的 snake_case 字段名。
    """
    fields = {}
    # 顶层字段
    if "serverAddr" in base_data:
        fields["server_addr"] = base_data["serverAddr"]
    if "serverPort" in base_data:
        fields["server_port"] = base_data["serverPort"]
    if "user" in base_data:
        fields["user"] = base_data["user"]
    if "loginFailExit" in base_data:
        fields["login_fail_exit"] = base_data["loginFailExit"]
    # auth.*
    auth = base_data.get("auth")
    if isinstance(auth, dict):
        if "method" in auth:
            fields["auth_method"] = auth["method"]
        if "token" in auth:
            fields["auth_token"] = auth["token"]
    # transport.*
    transport = base_data.get("transport")
    if isinstance(transport, dict):
        if "tcpMux" in transport:
            fields["transport_tcp_mux"] = transport["tcpMux"]
        if "dialServerTimeout" in transport:
            fields["transport_dial_server_timeout"] = transport["dialServerTimeout"]
        if "poolCount" in transport:
            fields["transport_pool_count"] = transport["poolCount"]
        http2 = transport.get("http2")
        if isinstance(http2, dict) and "enabled" in http2:
            fields["transport_http2_enabled"] = http2["enabled"]
        tls = transport.get("tls")
        if isinstance(tls, dict):
            for src, dst in [("enable", "transport_tls_enable"),
                              ("serverName", "transport_tls_server_name"),
                              ("skipVerify", "transport_tls_skip_verify"),
                              ("caFile", "transport_tls_ca_file"),
                              ("certFile", "transport_tls_cert_file"),
                              ("keyFile", "transport_tls_key_file")]:
                if src in tls:
                    fields[dst] = tls[src]
    # log.*
    log = base_data.get("log")
    if isinstance(log, dict):
        if "to" in log:
            fields["log_to"] = log["to"]
        if "level" in log:
            fields["log_level"] = log["level"]
        if "maxDays" in log:
            fields["log_max_days"] = log["maxDays"]
    # webServer.*
    web = base_data.get("webServer")
    if isinstance(web, dict):
        if "addr" in web:
            fields["web_server_addr"] = web["addr"]
        if "port" in web:
            fields["web_server_port"] = web["port"]
        if "user" in web:
            fields["web_server_user"] = web["user"]
        if "password" in web:
            fields["web_server_password"] = web["password"]
    return fields


def _wait_for_port(port, host="127.0.0.1", timeout=10.0, interval=0.3):
    """等待 TCP 端口就绪(用于自动恢复时等 frps 起来再启动 frpc)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        try:
            if s.connect_ex((host, port)) == 0:
                return True
        finally:
            s.close()
        time.sleep(interval)
    return False


def _get_frpc_instance(instance_id):
    """获取 frpc 实例;不存在或不是 frpc 时返回 (None, (status, error_body))。

    返回 (inst, None) 表示成功,调用方继续用 inst。
    返回 (None, (status, body)) 表示失败,调用方 `return status, body`。
    """
    inst = db.get_instance(instance_id)
    if not inst:
        return None, (404, {"error": "not found"})
    if inst["type"] != "frpc":
        return None, (400, {"error": "仅 frpc 实例支持代理规则"})
    return inst, None


def _restart_frpc_instance(inst):
    """重启 frpc 实例进程(binary 模式)。"""
    return frp_ops.restart_binary_instance(inst["name"], inst["config_path"], inst["type"])


def _regenerate_frpc_config(instance_id):
    """从数据库重新生成 frpc TOML 配置文件。返回 (config_path, success, error)。

    流程:读取现有 TOML 基础配置 → 从数据库读启用的代理 → 重新组装并写回。
    """
    inst, err = _get_frpc_instance(instance_id)
    if err:
        return None, False, err[1]["error"]

    config_path = inst["config_path"]

    # 读取现有 TOML 的基础配置(非 proxies 部分)
    existing_data = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "rb") as f:
                existing_data = tomllib.load(f)
        except Exception:
            existing_data = {}

    base_data = {k: v for k, v in existing_data.items() if k != "proxies"}
    fields = _parse_frpc_base_fields(base_data)
    fields["proxies"] = _build_enabled_proxies(instance_id)

    try:
        cfg = config_gen.FrpcConfig(**fields)
        with open(config_path, "w") as f:
            f.write(cfg.to_toml())
        db.update_instance(instance_id, {"updated_at": int(time.time())})
        return config_path, True, None
    except Exception as e:
        return config_path, False, str(e)


@app.get("/api/instances/:id/proxies")
def api_list_proxies(ctx):
    """列出实例下所有代理规则。"""
    instance_id = int(ctx["params"]["id"])
    inst = db.get_instance(instance_id)
    if not inst:
        return 404, {"error": "not found"}
    proxies = db.list_proxies(instance_id)
    return 200, {"proxies": proxies}


@app.post("/api/instances/:id/proxies")
def api_create_proxy(ctx):
    """新建代理规则(默认启用,会重新生成配置 + 重启实例)。"""
    instance_id = int(ctx["params"]["id"])
    inst, err = _get_frpc_instance(instance_id)
    if err:
        return err

    proxy_data = ctx["body"] or {}
    try:
        proxy_id = db.create_proxy(instance_id, proxy_data)
    except ValueError as e:
        return 400, {"error": str(e)}

    _, ok, reg_err = _regenerate_frpc_config(instance_id)
    if not ok:
        return 400, {"error": f"配置生成失败: {reg_err}", "proxy_id": proxy_id}

    restart_result = _restart_frpc_instance(inst)
    return 201, {"proxy_id": proxy_id, "restart": restart_result}


@app.put("/api/instances/:id/proxies/:pid")
def api_update_proxy(ctx):
    """更新代理规则(会重新生成配置 + 重启实例)。"""
    instance_id = int(ctx["params"]["id"])
    proxy_id = int(ctx["params"]["pid"])
    inst, err = _get_frpc_instance(instance_id)
    if err:
        return err

    proxy = db.get_proxy(proxy_id)
    if not proxy or proxy["instance_id"] != instance_id:
        return 404, {"error": "代理不存在"}

    proxy_data = ctx["body"] or {}
    try:
        db.update_proxy(proxy_id, proxy_data)
    except ValueError as e:
        return 400, {"error": str(e)}

    _, ok, reg_err = _regenerate_frpc_config(instance_id)
    if not ok:
        return 400, {"error": f"配置生成失败: {reg_err}"}

    restart_result = _restart_frpc_instance(inst)
    return 200, {"success": True, "restart": restart_result}


@app.post("/api/instances/:id/proxies/:pid/toggle")
def api_toggle_proxy(ctx):
    """启停单个代理规则(会重新生成配置 + 重启实例)。"""
    instance_id = int(ctx["params"]["id"])
    proxy_id = int(ctx["params"]["pid"])
    inst, err = _get_frpc_instance(instance_id)
    if err:
        return err

    proxy = db.get_proxy(proxy_id)
    if not proxy or proxy["instance_id"] != instance_id:
        return 404, {"error": "代理不存在"}

    body = ctx["body"] or {}
    enabled = bool(body.get("enabled", not proxy.get("enabled")))

    db.toggle_proxy(proxy_id, enabled)

    _, ok, reg_err = _regenerate_frpc_config(instance_id)
    if not ok:
        return 400, {"error": f"配置生成失败: {reg_err}"}

    restart_result = _restart_frpc_instance(inst)
    return 200, {
        "success": True,
        "proxy_id": proxy_id,
        "enabled": enabled,
        "restart": restart_result,
    }


@app.delete("/api/instances/:id/proxies/:pid")
def api_delete_proxy(ctx):
    """删除代理规则(会重新生成配置 + 重启实例)。"""
    instance_id = int(ctx["params"]["id"])
    proxy_id = int(ctx["params"]["pid"])
    inst, err = _get_frpc_instance(instance_id)
    if err:
        return err

    proxy = db.get_proxy(proxy_id)
    if not proxy or proxy["instance_id"] != instance_id:
        return 404, {"error": "代理不存在"}

    db.delete_proxy(proxy_id)

    _, ok, reg_err = _regenerate_frpc_config(instance_id)
    if not ok:
        return 400, {"error": f"配置生成失败: {reg_err}"}

    restart_result = _restart_frpc_instance(inst)
    return 200, {"success": True, "restart": restart_result}


def _find_owner_by_name(proxy_name: str):
    """在所有 frpc 实例的数据库里按代理名查找定义。

    frps 详情页的代理是 frpc 注册上来的运行时状态,定义存在某个 frpc 实例的数据库里。
    返回 (proxy_row, instance_row);找不到返回 (None, None)。
    """
    frpc_list = [i for i in db.list_instances() if i["type"] == "frpc"]
    for inst in frpc_list:
        row = db.get_proxy_by_name(inst["id"], proxy_name)
        if row:
            return row, inst
    return None, None


@app.delete("/api/instances/:id/frps-proxies/:name")
def api_delete_frps_proxy(ctx):
    """删除 frps 详情页里的某个代理。

    入口在 frps 实例详情页(那里能看到所有连上来的 frpc 的代理,含离线残留)。
    按代理名在本地所有 frpc 实例的数据库里查找定义,删掉 + 重生成配置 + 重启实例。
    代理不属于本机 frpm 管理的 frpc 时返回 409(需要去那个 frpc 所在的主机删)。
    """
    instance_id = int(ctx["params"]["id"])
    name = ctx["params"]["name"]
    inst = db.get_instance(instance_id)
    if not inst:
        return 404, {"error": "not found"}
    if inst["type"] != "frps":
        return 400, {"error": "not frps instance"}

    row, owner = _find_owner_by_name(name)
    if not row:
        return 409, {
            "error": f"代理 {name} 不属于本机 frpm 管理的任何 frpc",
            "hint": "请去该 frpc 所在主机(如 NAS)的 frpm 面板里删除",
        }

    db.delete_proxy(row["id"])
    _, ok, reg_err = _regenerate_frpc_config(owner["id"])
    if not ok:
        return 400, {"error": f"配置生成失败: {reg_err}"}
    restart_result = _restart_frpc_instance(owner)
    return 200, {"success": True, "deleted": name, "instance": owner["name"],
                 "restart": restart_result}


@app.post("/api/instances/:id/frps-proxies/batch-delete")
def api_batch_delete_frps_proxies(ctx):
    """批量删除 frps 详情页里的代理(按名字列表)。

    前端先拉 frps 实时状态挑出要删的代理名,POST 回来。
    逐个删除,返回成功/失败明细(部分失败不影响其他)。
    """
    instance_id = int(ctx["params"]["id"])
    inst = db.get_instance(instance_id)
    if not inst:
        return 404, {"error": "not found"}
    if inst["type"] != "frps":
        return 400, {"error": "not frps instance"}

    body = ctx["body"] or {}
    names = body.get("names") or []
    if not isinstance(names, list):
        return 400, {"error": "names 必须是字符串数组"}
    names = [str(n).strip() for n in names if str(n).strip()]
    if not names:
        return 400, {"error": "没有要删除的代理"}

    deleted, failed = [], []
    for name in names:
        row, owner = _find_owner_by_name(name)
        if not row:
            failed.append({"name": name, "error": "代理不属于本机 frpm 管理的任何 frpc"})
            continue
        try:
            db.delete_proxy(row["id"])
            _, ok, reg_err = _regenerate_frpc_config(owner["id"])
            if not ok:
                failed.append({"name": name, "error": f"配置生成失败: {reg_err}"})
                continue
            _restart_frpc_instance(owner)
            deleted.append({"name": name, "instance": owner["name"]})
        except Exception as e:
            failed.append({"name": name, "error": str(e)})

    return 200, {"deleted": deleted, "failed": failed}


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
    # 级联删除实例下所有代理规则
    deleted_proxies = db.delete_proxies_by_instance(instance_id)
    result["deleted"]["proxies"] = deleted_proxies
    if db.delete_instance(instance_id):
        result["deleted"]["db"] = instance_id
    return 200, result


@app.post("/api/instances/:id/start")
def api_start_instance(ctx):
    inst = db.get_instance(int(ctx["params"]["id"]))
    if not inst:
        return 404, {"error": "not found"}
    if inst["deploy_mode"] == "binary":
        return 200, frp_ops.start_binary_instance(inst["name"], inst["config_path"], inst["type"])
    return 200, frp_ops.start_instance(inst["name"])


@app.post("/api/instances/:id/stop")
def api_stop_instance(ctx):
    inst = db.get_instance(int(ctx["params"]["id"]))
    if not inst:
        return 404, {"error": "not found"}
    if inst["deploy_mode"] == "binary":
        return 200, frp_ops.stop_binary_instance(inst["name"], inst["config_path"])
    return 200, frp_ops.stop_instance(inst["name"])


@app.post("/api/instances/:id/proxies/:pid/nginx-conf")
def api_generate_nginx_conf(ctx):
    """为 frpc 代理生成 nginx 反向代理配置。

    入参:
      - ssl: bool, 是否启用 HTTPS(默认 true)
      - cert_path: str, ssl 开启时必填,如 /etc/ssl/frpm/example.crt
      - key_path: str, ssl 开启时必填,如 /etc/ssl/frpm/example.key
      - save: bool, 是否写入 /data/frpm/nginx/(默认 true)

    返回:
      - config: 完整 nginx server block 文本
      - host_path: 写入的宿主机路径(如 save=True)
    """
    instance_id = int(ctx["params"]["id"])
    proxy_id = int(ctx["params"]["pid"])
    inst, err = _get_frpc_instance(instance_id)
    if err:
        return err

    proxy = db.get_proxy(proxy_id)
    if not proxy or proxy["instance_id"] != instance_id:
        return 404, {"error": "代理不存在"}

    domains = proxy.get("custom_domains") or []
    if not domains:
        return 400, {"error": "代理未配置域名(custom_domains 为空),无法生成 nginx 配置"}

    body = ctx["body"] or {}
    ssl_enabled = bool(body.get("ssl", True))
    cert_path = (body.get("cert_path") or "").strip()
    key_path = (body.get("key_path") or "").strip()
    save = bool(body.get("save", True))

    if ssl_enabled:
        if not cert_path or not key_path:
            return 400, {"error": "SSL 已开启,必须填写证书路径和密钥路径"}

    # frpc 本地端口:用 local_port(代理目标);没有就 fallback 到 80
    local_port = proxy.get("local_port") or 80
    local_ip = proxy.get("local_ip") or "127.0.0.1"

    # 生成 nginx 配置
    lines = []
    if ssl_enabled:
        lines.append("server {")
        lines.append("    listen 80;")
        lines.append("    listen 443 ssl;")
        lines.append(f"    server_name {' '.join(domains)};")
        lines.append("")
        lines.append(f"    ssl_certificate     {cert_path};")
        lines.append(f"    ssl_certificate_key {key_path};")
        lines.append("    ssl_protocols TLSv1.2 TLSv1.3;")
        lines.append("    ssl_ciphers HIGH:!aNULL:!MD5;")
        lines.append("")
        lines.append("    location / {")
        lines.append(f"        proxy_pass http://{local_ip}:{local_port};")
    else:
        lines.append("server {")
        lines.append("    listen 80;")
        lines.append(f"    server_name {' '.join(domains)};")
        lines.append("")
        lines.append("    location / {")
        lines.append(f"        proxy_pass http://{local_ip}:{local_port};")
    lines.append("        proxy_http_version 1.1;")
    lines.append("        proxy_set_header Host $host;")
    lines.append("        proxy_set_header X-Real-IP $remote_addr;")
    lines.append("        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;")
    lines.append("        proxy_set_header X-Forwarded-Proto $scheme;")
    lines.append("        proxy_set_header Upgrade $http_upgrade;")
    lines.append("        proxy_set_header Connection $connection_upgrade;")
    lines.append("    }")
    lines.append("}")
    # 加 map 用于 WebSocket(放在文件头部)
    full_config = "# Auto-generated by frpm - " + time.strftime("%Y-%m-%d %H:%M:%S") + "\n"
    full_config += "# 域名: " + ", ".join(domains) + "\n"
    full_config += "# 代理: " + proxy["name"] + " (id=" + str(proxy_id) + ")\n\n"
    full_config += "map $http_upgrade $connection_upgrade {\n    default upgrade;\n    '' close;\n}\n\n"
    full_config += "\n".join(lines) + "\n"

    result = {
        "success": True,
        "config": full_config,
        "domains": domains,
        "ssl_enabled": ssl_enabled,
    }

    if save:
        try:
            target_dir = os.path.join(os.environ.get("FRPM_DATA_DIR", "/data/frpm"), "nginx")
            os.makedirs(target_dir, exist_ok=True)
            filename = proxy["name"].lower().replace(" ", "_") + ".conf"
            filepath = os.path.join(target_dir, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(full_config)
            result["filepath"] = filepath
            result["host_path"] = filepath  # frpm 容器用 host network + 挂载,路径相同
            result["message"] = f"配置已保存到 {filepath}"
        except Exception as e:
            result["error"] = f"写入文件失败: {e}"
            return 500, {"error": result["error"]}

    return 200, result


@app.post("/api/instances/:id/restart")
def api_restart_instance(ctx):
    inst = db.get_instance(int(ctx["params"]["id"]))
    if not inst:
        return 404, {"error": "not found"}
    if inst["deploy_mode"] == "binary":
        return 200, _restart_frpc_instance(inst)
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
    # 注意: transport_heartbeat_interval 是 frpc 的合法字段,不在 alias_map 里,
    # 直接走下方 valid 集合过滤。历史遗留曾把它映射到 None 丢弃,导致 frpc 不发心跳、
    # 控制连接被 frps 每 90 秒超时掐断,批量上传间歇报"网络连接异常"。
    alias_map = {
        'transport_heartbeat_ttl': 'transport_heartbeat_interval',
        'transport_connect_timeout': 'transport_dial_server_timeout',
        'http2_enabled': 'transport_http2_enabled',
        'allow_arbitrary_ports': None,
    }

    valid = {"server_addr", "server_port", "auth_method", "auth_token", "user",
             "login_fail_exit", "transport_tcp_mux", "transport_heartbeat_interval",
             "transport_dial_server_timeout", "transport_pool_count", "transport_http2_enabled",
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

    按类型分两批:先启动 frps,等 frps 的 bindPort 就绪后再启动 frpc,
    否则 frpc 因 loginFailExit=true 连不上 frps 会直接退出。
    """
    import sys
    def log(msg):
        print(msg, flush=True)
        sys.stdout.flush()

    log("[*] 自动恢复检查开始...")
    try:
        instances = db.list_instances()
    except Exception as e:
        log(f"[!] 自动恢复: 读取实例失败: {e}")
        return 0

    recovered = 0

    def _recover_one(inst):
        """恢复单个实例,返回是否成功启动。"""
        name = inst.get("name")
        config_path = inst.get("config_path")
        if not name or not config_path:
            return False
        try:
            status = frp_ops.get_binary_status(name, config_path)
            if not status.get("running", False):
                log(f"[*] 自动恢复实例: {name} (进程丢失)")
                result = frp_ops.start_binary_instance(name, config_path, inst.get("type"))
                if result.get("success"):
                    log(f"[*] 实例 {name} 已启动, pid={result.get('pid')}")
                    return True
                log(f"[!] 启动 {name} 失败: {result.get('error')}")
        except Exception as e:
            log(f"[!] 恢复 {name} 异常: {e}")
        return False

    # 第一批:启动所有 frps
    for inst in instances:
        if inst.get("type") == "frps":
            if _recover_one(inst):
                recovered += 1

    # 等 frps 端口就绪(只对有 frpc 实例时才需要等)
    if any(i.get("type") == "frpc" for i in instances):
        frps_bind_port = _get_first_frps_bind_port(instances)
        if frps_bind_port:
            log(f"[*] 等待 frps 端口 {frps_bind_port} 就绪...")
            if _wait_for_port(frps_bind_port, timeout=10.0):
                log(f"[*] frps 端口 {frps_bind_port} 已就绪")
            else:
                log(f"[!] 等待 frps 端口 {frps_bind_port} 超时(10s),仍尝试启动 frpc")

    # 第二批:启动所有 frpc
    for inst in instances:
        if inst.get("type") == "frpc":
            if _recover_one(inst):
                recovered += 1

    log(f"[*] 自动恢复完成: {recovered} 个实例已重启")
    return recovered


def _get_first_frps_bind_port(instances):
    """从 frps 实例配置里读取第一个 bindPort(用于自动恢复时等端口就绪)。"""
    for inst in instances:
        if inst.get("type") != "frps":
            continue
        try:
            with open(inst["config_path"], "rb") as f:
                cfg = tomllib.load(f)
            port = cfg.get("bindPort") or cfg.get("bind_port")
            if isinstance(port, int):
                return port
        except Exception:
            continue
    return None


def main():
    import sys
    def log(msg):
        print(msg, flush=True)
        sys.stdout.flush()
    db.init_db()
    port = int(os.environ.get("FRPM_PORT", "8080"))
    log(f"[*] FRP Manager 启动中... 监听 0.0.0.0:{port}")
    log(f"[*] 配置目录: {CONFIG_DIR}")
    log(f"[*] 数据库: {db.DB_PATH}")
    log(f"[*] 依赖: 仅使用 Python 标准库(无 Flask/psutil/docker)")
    # 启动时自动恢复 frps/frpc 实例(容器重建后进程会丢失)
    _auto_recover_instances()
    log(f"[*] HTTP 服务启动,开始接受请求...")
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
