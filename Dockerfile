# easy-frp-manager - frp 可视化管理面板
# 零外部依赖,仅使用 Python 标准库
# Docker 镜像:支持 amd64/arm64

FROM python:3.12-slim

LABEL org.opencontainers.image.title="easy-frp-manager"
LABEL org.opencontainers.image.description="frp 可视化管理面板 | 零依赖 | Docker/Binary 双模式"
LABEL org.opencontainers.image.source="https://github.com/kingc0000/easy-frp-manager"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

# 拷贝代码
COPY app.py db.py config_gen.py frp_ops.py http_server.py ./
COPY static/ static/
COPY scripts/ scripts/

# 数据目录
RUN mkdir -p /data/frpm-configs /var/lib/frpm/logs

# 默认配置
ENV FRPM_PORT=8080 \
    FRPM_DB=/data/frpm.sqlite \
    FRPM_CONFIG_DIR=/data/frpm-configs \
    FRPM_LOG_DIR=/var/lib/frpm/logs

EXPOSE 8080

# 健康检查
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/dashboard', timeout=3)" || exit 1

CMD ["python3", "app.py"]
