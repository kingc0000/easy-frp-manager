# FRP Manager — frp 管理 WebUI

零外部依赖的 frp 可视化管理面板,纯 Python 标准库实现(无 Flask / FastAPI / docker SDK)。
提供 frps/frpc 实例的创建、配置、启停、日志、代理规则、域名穿透一键引导等全生命周期管理。

- **二进制内置**:frp v0.61.1 静态二进制打包进 Docker 镜像,无需联网下载
- **零依赖后端**:仅用 Python 标准库(http.server + sqlite3 + subprocess + tomllib)
- **二进制模式**:frps/frpc 进程跑在 frpm 容器内(挂载宿主 docker.sock + 二进制)
- **持久化**:SQLite 存实例元信息 + TOML 文件存 frp 配置,重启 frpm 后自动恢复

## 功能特性

| 类别 | 功能 |
|---|---|
| 🚀 实例管理 | 多实例管理 frps / frpc,创建/编辑/删除,实时启停/重启 |
| 🛡️ 自动恢复 | frpm 启动时检测并恢复进程丢失的实例(frps 先起 + socket 探测端口就绪 + frpc 再起) |
| 📡 代理规则 | 6 种代理类型:tcp / udp / http / https / stcp / xtcp |
| 🔄 代理 CRUD | 单条代理的增删改查、单条启停(改 enabled + 重新生成 TOML + 重启实例) |
| 🌐 域名穿透 | 配置域名的 http/https 代理自带「域名穿透指南」,一键生成 nginx 配置写到容器内 |
| 🔐 SSL 配置 | nginx 指南里开关 SSL,自动填写证书路径,实时预览 |
| 📋 实时日志 | SSE 流式查看实例日志 |
| 🔑 认证 | JWT Bearer Token,密码登录 + 默认 admin/admin123 首启改密 |
| 🎯 HTTPS 访问 | 支持 nginx 反代 + Let's Encrypt 多 SNI 证书 |
| 🖥️ UI | 深色主题 + Tailwind + Lucide 图标,中文标签 + 示例 placeholder |

