# easy-frp-manager - frp 可视化管理面板
# 零外部依赖,仅使用 Python 标准库
# Docker 镜像:支持 amd64/arm64

FROM python:3.12-slim

LABEL org.opencontainers.image.title="easy-frp-manager"
LABEL org.opencontainers.image.description="frp 可视化管理面板 | 零依赖 | Docker/Binary 双模式"
LABEL org.opencontainers.image.source="https://github.com/anonymous/easy-frp-manager"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

# 安装 docker CLI(从 Docker 官方静态二进制,只取 docker CLI 不要 daemon/containerd)
# 用 TARGETARCH(buildx 自动注入)做架构映射,不用 dpkg --print-architecture
# 腾讯云镜像源(国内快) fallback 官方源
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl tar \
    && case "$TARGETARCH" in \
        amd64) DOCKER_ARCH=x86_64 ;; \
        arm64) DOCKER_ARCH=aarch64 ;; \
        *) DOCKER_ARCH="$TARGETARCH" ;; \
    esac \
    && (curl -fsSL --max-time 120 "https://mirrors.cloud.tencent.com/docker-ce/linux/static/stable/${DOCKER_ARCH}/docker-29.8.2.tgz" -o /tmp/docker.tgz \
        || curl -fsSL --max-time 180 "https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-29.8.2.tgz" -o /tmp/docker.tgz) \
    && tar xzf /tmp/docker.tgz -C /tmp/ docker/docker \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && chmod +x /usr/local/bin/docker \
    && rm -rf /tmp/docker /tmp/docker.tgz \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 拷贝代码
COPY app.py auth.py db.py config_gen.py frp_ops.py http_server.py version.py ./
COPY static/ static/
COPY scripts/ scripts/
# frp 内置二进制(amd64 + arm64)
# 打包进镜像让 frpm 自包含:可在容器内直接运行 frp(docker run frp-manager:local /app/bin/frps -v)
COPY bin/ bin/

# 数据目录
RUN mkdir -p /data/frpm-configs /var/lib/frpm/logs

# 默认配置
ENV FRPM_PORT=8080 \
    FRPM_DB=/data/frpm.sqlite \
    FRPM_CONFIG_DIR=/data/frpm-configs \
    FRPM_LOG_DIR=/var/lib/frpm/logs

EXPOSE 8080

# 健康检查
# 用免认证的 /api/auth/check 探测(返回 200),不能用需要登录的 /api/dashboard(会 401 误判 unhealthy)
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/auth/check', timeout=3).read()" || exit 1

CMD ["python3", "app.py"]
