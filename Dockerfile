# easy-frp-manager - frp 可视化管理面板
# 零外部依赖,仅使用 Python 标准库
# Docker 镜像:支持 amd64/arm64

FROM python:3.12-slim

LABEL org.opencontainers.image.title="easy-frp-manager"
LABEL org.opencontainers.image.description="frp 可视化管理面板 | 零依赖 | Docker/Binary 双模式"
LABEL org.opencontainers.image.source="https://github.com/kingc0000/easy-frp-manager"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

# 不内置 docker CLI —— docker CLI 由部署脚本挂载宿主机二进制进容器
# (镜像保持轻量;docker.sock + docker CLI 挂载由 setup-server.sh/deploy-docker.sh 负责)

# 拷贝代码
COPY app.py nginx_api.py auth.py db.py config_gen.py frp_ops.py http_server.py version.py ./
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
# 端口跟随 FRPM_PORT 环境变量,适配 -e FRPM_PORT=xxxx 部署(host 网络)与默认 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/auth/check' % os.environ.get('FRPM_PORT','8080'), timeout=3).read()" || exit 1

CMD ["python3", "app.py"]
