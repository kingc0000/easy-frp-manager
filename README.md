# frp 鉴权网关（auth-gate）

基于 easy-frp-manager 的独立鉴权网关进程。在 **frpc 侧**为每个代理规则加一层应用级鉴权——外部请求先到鉴权网关，校验通过后才转发到真实后端。

## 功能一览

- **6 种鉴权模式**：Basic（用户名+密码）、Bearer、API Key、URL Token、Any、None
- **上游凭证注入**：后端自带鉴权时（如 DSH、NAS），网关转发时自动携带后端的凭证（URL 参数 / Bearer 头 / Basic / 自定义头）
- **Cookie 会话透传**：后端有会话 cookie（如 DSH 的 `dsh-auth-*`）时，换到 cookie 后无需再带 token
- **WebSocket 透传**：检测 `Upgrade: websocket` 时改为原始 TCP 隧道（透传升级头 + 101 响应 + 双向字节流），支持 DSH 的 `/api/remote.mux` 等 WebSocket 端点
- **例外路径**：指定路径免鉴权（精确/前缀/通配）
- **Token 过期**：全局或按 token 设置过期时间
- **Token 剥离**：转发前去掉 URL 里的 token 参数（防泄漏到后端）
- **Token 生成**：一键生成密码学安全随机 token
- **预演鉴权**：保存前测试 token 是否命中
- **状态面板**：进程状态 + 每代理的成功/失败/通过率统计

---

## 架构与链路

```
外部访问者 → frps(公网端口) → frpc → 127.0.0.1:gate_port(auth-gate 容器内回环)
                                    ↓ 校验通过
                              backend_host:backend_port(真实后端，可跨宿主/远端)
```

**关键设计：两个地址语义分开**

| 地址 | 含义 | 取值 |
|------|------|------|
| frpc 的 `localIP` | frpc→auth-gate 的连接目标 | **固定 `127.0.0.1`**（auth-gate 与 frpc 同容器，回环可达） |
| auth-gate 的 `backend_host` | auth-gate→真实后端的转发目标 | 代理的 `local_ip`（如 `172.18.0.1` 指向宿主机 socat） |

> 开启鉴权后，frpc 的 toml 中 `localIP` 强制为 `127.0.0.1`、`localPort` 为 gate_port（16384 起）；auth-gate 规则的 `backend_host`/`backend_port` 用代理的 `local_ip`/`local_port`。不要把两者混用。

**gate_port**：16384–19999 池，容器内部回环监听，**不映射到宿主机**，无需防火墙放行（bridge 模式下天然安全）。

---

## 两种典型场景

### 场景 1：本机服务不带鉴权（最常见）

- **①鉴权方式**：URL Token（推荐）→ 填网关 token → 勾选"剥离 token"
- **②后端服务需要凭证吗**：**不需要**（默认，不展开）

访问：`http://公网IP:remotePort/?token=<网关token>`

无 token 或错 token → 401。

### 场景 2：本机服务自带鉴权（如 DSH 需 `?token=xxx`）

- **①鉴权方式**：URL Token → 填**网关 token**（外部访问者用）
- **②后端服务需要凭证吗**：选 **"需要 — 网址参数(?token=xxx)"** → 填**后端 token**（DSH 的 launch token）

访问：`http://公网IP:remotePort/?token=<网关token>`

流程：外部 token 校验 → 网关注入后端 token → 后端认证。后端真实 token 只存在网关配置里，外部不可见。

---

## 上游凭证注入（auth_upstream_*）

开启"②后端服务需要凭证吗"后，网关校验通过后转发时**自动携带后端凭证**。支持 4 种注入方式：

