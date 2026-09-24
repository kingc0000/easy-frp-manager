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
VERSION = "1.1.0"

# 简短描述(用于 Docker Hub / GitHub 显示)
DESCRIPTION = "frp 可视化管理面板 | 零依赖 | Docker/Binary 双模式 | 中文友好"

# 发布信息
AUTHOR = "kingc0000"
AUTHOR_URL = "https://github.com/kingc0000/easy-frp-manager"
LICENSE = "MIT"
DOCKER_HUB = "mejeor/easy-frp-manager"

# 版本变更历史(最新在前)
CHANGELOG = """
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
