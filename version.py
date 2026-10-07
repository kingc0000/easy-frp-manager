#!/usr/bin/env python3
"""
frpm 版本管理模块。

版本号规则:语义化版本 MAJOR.MINOR.PATCH
- MAJOR: 大版本变更,不兼容旧版(如 v1.0 -> v2.0)
- MINOR: 功能新增,向后兼容(如 v1.0 -> v1.1)
- PATCH: Bug 修复,小改动(如 v1.1.0 -> v1.1.1)

用法:
    import version
    print(version.VERSION)  # "1.1.0"
    print(version.VERSION_FILE)  # 当前版本号
"""

# 当前版本号
VERSION = "1.2.1"

# 简短描述(用于 Docker Hub / GitHub 显示)
DESCRIPTION = "frp 可视化管理面板 | 零依赖 | Docker/Binary 双模式 | 中文友好"

# 发布信息
AUTHOR = "anonymous"
AUTHOR_URL = "https://github.com/anonymous/easy-frp-manager"
LICENSE = "MIT"
DOCKER_HUB = "mejeor/easy-frp-manager"

# 版本变更历史(最新在前)
CHANGELOG = """
v1.2.0 (2026-10-03)
- feat: 前端完全重做,卡片式设计(渐变背景/大圆角/emoji 图标/彩色统计卡),实例列表改卡片网格,脱离深色表格风格
- feat: 新增 nginx 一键配置模块(复用宿主已有 conf 模板 + 证书路径自动探测 + 写入自动 nginx -t 校验回滚)
- fix: 登出接口从 headers 取 token(原从 ctx 取导致登出无效)
- fix: Router 端口冲突/非数字 id 路由崩溃返回 500 的问题

v1.1.4 (2026-10-01)
- feat: 所有部署脚本统一 --network host 网络模式,frps/frpc 端口天然直通
- fix: setup-server.sh 改用 host 网络(原 bridge 漏映射 frps 端口,外部连不上)
- fix: install.sh 对齐 host 网络 + FRPM_PORT/DB/CONFIG 环境变量 + docker.sock 挂载
- docs: README 增加网络模式说明(新增端口无需改 docker run / 无需重启容器)

v1.1.3 (2026-10-01)
- fix: 镜像移除内置 docker CLI,改回部署时挂载(镜像瘦身 ~243MB→~148MB)
- fix: docker 模式版本探测兼容 /app/bin 与 /usr/local/bin 双路径
- fix: setup-server.sh 补 FRPM_DB/FRPM_CONFIG_DIR/FRPM_LOG_DIR 环境变量,容器重建数据不丢
- fix: Dockerfile healthcheck 端口跟随 FRPM_PORT,不再硬编码 8080

v1.1.2 (2026-10-01)
- fix: frpm 容器内置 docker CLI,修复面板显示 'Docker ✗ 不可用'
- fix: setup-server.sh 自动检测云厂商选镜像源(阿里云/腾讯云/华为云)
- fix: 去掉 'http2 on;'(老 nginx 不支持),nginx -t 失败明确报错
- fix: acme.sh Skipping 容错,证书已存在时不再挂脚本
- fix: Tencent_SecretId/Key 驼峰命名(全大写 acme.sh 读不到)
- feat: 自动配 Docker Hub 镜像加速(中科大已关停,换 DaoCloud/1Panel/网易)

v1.1.1 (2026-09-24)
- fix: 修复 acme.sh 跳过证书申请时脚本挂掉

v1.1.0 (2026-09-24)
- feat: 添加登录功能(默认 admin/admin123,支持修改密码)
- feat: 内置 frp v0.61.1 二进制(amd64+arm64),用户无需单独安装 frp
- feat: 支持 Docker Hub + GitHub Actions 自动构建多平台镜像
- fix: 修复凭证文件初始化 bug(salt 和 hash 不匹配)

v1.0.0 (2026-09-24)
- 初始发布:frp 可视化管理面板
- 零外部依赖(纯 Python stdlib)
- frps/frpc 可视化配置(表单填写,自动生成 TOML)
- 支持 Docker / Binary 双模式部署
- 6 种代理类型(tcp/udp/http/https/stcp/xtcp)
- 实例 CRUD + 启停/重启/日志(SSE 实时流)
"""


def get_version() -> str:
    """返回当前版本号。"""
    return VERSION


def get_full_version() -> str:
    """返回完整版本信息。"""
    return f"v{VERSION}"


def format_changelog() -> str:
    """返回格式化后的变更历史。"""
    return CHANGELOG


def bump_version(bump_type: str = "patch") -> str:
    """
    提升版本号(用于开发,不直接用于发布)。
    
    参数:
        bump_type: 'major' / 'minor' / 'patch'
    返回:
        新版本号
    """
    parts = VERSION.split(".")
    major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
    if bump_type == "major":
        major += 1
        minor = 0
        patch = 0
    elif bump_type == "minor":
        minor += 1
        patch = 0
    elif bump_type == "patch":
        patch += 1
    else:
        raise ValueError(f"未知的 bump 类型: {bump_type}")
    return f"{major}.{minor}.{patch}"