| 方式 | 效果 | 适用 |
|------|------|------|
| `query` | `?param=<token>` 拼到 URL | 后端用 URL 参数鉴权（DSH） |
| `bearer` | `Authorization: Bearer <token>` | 后端用 Bearer 头 |
| `basic` | `Authorization: Basic base64(user:pass)` | 后端用 Basic |
| `header` | `<header>: <token>`（自定义头名） | 后端用自定义头 |
| `none` | 不注入（后端无鉴权） | 场景 1 |

**字段**：`auth_upstream_mode`、`auth_upstream_token`、`auth_upstream_header`、`auth_upstream_param`、`auth_upstream_user`、`auth_upstream_pass`

---

## Cookie 透传放行（auth_upstream_cookie）

**解决双重认证冲突**：后端（如 DSH）的认证是"URL token 换会话 cookie"模式——首次 `?token=xxx` → 303 换 cookie → 后续请求带 cookie。但 auth-gate 的 URL Token 模式要求**每个请求**都带 token，导致浏览器换到 cookie 后跳转 `./`（无 token）被拦。

**`auth_upstream_cookie=1`** 时：请求携带 `Cookie` 头 → 网关直接放行（不校验 token、**不注入上游凭证**），后端自行校验 cookie 真伪（伪造的会被后端拒）。

**完整浏览器流程**（DSH 场景）：
1. 浏览器访问 `?token=网关token` → 网关校验 + 注入 launch token → DSH 303 + set-cookie
2. 浏览器带 cookie 访问 → 网关 cookie 放行（不注入）→ DSH 200

> ⚠️ 安全提示：cookie 放行仅作便利，cookie 的有效性仍由后端严格校验。仅在后端确有会话 cookie 认证时启用。

> 🔑 排障：若浏览器报 **401 "dsh web authentication required"** 但服务端 curl 测试 200/303，通常是**浏览器存了过期的旧 DSH cookie**，触发了 cookie 放行但后端（DSH）校验旧 cookie 失败。**清除该站点的 `dsh-auth-*` cookie（F12 → Application → Cookies，或换无痕窗口）后重试**即可。网关的 cookie 放行分支会**剥离 URL 里的网关 token 参数**，避免把外部 token 泄漏给后端（否则后端把网关 token 误当自己的 token 校验 → 401）。

> 💡 子资源 404：`plugins/??...client.js` 等返回 404 是插件未安装（如 `dsh-api-gateway`、`dsh-client-ui-open-in-app`），属正常控制台噪音，不影响主功能。

> 🔧 **combo URL 透传**：DSH 等后端的 combo loader 用 `plugins/??a.js,b.js&rev=xxx` 批量加载模块。网关转发时必须**原样保留**该 URL——若用 `parse_qs` 重组 query，会把 `?a.js,b.js` 误当键解析，重组后多出 `=`，导致 combo URL 破坏 → 后端 404。已在 `_forward_request` 修复：检测 `?` 后紧跟 `?`（combo 格式）时原样透传，剥离/注入都用字符串操作。**排查子资源 404 时，先确认是网关路径破坏还是插件真未安装。**

---

## 面板操作

代理表单的「🔒 鉴权网关」区块，分两步：

- **①鉴权方式**：外部访问者怎么通过（Basic / Bearer / API Key / URL Token / Any）
- **②后端服务需要凭证吗**：默认"不需要"（不展开）；选"需要"时展开凭证字段

- 「🎲 生成 Token」按钮：生成 32 位密码学安全随机 token，自动追加到 Token 列表
- 「🧪 预演鉴权」按钮：保存前测试 token 是否命中
- 「🔒 鉴权状态」面板：进程状态 + 每代理的成功/失败/通过率 + 可复制的 token 列表

---

## API 端点

| 端点 | 说明 |
|------|------|
| `POST /api/auth-gate/gen-token` | 生成强随机 token `{length?, prefix?}` → `{token}` |
| `GET /api/auth-gate/status` | 进程状态 + 每代理的 gate_port/backend/mode/tokens |
| `POST /api/auth-gate/test-token` | token 预演 `{proxy_id, token?...}` → `{ok, reason, matched}` |
| `GET /api/auth-gate/stats` | 每 gate_port 的 ok/fail/reasons 统计 |

