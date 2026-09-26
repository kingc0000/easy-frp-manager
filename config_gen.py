"""frp 配置生成器:把 UI 表单数据转换成合法的 frp TOML 配置。

严格匹配 frp v0.61.1 官方配置格式(参考 conf/frps_full_example.toml 和 frpc_full_example.toml)。
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class FrpsConfig:
    """frps 服务端配置。"""
    bind_addr: str = "0.0.0.0"
    bind_port: int = 2005
    kcp_bind_port: Optional[int] = None
    proxy_bind_addr: Optional[str] = None
    vhost_http_port: Optional[int] = None
    vhost_https_port: Optional[int] = None
    auth_method: str = "token"
    auth_token: str = ""
    transport_tcp_mux: Optional[bool] = True
    transport_heartbeat_timeout: Optional[int] = None
    transport_tcp_keepalive: Optional[int] = None
    transport_max_pool_count: Optional[int] = None
    transport_tls_force: Optional[bool] = None
    transport_tls_cert_file: Optional[str] = None
    transport_tls_key_file: Optional[str] = None
    log_to: Optional[str] = None
    log_level: str = "info"
    log_max_days: int = 3
    web_server_addr: Optional[str] = None
    web_server_port: Optional[int] = None
    web_server_user: Optional[str] = "admin"
    web_server_password: Optional[str] = None
    enable_prometheus: Optional[bool] = False
    user_conn_timeout: Optional[int] = None
    detailed_errors_to_client: Optional[bool] = True
    allow_ports: Optional[str] = None

    def to_toml(self) -> str:
        lines = [
            "# frps 服务端配置 - 由 FRP Manager 自动生成",
            "# 修改后请重启实例生效",
            "",
            f'bindAddr = "{self.bind_addr}"',
            f'bindPort = {self.bind_port}',
        ]
        if self.kcp_bind_port:
            lines.append(f'kcpBindPort = {self.kcp_bind_port}')
        if self.proxy_bind_addr:
            lines.append(f'proxyBindAddr = "{self.proxy_bind_addr}"')
        if self.vhost_http_port:
            lines.append(f'vhostHTTPPort = {self.vhost_http_port}')
        if self.vhost_https_port:
            lines.append(f'vhostHTTPSPort = {self.vhost_https_port}')

        # 认证
        lines += ["", f'auth.method = "{self.auth_method}"']
        if self.auth_token:
            lines.append(f'auth.token = "{self.auth_token}"')

        # 传输
        if self.transport_tcp_mux is not None:
            lines.append(f'transport.tcpMux = {"true" if self.transport_tcp_mux else "false"}')
        if self.transport_heartbeat_timeout is not None:
            lines.append(f'transport.heartbeatTimeout = {self.transport_heartbeat_timeout}')
        if self.transport_tcp_keepalive is not None:
            lines.append(f'transport.tcpKeepalive = {self.transport_tcp_keepalive}')
        if self.transport_max_pool_count is not None:
            lines.append(f'transport.maxPoolCount = {self.transport_max_pool_count}')
        if self.transport_tls_force is not None:
            lines.append(f'transport.tls.force = {"true" if self.transport_tls_force else "false"}')
        if self.transport_tls_cert_file:
            lines.append(f'transport.tls.certFile = "{self.transport_tls_cert_file}"')
        if self.transport_tls_key_file:
            lines.append(f'transport.tls.keyFile = "{self.transport_tls_key_file}"')

        # 日志
        # log.to 默认为 None(不写),frp 默认输出到 stdout,被 frp_ops 重定向到 /data/frpm/logs/
        # 只有用户显式指定 log_to 时才写进配置
        if self.log_to:
            lines.append("")
            lines.append(f'log.to = "{self.log_to}"')
            lines.append(f'log.level = "{self.log_level}"')
            lines.append(f'log.maxDays = {self.log_max_days}')

        # Dashboard / WebServer
        if self.web_server_addr or self.web_server_port:
            lines.append("")
            if self.web_server_addr:
                lines.append(f'webServer.addr = "{self.web_server_addr}"')
            if self.web_server_port:
                lines.append(f'webServer.port = {self.web_server_port}')
            if self.web_server_user:
                lines.append(f'webServer.user = "{self.web_server_user}"')
            if self.web_server_password:
                lines.append(f'webServer.password = "{self.web_server_password}"')

        if self.enable_prometheus is not None:
            lines.append(f'enablePrometheus = {"true" if self.enable_prometheus else "false"}')
        if self.user_conn_timeout is not None:
            lines.append(f'userConnTimeout = {self.user_conn_timeout}')
        if self.detailed_errors_to_client is not None:
            lines.append(f'detailedErrorsToClient = {"true" if self.detailed_errors_to_client else "false"}')
        if self.allow_ports:
            # allowPorts 是 PortsRange 数组,如 [ {start = 10000, end = 20000}, {start = 22, end = 22} ]
            lines.append(f'allowPorts = {self.allow_ports}')

        return "\n".join(lines) + "\n"


@dataclass
class Proxy:
    """单条 frp 代理配置。"""
    name: str
    type: str  # tcp, udp, http, https, stcp, xtcp, tcpmux
    local_ip: str = "127.0.0.1"
    local_port: int = 0
    remote_port: Optional[int] = None
    custom_domains: List[str] = field(default_factory=list)
    subdomain: Optional[str] = None
    secret_key: Optional[str] = None  # stcp/xtcp 用
    host_header_rewrite: Optional[str] = None  # frp 的 hostHeaderRewrite
    locations: List[str] = field(default_factory=list)
    http_user: Optional[str] = None
    http_password: Optional[str] = None  # frp 实际是 httpPassword
    plugin_name: Optional[str] = None
    plugin_config: Optional[str] = None
    plugin_local_addr: Optional[str] = None
    request_headers: dict = field(default_factory=dict)
    response_headers: dict = field(default_factory=dict)
    metadatas: dict = field(default_factory=dict)
    annotations: dict = field(default_factory=dict)
    load_balancer_group: Optional[str] = None
    load_balancer_group_key: Optional[str] = None
    transport_bandwidth_limit: Optional[str] = None
    transport_use_encryption: Optional[bool] = None
    transport_use_compression: Optional[bool] = None

    def to_toml_block(self) -> str:
        """生成 [[proxies]] 块。"""
        lines = ["", "[[proxies]]"]
        lines.append(f'name = "{self.name}"')
        lines.append(f'type = "{self.type}"')

        if self.type in ("tcp", "udp"):
            lines.append(f'localIP = "{self.local_ip}"')
            lines.append(f'localPort = {self.local_port}')
            if self.remote_port:
                lines.append(f'remotePort = {self.remote_port}')

        if self.type == "http":
            lines.append(f'localIP = "{self.local_ip}"')
            lines.append(f'localPort = {self.local_port}')
            if self.subdomain:
                lines.append(f'subdomain = "{self.subdomain}"')
            if self.custom_domains:
                lines.append("customDomains = [" + ", ".join(f'"{d}"' for d in self.custom_domains) + "]")
            if self.locations:
                lines.append("locations = [" + ", ".join(f'"{l}"' for l in self.locations) + "]")
            if self.host_header_rewrite:
                lines.append(f'hostHeaderRewrite = "{self.host_header_rewrite}"')
            if self.http_user:
                lines.append(f'httpUser = "{self.http_user}"')
            if self.http_password:
                lines.append(f'httpPassword = "{self.http_password}"')
            if self.request_headers:
                for k, v in self.request_headers.items():
                    lines.append(f'requestHeaders.set.{k} = "{v}"')
            if self.response_headers:
                for k, v in self.response_headers.items():
                    lines.append(f'responseHeaders.set.{k} = "{v}"')

        if self.type == "https":
            lines.append(f'localIP = "{self.local_ip}"')
            lines.append(f'localPort = {self.local_port}')
            if self.subdomain:
                lines.append(f'subdomain = "{self.subdomain}"')
            if self.custom_domains:
                lines.append("customDomains = [" + ", ".join(f'"{d}"' for d in self.custom_domains) + "]")

        if self.type in ("stcp", "xtcp"):
            lines.append(f'localIP = "{self.local_ip}"')
            lines.append(f'localPort = {self.local_port}')
            if self.secret_key:
                lines.append(f'secretKey = "{self.secret_key}"')

        # 插件
        if self.plugin_name:
            lines.append(f'plugin_name = "{self.plugin_name}"')
            if self.plugin_config:
                lines.append(f'plugin_config = "{self.plugin_config}"')
            if self.plugin_local_addr:
                lines.append(f'plugin_local_addr = "{self.plugin_local_addr}"')

        return "\n".join(lines)


@dataclass
class ServerEntry:
    """frpc 的 server 入口(用于 v0.51+ 多 server 场景)。"""
    name: str
    serverAddr: str
    serverPort: int
    token: str = ""

    def to_toml_block(self) -> str:
        lines = ["", "[[servers]]"]
        lines.append(f'name = "{self.name}"')
        lines.append(f'serverAddr = "{self.serverAddr}"')
        lines.append(f'serverPort = {self.serverPort}')
        if self.token:
            lines.append(f'token = "{self.token}"')
        return "\n".join(lines)


@dataclass
class FrpcConfig:
    """frpc 客户端配置。"""
    # 单 server 模式(顶层字段)
    server_addr: Optional[str] = None
    server_port: Optional[int] = None
    auth_token: str = ""
    # 多 server 模式(用 [[servers]] 表,注意 frp v0.61.1 实际不接受这个语法,需要 v0.51+ 但格式不同)
    # 实际多 server 用顶层 serverAddr/serverPort + 多个 frpc 进程,或用 [[proxies]] 不同 server 配置
    auth_method: str = "token"
    user: Optional[str] = None
    login_fail_exit: bool = True
    transport_tcp_mux: Optional[bool] = True
    transport_dial_server_timeout: Optional[int] = None
    transport_pool_count: Optional[int] = None
    transport_http2_enabled: Optional[bool] = None
    transport_tls_enable: Optional[bool] = None
    transport_tls_server_name: Optional[str] = None
    transport_tls_skip_verify: Optional[bool] = None
    transport_tls_ca_file: Optional[str] = None
    transport_tls_cert_file: Optional[str] = None
    transport_tls_key_file: Optional[str] = None
    transport_bandwidth_limit: Optional[str] = None
    log_to: Optional[str] = None
    log_level: str = "info"
    log_max_days: int = 3
    web_server_addr: Optional[str] = None
    web_server_port: Optional[int] = None
    web_server_user: Optional[str] = "admin"
    web_server_password: Optional[str] = None
    proxies: List[Proxy] = field(default_factory=list)

    def to_toml(self) -> str:
        lines = [
            "# frpc 客户端配置 - 由 FRP Manager 自动生成",
            "# 修改后请重启实例生效",
            "",
        ]

        if self.user:
            lines.append(f'user = "{self.user}"')
            lines.append("")

        # server 地址(顶层字段)
        if self.server_addr and self.server_port:
            lines.append(f'serverAddr = "{self.server_addr}"')
            lines.append(f'serverPort = {self.server_port}')
            lines.append("")

        # 认证
        if self.auth_method:
            lines.append(f'auth.method = "{self.auth_method}"')
            if self.auth_token:
                lines.append(f'auth.token = "{self.auth_token}"')
            lines.append("")

        if self.login_fail_exit is not None:
            lines.append(f'loginFailExit = {"true" if self.login_fail_exit else "false"}')

        # 传输
        if self.transport_tcp_mux is not None:
            lines.append(f'transport.tcpMux = {"true" if self.transport_tcp_mux else "false"}')
        if self.transport_dial_server_timeout is not None:
            lines.append(f'transport.dialServerTimeout = {self.transport_dial_server_timeout}')
        if self.transport_pool_count is not None:
            lines.append(f'transport.poolCount = {self.transport_pool_count}')
        if self.transport_http2_enabled is not None:
            lines.append(f'transport.http2.enabled = {"true" if self.transport_http2_enabled else "false"}')
        if self.transport_tls_enable is not None:
            lines.append(f'transport.tls.enable = {"true" if self.transport_tls_enable else "false"}')
        if self.transport_tls_server_name:
            lines.append(f'transport.tls.serverName = "{self.transport_tls_server_name}"')
        if self.transport_tls_skip_verify is not None:
            lines.append(f'transport.tls.skipVerify = {"true" if self.transport_tls_skip_verify else "false"}')
        if self.transport_tls_ca_file:
            lines.append(f'transport.tls.caFile = "{self.transport_tls_ca_file}"')
        if self.transport_tls_cert_file:
            lines.append(f'transport.tls.certFile = "{self.transport_tls_cert_file}"')
        if self.transport_tls_key_file:
            lines.append(f'transport.tls.keyFile = "{self.transport_tls_key_file}"')
        if self.transport_bandwidth_limit:
            lines.append(f'transport.bandwidthLimit = "{self.transport_bandwidth_limit}"')

        # 日志
        # log.to 默认为 None(不写),frp 默认输出到 stdout,被 frp_ops 重定向到 /data/frpm/logs/
        # 只有用户显式指定 log_to 时才写进配置
        if self.log_to:
            lines.append("")
            lines.append(f'log.to = "{self.log_to}"')
            lines.append(f'log.level = "{self.log_level}"')
            lines.append(f'log.maxDays = {self.log_max_days}')

        # Dashboard
        if self.web_server_addr or self.web_server_port:
            lines.append("")
            if self.web_server_addr:
                lines.append(f'webServer.addr = "{self.web_server_addr}"')
            if self.web_server_port:
                lines.append(f'webServer.port = {self.web_server_port}')
            if self.web_server_user:
                lines.append(f'webServer.user = "{self.web_server_user}"')
            if self.web_server_password:
                lines.append(f'webServer.password = "{self.web_server_password}"')

        # 代理
        for proxy in self.proxies:
            lines.append("")
            lines.append(proxy.to_toml_block())

        if not self.proxies:
            lines.append("")
            lines.append("# 尚未配置代理。请在 [[proxies]] 下添加。")

        return "\n".join(lines) + "\n"


# === 默认模板 ===

def default_server_template() -> dict:
    return {
        "name": "my-server",
        "serverAddr": "127.0.0.1",
        "serverPort": 2005,
        "token": "your_token_here",
    }


def default_proxy_template(proxy_type: str) -> dict:
    base = {"name": f"my-{proxy_type}", "type": proxy_type,
            "local_ip": "127.0.0.1", "local_port": 8080}
    if proxy_type in ("tcp", "udp"):
        base["remote_port"] = 6000
    if proxy_type in ("http", "https"):
        base["custom_domains"] = ["example.com"]
    if proxy_type in ("stcp", "xtcp"):
        base["secret_key"] = "your_secret_key"
    return base
