"""frp 检测/安装/卸载逻辑。

支持两种部署模式:
1. docker: 通过 Docker API 管理 frp 容器(推荐,隔离好)
2. binary: 把 frp 二进制装到 /usr/local/bin,用 systemd 管理(需要宿主机权限)

Docker 模式通过 docker python SDK 操作,需要在容器外(或挂载 docker.sock)
才能工作。本工具设计为以"管理工具"身份运行,通过 docker.sock 控制 frp 容器。
"""

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class FrpStatus:
    """frp 状态探测结果。"""
    frps_installed: bool = False
    frps_version: Optional[str] = None
    frps_location: Optional[str] = None  # binary 路径或 docker 容器名
    frpc_installed: bool = False
    frpc_version: Optional[str] = None
    frpc_location: Optional[str] = None
    docker_available: bool = False
    docker_version: Optional[str] = None
    mode: Optional[str] = None  # 当前部署模式 docker / binary / None


def detect_frp() -> FrpStatus:
    """探测主机上的 frp 安装情况。

    按以下顺序探测:
    1. Docker 容器(名字前缀 frpm- / frp- 或镜像名含 frp)
    2. 本机 systemd 服务 frps/frpc
    3. 二进制在 PATH 中
    """
    status = FrpStatus()

    # 探测 Docker
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=8,
        )
        if r.returncode == 0:
            status.docker_available = True
            status.docker_version = r.stdout.strip()
    except Exception as e:
        status.docker_available = False
        status.docker_version = f"unavailable: {type(e).__name__}"

    if status.docker_available:
        # 找带 frpm.role 标签的容器
        try:
            r = subprocess.run(
                ["docker", "ps", "-a", "--filter", "label=maintainer=frp-manager",
                 "--format", "{{.Names}}\t{{.Labels}}\t{{.Image}}\t{{.Status}}"],
                capture_output=True, text=True, timeout=8,
            )
            for line in r.stdout.strip().splitlines():
                parts = line.split("\t")
                if len(parts) < 4:
                    continue
                cname, labels_str, image, st = parts[0], parts[1], parts[2], parts[3]
                role = None
                for kv in labels_str.split(","):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        if k == "frpm.role":
                            role = v.strip('"')
                if role == "frps" and not status.frps_installed:
                    status.frps_installed = True
                    status.frps_location = cname
                    # 从容器内拿版本
                    r2 = subprocess.run(
                        ["docker", "exec", cname, "/usr/local/bin/frps", "-v"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if r2.returncode == 0:
                        status.frps_version = r2.stdout.strip().split()[-1]
                if role == "frpc" and not status.frpc_installed:
                    status.frpc_installed = True
                    status.frpc_location = cname
                    r2 = subprocess.run(
                        ["docker", "exec", cname, "/usr/local/bin/frpc", "-v"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if r2.returncode == 0:
                        status.frpc_version = r2.stdout.strip().split()[-1]
        except Exception:
            pass

    # 探测本机 binary
    frps_bin = shutil.which("frps") or shutil.which("/usr/local/bin/frps")
    frpc_bin = shutil.which("frpc") or shutil.which("/usr/local/bin/frpc")
    if frps_bin and not status.frps_installed:
        status.frps_installed = True
        status.frps_version = _get_binary_version(frps_bin)
        status.frps_location = frps_bin
    if frpc_bin and not status.frpc_installed:
        status.frpc_installed = True
        status.frpc_version = _get_binary_version(frpc_bin)
        status.frpc_location = frpc_bin

    # 探测 systemd 服务(binary 模式)
    if not status.frps_installed and _systemd_service_active("frps"):
        status.frps_installed = True
        status.frps_location = "systemd:frps"
        status.frps_version = _get_binary_version("/usr/local/bin/frps") or "?"
    if not status.frpc_installed and _systemd_service_active("frpc"):
        status.frpc_installed = True
        status.frpc_location = "systemd:frpc"
        status.frpc_version = _get_binary_version("/usr/local/bin/frpc") or "?"

    # 决定主模式
    if status.docker_available:
        status.mode = "docker"
    elif status.frps_installed or status.frpc_installed:
        status.mode = "binary"
    else:
        status.mode = None

    return status


def _get_container_frp_version(container) -> Optional[str]:
    """从容器里执行 frps/frpc -v 拿版本。"""
    try:
        out = container.exec_run(["/usr/local/bin/frps", "-v"])
        if out.exit_code == 0:
            return out.output.decode().strip().split()[-1]
    except Exception:
        pass
    try:
        out = container.exec_run(["/usr/local/bin/frpc", "-v"])
        if out.exit_code == 0:
            return out.output.decode().strip().split()[-1]
    except Exception:
        pass
    return None


def _get_binary_version(bin_path: str) -> Optional[str]:
    """调用本地 frp 二进制获取版本。"""
    if not os.path.exists(bin_path):
        return None
    try:
        r = subprocess.run(
            [bin_path, "-v"], capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            return r.stdout.strip().split()[-1]
    except Exception:
        pass
    return None


def _systemd_service_active(name: str) -> bool:
    """检查 systemd 服务是否 active。"""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "--quiet", name],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


# === 安装逻辑 ===

FRP_VERSIONS = ["v0.61.1", "v0.60.0", "v0.59.0", "v0.58.0", "v0.57.0", "v0.56.0"]
DEFAULT_VERSION = FRP_VERSIONS[0]


def install_frp_binary(version: str = DEFAULT_VERSION,
                       install_type: str = "both",
                       install_dir: str = "/usr/local/bin") -> dict:
    """从 frp 官方 GitHub Releases 下载二进制并安装到宿主机。

    仅适用于 Docker 模式不可用的场景。需要 systemd 权限创建服务。
    """
    result = {"success": False, "version": version, "messages": []}
    target_dir = install_dir

    # 下载 URL 模板
    arch = "amd64" if os.uname().machine == "x86_64" else "arm64"
    base_url = (
        f"https://github.com/fatedier/frp/releases/download/{version}/"
        f"frp_{version}_linux_{arch}"
    )

    try:
        import requests
        r = requests.get(base_url, timeout=120, stream=True)
        if r.status_code != 200:
            raise RuntimeError(f"下载失败: HTTP {r.status_code}")
        with open("/tmp/frp.tar.gz", "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
        result["messages"].append(f"已下载 {os.path.getsize('/tmp/frp.tar.gz')} 字节")

        # 解压
        subprocess.run(
            ["tar", "xzf", "/tmp/frp.tar.gz", "-C", "/tmp"],
            check=True, timeout=60,
        )
        result["messages"].append("已解压")

        # 安装二进制
        for typ in ([install_type] if install_type in ("frps", "frpc") else ["frps", "frpc"]):
            src = f"/tmp/frp_{version}_linux_{arch}/{typ}"
            if not os.path.exists(src):
                raise FileNotFoundError(f"未找到 {src}")
            os.chmod(src, 0o755)
            shutil.copy(src, os.path.join(target_dir, typ))
            result["messages"].append(f"已安装 {typ} -> {target_dir}/{typ}")

        result["success"] = True
    except Exception as e:
        result["messages"].append(f"安装失败: {e}")
    finally:
        shutil.rmtree(f"/tmp/frp_{version}_linux_{arch}", ignore_errors=True)
        os.path.exists("/tmp/frp.tar.gz") and os.remove("/tmp/frp.tar.gz")

    return result


def install_frp_docker(image: str = "snowdreamtech/frps:latest",
                       image_c: str = "snowdreamtech/frpc:latest") -> dict:
    """拉取 frp Docker 镜像,供后续创建实例使用。"""
    result = {"success": True, "messages": []}
    for img_name, alias in [(image, "frps"), (image_c, "frpc")]:
        # 检查镜像是否已存在
        r = subprocess.run(
            ["docker", "image", "inspect", img_name],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            result["messages"].append(f"{img_name} 已存在")
            continue
        # 拉取
        try:
            r = subprocess.run(
                ["docker", "pull", img_name],
                capture_output=True, text=True, timeout=600,
            )
            if r.returncode == 0:
                result["messages"].append(f"已拉取 {img_name}")
            else:
                result["messages"].append(f"拉取 {img_name} 失败: {r.stderr.strip()[:200]}")
                result["success"] = False
        except Exception as e:
            result["messages"].append(f"拉取 {img_name} 失败: {e}")
            result["success"] = False
    return result


def uninstall_frp_binary() -> dict:
    """卸载本机 frp 二进制和 systemd 服务。"""
    result = {"success": True, "messages": []}
    for name in ("frps", "frpc"):
        try:
            subprocess.run(["systemctl", "stop", name], capture_output=True, timeout=10)
            subprocess.run(["systemctl", "disable", name], capture_output=True, timeout=10)
            os.path.exists(f"/etc/systemd/system/{name}.service") and \
                os.remove(f"/etc/systemd/system/{name}.service")
        except Exception as e:
            result["messages"].append(f"stop {name}: {e}")
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    for path in ("/usr/local/bin/frps", "/usr/local/bin/frpc"):
        if os.path.exists(path):
            os.remove(path)
            result["messages"].append(f"已删除 {path}")
    return result


# === 二进制部署模式:用 systemd 服务管理 ===

SYSTEMD_TEMPLATE = """[Unit]
Description={binary} instance: {name}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={bin_path} -c {config_path}
WorkingDirectory=/root
Restart=always
RestartSec=5
LimitNOFILE=65535
MemoryMax=512M

[Install]
WantedBy=multi-user.target
"""


def _systemd_service_name(instance_type: str, instance_name: str) -> str:
    """生成 systemd 服务名,确保唯一。"""
    # 例: frps-prod -> frps@prod (用 systemd template)
    # 简化:直接用 frpm-{name}
    return f"frpm-{instance_name}"


def _write_systemd_service(instance_name: str, instance_type: str,
                            bin_path: str, config_path: str) -> bool:
    """写入 systemd service 文件。"""
    service_name = _systemd_service_name(instance_type, instance_name)
    service_path = f"/etc/systemd/system/{service_name}.service"
    content = SYSTEMD_TEMPLATE.format(
        binary=instance_type,
        name=instance_name,
        bin_path=bin_path,
        config_path=config_path,
    )
    with open(service_path, "w") as f:
        f.write(content)
    # 重新加载 systemd
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=10)
    # 启用开机自启
    subprocess.run(["systemctl", "enable", service_name], capture_output=True, timeout=10)
    return True


def create_binary_instance(instance_name: str,
                            instance_type: str,
                            config_path: str,
                            bin_path: str = None) -> dict:
    """用 systemd 部署一个 frp 实例(binary 模式)。"""
    result = {"success": False}
    if not bin_path:
        bin_path = f"/usr/local/bin/{instance_type}"
    if not os.path.exists(bin_path):
        return {"success": False, "error": f"frp 二进制不存在: {bin_path}"}

    try:
        # 检查是否已有同名 service
        service_name = _systemd_service_name(instance_type, instance_name)
        r = subprocess.run(
            ["systemctl", "is-active", "--quiet", service_name],
            capture_output=True, timeout=5,
        )
        if r.returncode == 0:
            return {"success": False, "error": f"服务已存在: {service_name}"}

        # 写 service 文件并启动
        _write_systemd_service(instance_name, instance_type, bin_path, config_path)
        # 启动
        r = subprocess.run(
            ["systemctl", "start", service_name],
            capture_output=True, timeout=15,
        )
        if r.returncode != 0:
            return {"success": False, "error": f"启动失败: {r.stderr.decode() or r.stdout.decode()}"}

        # 等待一下,确认服务在运行
        time.sleep(0.5)
        r = subprocess.run(
            ["systemctl", "is-active", "--quiet", service_name],
            capture_output=True, timeout=5,
        )
        result = {
            "success": True,
            "service_name": service_name,
            "running": r.returncode == 0,
        }
    except Exception as e:
        result["error"] = str(e)
    return result


def get_binary_status(instance_name: str) -> dict:
    """查询 binary 模式实例的 systemd 状态。"""
    service_name = _systemd_service_name("frps", instance_name)
    # 也试试 frpc 命名
    for prefix in ("frps", "frpc"):
        sn = _systemd_service_name(prefix, instance_name)
        r = subprocess.run(
            ["systemctl", "show", sn, "--property=ActiveState,SubState,MainPID,ExecMainStatusTimestampMonotonic,NRestarts"],
            capture_output=True, timeout=5,
        )
        if r.returncode == 0:
            props = {}
            for line in r.stdout.decode().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k] = v
            if props.get("ActiveState") in ("active", "inactive", "failed"):
                return {
                    "running": props.get("ActiveState") == "active" and props.get("SubState") == "running",
                    "status": props.get("ActiveState"),
                    "exit_code": props.get("SubState") == "dead" and int(props.get("ExecMainCode", 0) or 0) or 0,
                    "started_at": props.get("ExecMainStartTimestamp", ""),
                    "restarts": int(props.get("NRestarts", 0) or 0),
                    "service_name": sn,
                }
    return {"running": False, "status": "not_found"}


def start_binary_instance(instance_name: str) -> dict:
    try:
        for prefix in ("frps", "frpc"):
            sn = _systemd_service_name(prefix, instance_name)
            r = subprocess.run(["systemctl", "start", sn], capture_output=True, timeout=15)
            if r.returncode == 0:
                return {"success": True, "service": sn}
        return {"success": False, "error": "服务未找到"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def stop_binary_instance(instance_name: str) -> dict:
    try:
        for prefix in ("frps", "frpc"):
            sn = _systemd_service_name(prefix, instance_name)
            r = subprocess.run(["systemctl", "stop", sn], capture_output=True, timeout=15)
            if r.returncode == 0:
                return {"success": True, "service": sn}
        return {"success": False, "error": "服务未找到"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def restart_binary_instance(instance_name: str) -> dict:
    try:
        for prefix in ("frps", "frpc"):
            sn = _systemd_service_name(prefix, instance_name)
            r = subprocess.run(["systemctl", "restart", sn], capture_output=True, timeout=15)
            if r.returncode == 0:
                return {"success": True, "service": sn}
        return {"success": False, "error": "服务未找到"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_binary_logs(instance_name: str, lines: int = 200) -> str:
    """获取 binary 实例日志(从 journalctl)。"""
    try:
        for prefix in ("frps", "frpc"):
            sn = _systemd_service_name(prefix, instance_name)
            r = subprocess.run(
                ["journalctl", "-u", sn, "-n", str(lines), "--no-pager", "-o", "short-iso"],
                capture_output=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.decode("utf-8", errors="replace")
        return f"未找到服务 {instance_name} 的日志"
    except Exception as e:
        return f"读取日志失败: {e}"


def delete_binary_instance(instance_name: str, remove_config: bool = True) -> dict:
    """删除 binary 实例(service + 可选配置文件)。"""
    result = {"success": True}
    try:
        for prefix in ("frps", "frpc"):
            sn = _systemd_service_name(prefix, instance_name)
            subprocess.run(["systemctl", "stop", sn], capture_output=True, timeout=10)
            subprocess.run(["systemctl", "disable", sn], capture_output=True, timeout=10)
            svc_path = f"/etc/systemd/system/{sn}.service"
            if os.path.exists(svc_path):
                os.remove(svc_path)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=10)
        result["deleted_service"] = True
    except Exception as e:
        result["service_error"] = str(e)
    return result


# === 实例操作 ===

def create_frp_container(instance_name: str,
                          instance_type: str,
                          config_path: str,
                          image: Optional[str] = None,
                          port_bindings: Optional[dict] = None,
                          extra_env: Optional[dict] = None) -> dict:
    """创建一个 frp 实例容器(用 docker CLI)。

    :param instance_name: 唯一实例名(用于 docker 容器名和 label)
    :param instance_type: 'frps' 或 'frpc'
    :param config_path: 宿主机上的配置文件绝对路径(容器内挂载为 /etc/frp/frp.toml)
    :param image: Docker 镜像(默认按类型选)
    :return: {success, container_name, state, error?}
    """
    image = image or (
        "snowdreamtech/frps:latest" if instance_type == "frps"
        else "snowdreamtech/frpc:latest"
    )
    result = {"success": False}

    # 检查容器是否已存在
    r = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^{instance_name}$",
         "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=8,
    )
    if instance_name in r.stdout.strip():
        return {"success": False, "error": f"容器已存在: {instance_name}"}

    # 准备 volume
    config_path_abs = os.path.abspath(config_path)
    log_dir = f"/var/lib/frpm/{instance_name}/logs"
    os.makedirs(log_dir, exist_ok=True)

    # 网络模式
    network_args = []
    if instance_type == "frps":
        # bridge 模式,解析 bindPort 做端口映射
        import re
        try:
            with open(config_path_abs) as f:
                m = re.search(r'bindPort\s*=\s*(\d+)', f.read())
            bp = int(m.group(1)) if m else 7000
            network_args = ["-p", f"{bp}:{bp}"]
        except Exception:
            network_args = ["-p", "7000:7000"]
    else:
        # host 模式继承宿主机 DNS
        network_args = ["--network", "host"]

    # 构造 docker run 命令
    cmd = [
        "docker", "run", "-d",
        "--name", instance_name,
        "--restart", "unless-stopped",
        "-v", f"{config_path_abs}:/etc/frp/frp.toml:ro",
        "-v", f"{log_dir}:/frp/logs",
        "--log-driver", "json-file",
        "--log-opt", "max-size=10m",
        "--log-opt", "max-file=3",
        "--hostname", instance_name,
        "--label", "frpm.role=" + instance_type,
        "--label", "frpm.instance=" + instance_name,
        "--label", "maintainer=frp-manager",
    ]
    cmd.extend(network_args)
    cmd.extend([image, "-c", "/etc/frp/frp.toml"])

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return {"success": False, "error": r.stderr.strip() or r.stdout.strip()[:500]}
        # 拿到 container id
        container_id = r.stdout.strip().split()[0] if r.stdout.strip() else ""
        # 检查状态
        r2 = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", instance_name],
            capture_output=True, text=True, timeout=8,
        )
        state = r2.stdout.strip() if r2.returncode == 0 else "unknown"
        result = {
            "success": True,
            "container_id": container_id,
            "container_name": instance_name,
            "state": state,
        }
    except Exception as e:
        result["error"] = str(e)
    return result


def get_instance_container(instance_name: str) -> str:
    """返回容器 ID(字符串),用于 docker logs/exec。"""
    r = subprocess.run(
        ["docker", "inspect", "--format", "{{.Id}}", instance_name],
        capture_output=True, text=True, timeout=5,
    )
    if r.returncode != 0:
        raise RuntimeError(f"容器不存在: {instance_name}")
    return r.stdout.strip()


def get_container_status(instance_name: str) -> dict:
    """获取容器状态。"""
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format",
             "Running={{.State.Running}} Status={{.State.Status}} ExitCode={{.State.ExitCode}} "
             "StartedAt={{.State.StartedAt}} FinishedAt={{.State.FinishedAt}} PID={{.State.Pid}}",
             instance_name],
            capture_output=True, text=True, timeout=8,
        )
        if r.returncode != 0:
            return {"running": False, "status": "not_found"}
        props = {}
        for kv in r.stdout.strip().split():
            if "=" in kv:
                k, v = kv.split("=", 1)
                props[k] = v
        return {
            "running": props.get("Running") == "true",
            "status": props.get("Status"),
            "exit_code": int(props.get("ExitCode") or 0),
            "started_at": props.get("StartedAt"),
            "finished_at": props.get("FinishedAt"),
            "pid": int(props.get("PID") or 0),
        }
    except Exception as e:
        return {"running": False, "status": f"error: {e}"}


def get_container_logs(instance_name: str, lines: int = 200,
                        since: int = 0, follow: bool = False) -> str:
    """获取容器日志。"""
    try:
        cmd = ["docker", "logs", "--timestamps", "-t", str(lines), instance_name]
        if since:
            cmd.insert(2, "--since")
            cmd.insert(3, str(since))
        r = subprocess.run(cmd, capture_output=True, timeout=15)
        return r.stdout.decode("utf-8", errors="replace")
    except Exception as e:
        return f"读取日志失败: {e}"


def stream_container_logs(instance_name: str):
    """流式获取容器日志(生成器)。"""
    import subprocess as sp
    try:
        proc = sp.Popen(
            ["docker", "logs", "-f", "--timestamps", instance_name],
            stdout=sp.PIPE, stderr=sp.STDOUT,
        )
        try:
            for line in iter(proc.stdout.readline, b""):
                yield line.decode("utf-8", "replace")
        finally:
            proc.kill()
            proc.wait(timeout=3)
    except Exception as e:
        yield f"[error] {e}\n"


def start_instance(instance_name: str) -> dict:
    r = subprocess.run(["docker", "start", instance_name],
                       capture_output=True, text=True, timeout=15)
    if r.returncode == 0:
        return {"success": True, "container_name": instance_name}
    return {"success": False, "error": r.stderr.strip() or r.stdout.strip()[:200]}


def stop_instance(instance_name: str) -> dict:
    r = subprocess.run(["docker", "stop", "-t", "10", instance_name],
                       capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        return {"success": True, "container_name": instance_name}
    return {"success": False, "error": r.stderr.strip() or r.stdout.strip()[:200]}


def restart_instance(instance_name: str) -> dict:
    r = subprocess.run(["docker", "restart", "-t", "10", instance_name],
                       capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        return {"success": True, "container_name": instance_name}
    return {"success": False, "error": r.stderr.strip() or r.stdout.strip()[:200]}


def delete_instance_container(instance_name: str, remove_volume: bool = False) -> dict:
    """删除 frp 实例容器。"""
    try:
        r = subprocess.run(["docker", "stop", "-t", "10", instance_name],
                           capture_output=True, text=True, timeout=20)
        r = subprocess.run(["docker", "rm", "-f", instance_name],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return {"success": True}
        return {"success": False, "error": r.stderr.strip() or r.stdout.strip()[:200]}
    except Exception as e:
        return {"success": False, "error": str(e)}


def list_frp_containers() -> list:
    """列出所有由 FRP Manager 创建的 frp 容器。"""
    result = []
    try:
        r = subprocess.run(
            ["docker", "ps", "-a", "--filter", "label=maintainer=frp-manager",
             "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}\t{{.Labels}}"],
            capture_output=True, text=True, timeout=8,
        )
        for line in r.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            name, image, status, ports, labels = parts
            role = ""
            for kv in labels.split(","):
                if kv.startswith("frpm.role="):
                    role = kv.split("=", 1)[1].strip('"')
            result.append({
                "name": name,
                "image": image,
                "running": status.startswith("Up"),
                "status": status,
                "ports": ports,
                "type": role,
            })
    except Exception as e:
        result.append({"error": str(e)})
    return result