代理创建/更新时透传 `auth_enabled`、`auth_mode`、`auth_tokens`、`auth_upstream_*` 等字段，自动分配 gate_port、哈希 Basic 密码（PBKDF2）、重新生成规则并重启实例。

---

## 部署要点

### bridge 模式（推荐，安全隔离保留）

- frpc 访问宿主机服务：`local_ip=172.18.0.1`（docker 网桥网关）
- **DSH 直连监听 `172.18.0.1`**（推荐，无需 socat）：DSH `--host 172.18.0.1`，nginx 2000 `proxy_pass http://172.18.0.1:5068`
- 若宿主机服务只监听 `127.0.0.1`（如 DSH），需加 **socat 转发**：`172.18.0.1:port → 127.0.0.1:port`
- **iptables 放行**：宿主 INPUT 链 default DROP 会拦非 2000-2050 端口，需放行 `172.18.0.0/16 → 172.18.0.1:port`

iptables 规则服务（`/etc/systemd/system/dsh-iptables.service`，重启保留）：

```ini
[Unit]
Description=DSH iptables rules for docker gateway access
After=network.target docker.service

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'iptables -C INPUT -s 172.18.0.0/16 -d 172.18.0.1 -p tcp --dport 5068 -j ACCEPT 2>/dev/null || iptables -I INPUT 3 -s 172.18.0.0/16 -d 172.18.0.1 -p tcp --dport 5068 -j ACCEPT'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

> 🔑 **Host 透传（关键）**：auth-gate 转发时必须**保留浏览器原始 Host 头**。DSH 等后端做 **CSRF/Origin 校验**——若网关用 `backend_host` 设 Host（如 `172.18.0.1:5068`），浏览器发的 `Origin: http://公网IP:2010` 与 Host 不匹配 → **403**；且 cookie 的 `authority` 也错误 → **401**。`_forward_request` 已修复：从原始请求取 Host 透传（`http.client` 检测到已有 Host 不会重复添加）。

> 🔌 **WebSocket 透传**：HTTP 层 `http.client` 不能处理 WebSocket 升级（Upgrade 头会被过滤、101 后是双向字节流而非 HTTP）。`_forward_and_reply` 检测 `Connection: upgrade` + `Upgrade: websocket` 时，改用 `_proxy_websocket`：重建原始请求字节发给后端 → 原样转 101 → 双向 pump socket（5 分钟空闲自动断开）。支持 DSH 的 `/api/remote.mux`、前端实时通道等。**排查 WebSocket 连接失败时，先确认网关未过滤 Upgrade 头。**

### host 模式（简化但隔离降低）

- frpc 访问宿主机服务：`local_ip=127.0.0.1` 直达
- **注意**：容器端口直接绑宿主（auth-gate 端口池 16384-19999 暴露到宿主接口）；8080 面板需额外防火墙约束

---

## 已知限制

1. **DSH launch token 是进程级动态值**——DSH web 重启后 token 变化，需重新填入代理的"后端转发凭证"（或建自动同步机制）。当前可通过读 `/root/.deepseek-harness/logs/dsh-web.log` 里的 `dsh web: http://.../?token=<launch>` 拿到最新 token。
2. **reload(SIGHUP) 不保证重读规则**——规则变更后建议 `stop + start` auth-gate 确保生效（日志确认 `backend=` 是否正确）。
3. **容器重建后 frpc 进程丢失自动恢复**——已由 `_auto_recover_instances` 处理（先 frps 后 frpc）；auth-gate 需手动或经面板操作启动（当前设计为按需）。
4. **PID 复用误判**：`get_binary_status` 已修复——校验 pid 的 cmdline 确实匹配实例配置（防容器重启后旧 pid 被新进程占用误判"运行中"）。
