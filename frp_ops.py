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


# === 内置 frp 二进制解析 ===
# frp 二进制已打包在项目 bin/ 目录(amd64 + arm64),无需用户单独安装。
# 优先用内置二进制;找不到再回退到系统 PATH / /usr/local/bin。
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(SCRIPT_DIR, "bin")


def builtin_binary(frp_type: str) -> Optional[str]:
    """返回内置 frp 二进制路径(自动按架构选 amd64/arm64)。

    frp_type: "frpc" 或 "frps"。找不到返回 None。
    """
    if not os.path.isdir(BIN_DIR):
        return None
    machine = os.uname().machine
    is_arm = machine in ("aarch64", "arm64")
    # 命名约定: amd64 -> frpc, arm64 -> frpc-arm64
    candidates = [
        os.path.join(BIN_DIR, f"{frp_type}-arm64" if is_arm else frp_type),
        os.path.join(BIN_DIR, frp_type),
        os.path.join(BIN_DIR, f"{frp_type}-arm64"),
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def resolve_binary(frp_type: str) -> Optional[str]:
    """解析 frp 可执行路径:内置 bin/ > 系统 PATH > /usr/local/bin。"""
    return (
        builtin_binary(frp_type)
        or shutil.which(frp_type)
        or (shutil.which(f"/usr/local/bin/{frp_type}")
            if os.path.exists(f"/usr/local/bin/{frp_type}") else None)
    )


# === 进程管理(binary 模式:容器内直接 nohup frps/frpc,不用 systemctl) ===
# frpm 跑在 Docker 容器里,容器内没有 systemd/systemctl。
# 所以 binary 模式改为:用 start_new_session 把 frp 放到独立会话里直接起,
# 用 PID 文件跟踪进程,用 /proc 文件系统查进程存活(slim 镜像没有 pgrep/pkill)。
# 注意:frpm 和 frp 是同一个容器,容器重建时 frp 进程会一起消失(这是设计如此)。

RUNTIME_DIR = os.environ.get(
    "FRPM_RUNTIME_DIR",
    "/var/lib/frpm" if os.path.isdir("/var/lib/frpm") else "/data/frpm",
)
PROC_DIR = os.path.join(RUNTIME_DIR, "proc")
LOG_DIR = os.environ.get("FRPM_LOG_DIR", "/data/frpm/logs")


def _log_dir() -> str:
    """确保日志目录存在并返回路径。"""
    os.makedirs(LOG_DIR, exist_ok=True)
    return LOG_DIR


def _pidfile(instance_name: str) -> str:
    """确保 proc 目录存在并返回该实例的 PID 文件路径。"""
    os.makedirs(PROC_DIR, exist_ok=True)
    return os.path.join(PROC_DIR, f"{instance_name}.pid")


def _running_frps_cmdline() -> dict:
    """扫描 /proc,找出正在运行的 frp 进程。

    返回 {pid: cmdline_string}。用于容器重启后(无 PID 文件时)仍能查到 frp 进程。
    """
    found = {}
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    cmdline = f.read().replace(b"\x00", b" ").decode(errors="replace")
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            # 跳过 grep 自身,只匹配 frps / frpc 作为主程序
            if "grep" in cmdline:
                continue
            # cmdline 形如 "/app/bin/frps -c /path/to/config.toml"
            parts = cmdline.split()
            if parts and parts[0].rstrip("/") in ("/app/bin/frps", "/app/bin/frpc", "frps", "frpc"):
                found[int(entry)] = cmdline
    except Exception:
        pass
    return found


def _find_pid_by_config(instance_name: str, config_path: str) -> Optional[int]:
    """按配置文件路径匹配运行中的 frp 进程 PID。"""
    cfg = os.path.abspath(config_path) if config_path else None
    for pid, cmdline in _running_frps_cmdline().items():
        if cfg and cfg in cmdline:
            return pid
    return None


def _is_zombie(pid: int) -> bool:
    """检查 pid 是否是僵尸进程(Z state)。
    
    僵尸进程:进程已死但父进程没 wait(),内核保留 PID 表项。
    os.kill(pid, 0) 对僵尸进程会返回成功(进程表项还在),
    所以必须额外读 /proc/<pid>/stat 确认 state。
    """
    try:
        with open(f"/proc/{pid}/stat") as f:
            # stat 格式: pid (comm) state ppid ...
            # comm 可能含空格和括号,所以从最后一个 ')' 后取
            content = f.read()
            after_paren = content.rsplit(')', 1)[-1]
            fields = after_paren.split()
            if fields:
                return fields[0] in ('Z', 'z')
    except (OSError, FileNotFoundError):
        return False
    return False


def _read_pid(instance_name: str, config_path: str = None) -> Optional[int]:
    """获取实例当前 PID:先读 PID 文件,失效则按配置文件匹配进程。"""
    pf = _pidfile(instance_name)
    if os.path.exists(pf):
        try:
            with open(pf) as f:
                pid = int(f.read().strip())
            # 验证进程是否还活着(排除僵尸进程)
            try:
                os.kill(pid, 0)  # 信号 0 仅探测存活,不杀进程
                if _is_zombie(pid):
                    # 僵尸进程:进程已死但父进程没回收,删除 PID 文件
                    os.remove(pf)
                    return None
                return pid
            except (ProcessLookupError, PermissionError):
                os.remove(pf)
        except (ValueError, OSError):
            pass
    # PID 文件无效时按配置文件路径匹配
    return _find_pid_by_config(instance_name, config_path)


def _write_pid(instance_name: str, pid: int) -> None:
    """写入 PID 文件。"""
    try:
        with open(_pidfile(instance_name), "w") as f:
            f.write(str(pid))
    except OSError:
        pass


def _kill_process(pid: int) -> bool:
    """终止进程(先 TERM 优雅退出,超时则 KILL)。"""
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
        # 等最多 3 秒
        for _ in range(15):
            try:
                os.kill(pid, 0)
                time.sleep(0.2)
            except ProcessLookupError:
                return True
        os.kill(pid, signal.SIGKILL)
        return True
    except (ProcessLookupError, PermissionError):
        return True
    except Exception:
        return False


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

    # 探测 frp 二进制(优先内置 bin/ > 系统 PATH > /usr/local/bin)
    frps_bin = resolve_binary("frps")
    frpc_bin = resolve_binary("frpc")
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
        status.frps_version = _get_binary_version(resolve_binary("frps")) or "?"
    if not status.frpc_installed and _systemd_service_active("frpc"):
        status.frpc_installed = True
        status.frpc_location = "systemd:frpc"
        status.frpc_version = _get_binary_version(resolve_binary("frpc")) or "?"

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


def create_binary_instance(instance_name: str,
                            instance_type: str,
                            config_path: str,
                            bin_path: str = None) -> dict:
    """容器内直接启动一个 frp 进程(binary 模式,不用 systemd)。

    用 setsid + nohup 把 frp 放到独立会话里后台运行,PID 写入 proc 目录,
    日志写到 LOG_DIR。容器重启后通过 /proc 扫描恢复进程状态。
    """
    result = {"success": False}
    if not bin_path:
        bin_path = resolve_binary(instance_type)
    if not bin_path:
        return {"success": False, "error": f"未找到 frp 二进制: {instance_type}"}
    if not os.path.isfile(bin_path):
        return {"success": False, "error": f"frp 二进制不存在: {bin_path}"}
    if not os.path.isfile(config_path):
        return {"success": False, "error": f"配置文件不存在: {config_path}"}

    try:
        # 检查是否已有同名进程在跑
        if _read_pid(instance_name, config_path):
            return {"success": False, "error": f"实例已在运行: {instance_name}"}

        # 日志文件
        log_path = os.path.join(_log_dir(), f"{instance_name}.log")
        with open(log_path, "ab") as logf:
            # setsid 创建独立会话 + nohup 防 SIGHUP,frp 进程脱离 frpm 独立运行
            proc = subprocess.Popen(
                [bin_path, "-c", config_path],
                stdout=logf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd="/",
                start_new_session=True,  # 新会话,脱离 frpm 父进程,防 frpm 退出时连带杀掉
            )
        pid = proc.pid
        _write_pid(instance_name, pid)

        # 等一会儿确认进程还活着
        time.sleep(0.6)
        still_running = False
        try:
            os.kill(pid, 0)
            still_running = True
        except ProcessLookupError:
            still_running = False

        if not still_running:
            # 进程已退出,读日志找原因
            try:
                with open(log_path) as f:
                    err = f.read()[-500:]
            except OSError:
                err = ""
            return {"success": False, "error": f"启动后进程立即退出: {err}"}

        result = {
            "success": True,
            "pid": pid,
            "bin_path": bin_path,
            "log_path": log_path,
        }
    except Exception as e:
        result["error"] = str(e)
    return result


def get_binary_status(instance_name: str, config_path: str = None) -> dict:
    """查询 binary 实例进程状态(扫 /proc,不依赖 systemctl)。"""
    pid = _read_pid(instance_name, config_path)
    if pid is None:
        return {"running": False, "status": "stopped"}
    return {"running": True, "status": "running", "pid": pid}


def start_binary_instance(instance_name: str, config_path: str = None) -> dict:
    """启动 binary 实例(容器内 nohup frp 进程)。"""
    # config_path 必须提供(从数据库实例记录里取)
    if not config_path:
        return {"success": False, "error": "缺少配置文件路径,无法启动"}
    # 找实例类型:从配置路径所在实例推断,这里直接用 frps/frpc 都试
    for frp_type in ("frps", "frpc"):
        bin_path = resolve_binary(frp_type)
        if bin_path and os.path.isfile(config_path):
            r = create_binary_instance(instance_name, frp_type, config_path, bin_path)
            return r
    return {"success": False, "error": "未找到 frp 二进制或配置文件"}


def stop_binary_instance(instance_name: str, config_path: str = None) -> dict:
    """停止 binary 实例(杀进程)。"""
    pid = _read_pid(instance_name, config_path)
    if pid is None:
        return {"success": False, "error": "实例未在运行"}
    if _kill_process(pid):
        # 清 PID 文件
        try:
            os.remove(_pidfile(instance_name))
        except OSError:
            pass
        return {"success": True, "pid": pid}
    return {"success": False, "error": "停止失败"}


def restart_binary_instance(instance_name: str, config_path: str = None) -> dict:
    """重启 binary 实例。"""
    stop_binary_instance(instance_name, config_path)
    time.sleep(0.3)
    return start_binary_instance(instance_name, config_path)


def get_binary_logs(instance_name: str, lines: int = 200) -> str:
    """获取 binary 实例日志(从 LOG_DIR 下的日志文件)。"""
    log_path = os.path.join(_log_dir(), f"{instance_name}.log")
    if not os.path.isfile(log_path):
        return f"未找到日志文件: {log_path}"
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return "".join(all_lines[-lines:])
    except Exception as e:
        return f"读取日志失败: {e}"


def delete_binary_instance(instance_name: str, remove_config: bool = True) -> dict:
    """删除 binary 实例(停进程 + 清 PID + 可选删配置)。"""
    result = {"success": True}
    try:
        pid = _read_pid(instance_name)
        if pid:
            _kill_process(pid)
        try:
            os.remove(_pidfile(instance_name))
        except OSError:
            pass
        result["deleted_process"] = pid is not None
    except Exception as e:
        result["process_error"] = str(e)
    return result


# === 实例操作 ===

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