## 架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        云主机                                    │
│                                                                 │
│  nginx :80/443 ─── 多 SNI ──┐                                    │
│   example.com               │                                    │
│   frpm.example.com          ▼                                    │
│                          ┌──────────────────────────────────┐  │
│                          │  frpm 容器 (frp-manager:local)    │  │
│                          │  host network :2003               │  │
│                          │  Flask-lite WebUI + API           │  │
│                          │  ───────────────────────────────  │  │
│                          │  SQLite /data/frpm/frpm.sqlite    │  │
│                          │  TOML   /data/frpm/configs/*.toml │  │
│                          │  nginx  /data/frpm/nginx/*.conf   │  │
│                          │  二进制 /app/bin/frps/frpc        │  │
│                          │         │                          │  │
│                          │         ├─ frps 进程 :2005         │  │
│                          │         └─ frpc 进程 ──────┐       │  │
│                          └────────────────────────────┼───────┘  │
│                                                       │          │
│  /data/frpm/ (挂载) ◄─── 容器内进程直接操作 ────┐     │          │
│  - configs/*.toml                              │     │          │
│  - logs/                                       │     │          │
│  - certs/ (acme.sh 证书)                       │     │          │
│  - nginx/ (生成的 nginx 配置)                  │     │          │
│  - frpm.sqlite                                 │     │          │
│                                                │     │          │
│  挂载宿主 docker.sock ────── frpm 管理容器 ─────┘     │          │
│                                                         ▼          │
│                                                         frps      │
│                              frpc 进程 ──── :2005 ──────► frps    │
│                              (login token + 代理规则)             │
└─────────────────────────────────────────────────────────────────┘
```

## 快速开始

### Docker 部署(推荐)

```bash
# 1. 构建镜像(本地 docker build,不需要联网拉依赖)
cd frp-manager
docker build -t frp-manager:local .

# 2. 部署(推荐用提供的脚本,会自动挂载 docker.sock + libonion.so + 二进制)
bash scripts/deploy-docker.sh

# 3. 访问
# 默认监听 0.0.0.0:2003
# 默认账号 admin / admin123(首登改密)
```

**部署脚本会做**:
- 创建 `/data/frpm/{configs,logs,nginx,certs}` 目录
- 启动 `frpm` 容器(host network,端口 2003)
- 挂载:宿主 `/var/run/docker.sock`、宿主 docker CLI、宿主 `libonion.so.4`、`/data/frpm` 数据目录
- 复制宿主 `/usr/local/bin/frps`、`frpc` 二进制到镜像内置 `bin/`(已存在则跳过)

### HTTPS 反代(可选,推荐)

frpm 默认明文 HTTP。生产环境建议 nginx 反代 + TLS 证书:

```nginx
# /etc/nginx/conf.d/frpm.conf
server {
    listen 80;
    server_name frpm.example.com;
    location / {
        proxy_pass http://127.0.0.1:2003;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}

server {
    listen 443 ssl;
    server_name frpm.example.com;
    ssl_certificate     /etc/ssl/frpm/frpm.example.com.crt;
    ssl_certificate_key /etc/ssl/frpm/frpm.example.com.key;
    location / {
        proxy_pass http://127.0.0.1:2003;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # WebSocket(SSE 实时日志)支持
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
    }
}
```

## 使用流程

### 1. 创建 frps 服务端

WebUI → 实例 → 新建实例 → 选 `frps` 类型:

| 字段 | 说明 | 示例 |
|---|---|---|
| 实例名 | 唯一标识 | `my-frps` |
| 部署模式 | binary(推荐) / docker | binary |
| bindAddr | 监听地址 | `0.0.0.0` |
| bindPort | 监听端口(建议 2000-2050 范围,云厂商安全组默认放行) | `2005` |
| authToken | 客户端鉴权 token | 强随机串 |
| webServer.port | Dashboard 端口(可选) | `2010` |

提交后:
- frpm 在 `/data/frpm/configs/<name>.toml` 写入配置
- 二进制模式启动 frps 进程,记录 pid
- 自动保存运行状态,frpm 重启后自动恢复

### 2. 创建 frpc 客户端

WebUI → 实例 → 新建实例 → 选 `frpc` 类型:

| 字段 | 说明 |
|---|---|
| 实例名 | 唯一标识,如 `my-frpc` |
| serverAddr | frps 地址(IP 或域名) |
| serverPort | frps 端口 |
| authToken | 与 frps 一致 |
| 代理列表 | 可批量添加多条代理规则 |

**6 种代理类型**:

| 类型 | 必填字段 | 用途 |
|---|---|---|
| `tcp` | localIP, localPort, remotePort | TCP 端口转发 |
| `udp` | localIP, localPort, remotePort | UDP 端口转发 |
| `http` | localIP, localPort, customDomains | HTTP 反向代理 |
| `https` | localIP, localPort, customDomains | HTTPS 反向代理 |
| `stcp` | localIP, localPort, secretKey | 加密 TCP 隧道 |
| `xtcp` | localIP, localPort, secretKey | 加密 TCP 隧道(UDP 穿透) |

### 3. 管理代理规则

每条 frpc 实例的代理规则独立管理:

- **新增**:WebUI 实例列表 → 展开 frpc 行 → 「+ 新增端口」
- **编辑**:代理行右侧「编辑」按钮
- **删除**:代理行右侧「删除」按钮
- **启停**:代理行右侧「停止」/「启动」按钮(改 enabled + 重新生成 TOML + 重启实例)

### 4. 域名穿透(带域名的 http/https 代理)

配置了 `custom_domains` 的 http/https 代理,代理行会显示「🌐 域名穿透指南」按钮。点击弹出模态框,4 个步骤:

1. **DNS 解析**:登录 DNS 服务商 → 加 A 记录 → 用 dig 验证
2. **TLS 证书**:acme.sh DNS-01 签发 + 安装到 `/etc/ssl/frpm/`
3. **nginx 配置**:SSL 开关 + 证书路径输入 + 实时预览 + 「保存到容器」按钮
4. **验证部署**:`docker cp` 出容器 → `nginx -t` → `nginx -s reload` → `curl` 验证

「💾 保存到容器」会把 nginx 配置写到 `/data/frpm/nginx/<代理名>.conf`,然后:
```bash
docker cp frpm:/data/frpm/nginx/<代理名>.conf /etc/nginx/conf.d/
nginx -t && nginx -s reload
```

### 5. 实时日志

实例详情页 → 「查看日志」按钮 → 弹出模态框,支持:
- 默认加载最近 200 行
- SSE 流式刷新(每 5 秒)
- 复制、关闭

## 自动恢复机制

frpm 启动时会检查数据库里的实例,如果进程丢失(如宿主机重启、容器重建),自动恢复:

```
[*] 自动恢复检查开始...
[*] 自动恢复实例: my-frps (frps, 进程丢失)
[*] 实例 my-frps 已启动, pid=7
[*] 等待 frps 端口 2005 就绪...
[*] frps 端口 2005 已就绪
[*] 自动恢复实例: my-frpc (frpc, 进程丢失)
[*] 实例 my-frpc 已启动, pid=14
[*] 自动恢复完成: 2 个实例已重启
```

**关键设计**:
- 按类型分两批:frps 先启动 → socket 探测 bindPort 就绪 → frpc 再起
- socket 探测代替固定 sleep(50ms 间隔,30s 超时)
- frpc 启动时如果 frps 未就绪会因 loginFailExit 立刻退出,所以必须等端口就绪

## API 接口

所有接口需 `Authorization: Bearer <token>`(登录接口除外)。

### 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/login` | 登录,返回 token |
| POST | `/api/auth/logout` | 登出 |
| POST | `/api/auth/change-password` | 修改密码 |

### 系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/dashboard` | 系统概况(CPU/内存/磁盘/版本) |

### 实例

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/instances` | 列出所有实例 |
| GET | `/api/instances/:id` | 获取实例详情 + 配置 |
| POST | `/api/instances` | 创建实例 |
| PUT | `/api/instances/:id` | 更新配置(可附 raw_config) |
| DELETE | `/api/instances/:id` | 删除实例(级联删除代理) |
| POST | `/api/instances/:id/start` | 启动 |
| POST | `/api/instances/:id/stop` | 停止 |
| POST | `/api/instances/:id/restart` | 重启 |
| GET | `/api/instances/:id/logs?lines=200` | 获取日志 |
| GET | `/api/instances/:id/logs?stream=1` | SSE 实时日志 |

### 代理

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/instances/:id/proxies` | 列出实例下所有代理 |
| POST | `/api/instances/:id/proxies` | 新建代理(重新生成 TOML + 重启实例) |
| PUT | `/api/instances/:id/proxies/:pid` | 更新代理 |
| DELETE | `/api/instances/:id/proxies/:pid` | 删除代理 |
| POST | `/api/instances/:id/proxies/:pid/toggle` | 启停代理(`{"enabled": true/false}`) |
| POST | `/api/instances/:id/proxies/:pid/nginx-conf` | 生成 nginx 配置(可保存到容器) |

### 配置 / 模板

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/proxy-types` | 支持的代理类型 |
| GET | `/api/template/proxy/:type` | 代理配置模板 |

## 数据持久化

```
/data/frpm/                              # 挂载到 frpm 容器
├── frpm.sqlite                          # SQLite 数据库(实例 + 代理元信息)
├── configs/
│   ├── my-frps.toml                  # frps 配置
│   └── my-frpc.toml                  # frpc 配置
├── logs/
│   ├── my-frps.log
│   └── my-frpc.log
├── nginx/
│   └── frpm-proxy.conf                   # 域名穿透生成的 nginx 配置
└── certs/                               # acme.sh 证书(可选)
```

**TOML 是产物,SQLite 是真相源**:
- 实例配置存 SQLite,同时生成 TOML 文件供 frp 读取
- 代理规则存 SQLite,TOML 是「启用的代理」的生成结果
- frpm 重启 → 从 SQLite 读 → 生成 TOML → 启动 frp 进程

## 项目结构

```
frp-manager/
├── app.py                # Web 应用入口(Flask-lite API + 路由)
├── http_server.py        # 轻量级 HTTP 服务器(基于 stdlib)
├── auth.py               # JWT 认证
├── db.py                 # SQLite 数据库层(instances + proxies 表)
├── config_gen.py         # frps/frpc TOML 配置生成器(dataclass)
├── frp_ops.py            # frp 检测/安装/进程管理
├── version.py            # 版本号
├── static/
│   ├── index.html        # 单页前端(Tailwind + Lucide)
│   └── vendor/tailwind.css
├── bin/                  # 内置 frp 二进制(镜像打包)
│   ├── frps              # x86_64
│   ├── frpc
│   ├── frps-arm64
│   └── frpc-arm64
├── scripts/
│   ├── deploy-docker.sh  # 部署脚本(构建 + 启动 frpm 容器)
│   ├── install.sh        # 一键安装脚本
│   └── generate-icon.py
├── docs/icon.svg
├── Dockerfile            # 镜像构建(打包内置二进制)
├── requirements.txt      # 占位(运行时零外部依赖)
└── README.md
```

## 部署模式

| 维度 | Docker 模式 | Binary 模式(推荐) |
|---|---|---|
| 隔离性 | ✅ 容器隔离 | ⚠️ frpm 容器内进程 |
| 启动开销 | 高(每个实例一个容器) | 低(直接 fork 进程) |
| 配置生成 | frpm 生成 TOML | frpm 生成 TOML |
| 进程管理 | docker CLI(挂载 socket) | subprocess + PID 文件 |
| 适合场景 | 多实例隔离要求高 | 资源紧张、简单部署 |

## 故障排查

### frpc 启动失败:`json: unknown field "loginFailExit"`
frpc 配置被 frps 二进制错误解析。frpm 已在 `start_binary_instance` 加 `instance_type` 参数,确保用对应二进制启动。

### frpc 启动后立刻退出
frps 未就绪 → frpc 登录失败 → `loginFailExit=true` 退出。frpm 自动恢复时已修复:socket 探测 frps bindPort 就绪后才启动 frpc。

### 容器启动失败
```bash
docker logs frpm
docker exec frpm /app/bin/frpc verify -c /data/frpm/configs/<name>.toml
```

### frpm 启动后没有自动恢复实例
- 检查 `/data/frpm/frpm.sqlite` 是否有实例记录
- 检查日志 `docker logs frpm | grep 自动恢复`
- 确认实例的 `running=true`(frpm 上次退出前正在运行,才会标记需恢复)

### HTTPS 访问 502
- 检查 nginx 配置:`nginx -t`
- 检查证书:`ls -la /etc/ssl/frpm/`
- 检查 frpm 是否在运行:`docker ps | grep frpm`
- 检查端口:`ss -tlnp | grep 2003`

## 安全建议

1. **限制访问**:WebUI 默认明文 HTTP,生产环境建议 nginx 反代 + TLS
2. **强 token**:frps authToken 用强随机串,避免未授权连接
3. **认证**:首登默认 admin/admin123,登录后立即修改密码
4. **安全组**:云厂商安全组只放行 80/443 + 2000-2050 范围,不要开 2000-2050 之外的端口
5. **二进制**:镜像内置 frp 二进制,无需运行时联网下载

## License

MIT
