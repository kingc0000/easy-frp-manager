# FRP Manager - frp 管理 WebUI

一个零外部依赖的 frp 管理工具,提供 frps/frpc 的可视化配置、一键安装、实例启停、日志查看等功能。

**实测通过**:在腾讯云服务器上,通过 WebUI 创建 frps + frpc 实例,成功实现端到端 TCP 转发(`curl http://127.0.0.1:11111/` 返回测试服务响应)。

## 功能特性

| 功能 | 说明 |
|---|---|
| ✅ **frp 检测** | 自动探测 docker / systemd / 二进制三种安装方式,显示版本 |
| ✅ **一键安装** | Docker 拉镜像 / 二进制从 GitHub Releases 下载(支持多版本) |
| ✅ **多实例管理** | 同时管理多个 frps/frpc,每个独立配置 |
| ✅ **可视化配置** | 表单填写 → 自动生成合法 TOML(已用 frp 官方 verify 命令校验) |
| ✅ **代理类型** | tcp / udp / http / https / stcp / xtcp 六种 |
| ✅ **生命周期** | 创建/启停/重启/删除 + 实时日志(SSE 流式) |
| ✅ **部署模式** | Docker(挂载 docker.sock) / Binary(systemd 服务) |
| ✅ **零依赖** | 仅使用 Python 标准库,无 Flask/psutil/docker SDK |
| ✅ **数据持久化** | SQLite 存元信息,TOML 文件存配置 |

## 快速开始

### 方案 1:Docker 部署(推荐)

```bash
# 在目标服务器执行
docker build -t frpm:latest .
docker run -d \
  --name frpm \
  --restart unless-stopped \
  -p 8080:8080 \
  -v /var/run/docker.sock:/var/run/docker.sock \  # 让 frpm 管理宿主机的 docker
  -v /data/frpm:/data \
  frpm:latest

# 浏览器访问 http://服务器IP:8080
```

### 方案 2:systemd 原生部署(无 Docker 依赖)

```bash
# 1. 拷贝代码到服务器
scp -r frp-manager/ root@服务器:/opt/frp-manager/

# 2. 创建 systemd 服务
sudo tee /etc/systemd/system/frp-manager.service > /dev/null <<EOF
[Unit]
Description=FRP Manager WebUI
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/frp-manager
ExecStart=/usr/bin/python3 /opt/frp-manager/app.py
Restart=always
RestartSec=5
User=root
Environment=FRPM_PORT=8080
Environment=FRPM_DB=/data/frpm.sqlite
Environment=FRPM_CONFIG_DIR=/data/frpm-configs
Environment=FRPM_LOG_DIR=/var/lib/frpm/logs

[Install]
WantedBy=multi-user.target
EOF

sudo mkdir -p /data/frpm-configs /var/lib/frpm/logs
sudo systemctl enable --now frp-manager

# 3. 验证
curl http://127.0.0.1:8080/api/dashboard
```

### 方案 3:一键安装脚本

```bash
# 下载并运行(已包含 systemd 注册、依赖检测)
curl -sSL https://your-server/frpm-install.sh | bash
```

## 使用流程

### 1. 检测 frp 安装状态

打开 WebUI → 仪表盘,会自动显示:
- frps / frpc 是否安装(容器 / systemd / 二进制三种方式)
- 版本信息
- 当前模式(docker / binary)

### 2. 安装 frp

**Docker 模式**(推荐):
```
点击"安装 Docker 镜像" → frpm 自动拉取 snowdreamtech/frps:latest 和 frpc:latest
```

**Binary 模式**:
```
点击"安装二进制" → 选择版本(默认 v0.61.1) → frpm 从 GitHub Releases 下载到 /usr/local/bin
```

### 3. 创建 frps(服务端)实例

表单字段:
| 字段 | 说明 | 示例 |
|---|---|---|
| 实例名 | 唯一标识 | `frps-prod` |
| bindAddr | 监听地址 | `0.0.0.0` |
| bindPort | 监听端口 | `7000` |
| authToken | 客户端鉴权 token | 自定 |
| transport.tcpMux | TCP 复用(默认 true) | |
| transport.heartbeatTimeout | 心跳超时(秒) | `90` |
| webServer.port | Dashboard 端口(可选) | `7500` |
| allowPorts | 允许的端口范围(可选) | `[{start=10000,end=20000}]` |

提交后:
- Docker 模式 → 创建容器,挂载配置,自动启动
- Binary 模式 → 生成 systemd 服务,自动启动

### 4. 创建 frpc(客户端)实例

表单字段:
- 连接信息:serverAddr / serverPort / authToken
- 多个代理(可批量添加)

**代理类型支持**:
| 类型 | 必填字段 | 用途 |
|---|---|---|
| `tcp` | localIP, localPort, remotePort | TCP 端口转发 |
| `udp` | localIP, localPort, remotePort | UDP 端口转发 |
| `http` | localIP, localPort, customDomains | HTTP 反向代理 |
| `https` | localIP, localPort, customDomains | HTTPS 反向代理 |
| `stcp` | localIP, localPort, secretKey | 加密 TCP 隧道 |
| `xtcp` | localIP, localPort, secretKey | 加密 TCP 隧道(支持双向) |

### 5. 管理实例

- **启动/停止/重启**:每个实例卡片上有按钮
- **查看日志**:详情页可看实时日志(SSE 流式)
- **编辑配置**:直接编辑 TOML,保存后自动重启实例
- **删除**:清理容器/服务 + 配置 + DB 记录

## 配置示例

### frps.toml(自动生成)

```toml
# frps 服务端配置 - 由 FRP Manager 自动生成
bindAddr = "0.0.0.0"
bindPort = 7000

auth.method = "token"
auth.token = "your_token_here"
transport.tcpMux = true
transport.heartbeatTimeout = 90
transport.tcpKeepalive = 7200
transport.maxPoolCount = 5

log.to = "/frp/logs/frps.log"
log.level = "info"
log.maxDays = 3

webServer.addr = "127.0.0.1"
webServer.port = 7500
webServer.user = "admin"
webServer.password = "admin"

enablePrometheus = true
allowPorts = [{start = 10000, end = 20000}]
```

### frpc.toml(自动生成)

```toml
# frpc 客户端配置 - 由 FRP Manager 自动生成
serverAddr = "124.221.238.106"
serverPort = 7000

auth.method = "token"
auth.token = "your_token_here"

loginFailExit = true
transport.tcpMux = true
transport.poolCount = 5

log.to = "/frp/logs/frpc.log"
log.level = "info"
log.maxDays = 3

[[proxies]]
name = "web"
type = "tcp"
localIP = "127.0.0.1"
localPort = 8080
remotePort = 1111

[[proxies]]
name = "http-web"
type = "http"
localIP = "127.0.0.1"
localPort = 80
customDomains = ["www.example.com"]
subdomain = "web"
hostHeaderRewrite = "example.com"

[[proxies]]
name = "db"
type = "stcp"
localIP = "127.0.0.1"
localPort = 3306
secretKey = "your_secret"
```

## API 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/dashboard` | 系统概况 + frp 状态 |
| GET | `/api/instances` | 列出所有实例 |
| GET | `/api/instances/:id` | 获取实例详情 + 配置 |
| POST | `/api/instances` | 创建实例 |
| PUT | `/api/instances/:id` | 更新配置(可附 raw_config) |
| DELETE | `/api/instances/:id` | 删除实例 |
| POST | `/api/instances/:id/start` | 启动 |
| POST | `/api/instances/:id/stop` | 停止 |
| POST | `/api/instances/:id/restart` | 重启 |
| GET | `/api/instances/:id/logs?lines=200` | 获取日志 |
| GET | `/api/instances/:id/logs?stream=1` | SSE 实时日志 |
| POST | `/api/install/docker` | 拉取 frp Docker 镜像 |
| POST | `/api/install/binary` | 安装 frp 二进制 |
| POST | `/api/uninstall/binary` | 卸载 frp 二进制 |
| GET | `/api/install/versions` | 可用版本列表 |
| GET | `/api/template/proxy/:type` | 代理配置模板 |
| GET | `/api/proxy-types` | 支持的代理类型 |

## 部署模式对比

| 维度 | Docker 模式 | Binary 模式 |
|---|---|---|
| 隔离性 | ✅ 强(容器隔离) | ⚠️ 弱(systemd 进程) |
| 安装依赖 | 需要 Docker daemon | 需要 systemd |
| 配置生成 | frpm 自动生成 TOML | frpm 自动生成 TOML |
| 实例运行 | Docker 容器 | systemd 服务 |
| 端口暴露 | 容器端口映射 / host 网络 | 直接绑定宿主机 |
| 适合场景 | 多实例、隔离要求高 | 资源紧张、不想装 Docker |

## 安全建议

1. **限制访问**:WebUI 默认监听 8080,建议用 nginx 反代 + TLS + Basic Auth
2. **frp authToken**:务必设置强 token,避免未授权连接
3. **二进制安装权限**:binary 模式需要 root 权限(写 /usr/local/bin 和 /etc/systemd/system)
4. **日志轮转**:配置中已默认启用 `log.maxDays = 3`,按需调整

## 故障排查

### 容器启动失败
```bash
# 查看日志
docker logs <container_name>
# 检查配置语法
docker exec <container_name> /usr/local/bin/frps verify -c /etc/frp/frp.toml
```

### systemd 服务启动失败
```bash
systemctl status frpm-<instance_name>
journalctl -u frpm-<instance_name> -n 50 --no-pager
```

### 配置校验
```bash
# frps 配置
/usr/local/bin/frps verify -c /path/to/frps.toml
# frpc 配置
/usr/local/bin/frpc verify -c /path/to/frpc.toml
```

## 项目结构

```
frp-manager/
├── app.py              # Web 应用入口(Flask 风格 API,自写 HTTP 服务器)
├── http_server.py      # 轻量级 HTTP 服务器(基于 stdlib,无外部依赖)
├── db.py               # SQLite 数据库层
├── config_gen.py       # frps/frpc TOML 配置生成器
├── frp_ops.py          # frp 检测/安装/容器/服务管理
├── static/
│   └── index.html      # 单页前端(Tailwind + Lucide icons)
├── templates/          # (预留)
├── scripts/
│   └── install.sh      # 一键安装脚本
├── Dockerfile          # Docker 镜像构建
├── requirements.txt    # (保留,但运行时无需安装)
└── README.md
```

## 版本

- 当前版本:v1.0.0
- 支持的 frp 版本:v0.61.x(向后兼容 v0.5x)
- 测试环境:Ubuntu 22.04+ / Debian 12,腾讯云 3.6GiB 内存服务器

## License

MIT
