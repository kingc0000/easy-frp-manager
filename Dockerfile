# FRP Manager - frp 管理 WebUI
# 零外部依赖,仅使用 Python 标准库
# 基于 python:3.12-slim(或 node:24-bookworm-slim 装 python3,国内可达)

# 方案 A:标准 python slim(推荐,网络正常时用)
FROM python:3.12-slim

WORKDIR /app

# 拷贝代码
COPY app.py db.py config_gen.py frp_ops.py http_server.py ./
COPY static/ static/

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


# =========================================
# 方案 B(注释):用 node:24-bookworm-slim + 清华源
# 适用于网络受限服务器,但需要构建机能访问清华源
# =========================================
# FROM node:24-bookwork-slim AS base
# RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/* && \
#     cat > /etc/apt/sources.list <<EOF
# deb http://mirrors.tuna.tsinghua.edu.cn/debian bookworm main contrib non-free
# deb http://mirrors.tuna.tsinghua.edu.cn/debian bookworm-updates main contrib non-free
# deb http://mirrors.tuna.tsinghua.edu.cn/debian-security bookworm-security main contrib non-free
# EOF
# RUN apt-get update && apt-get install -y --no-install-recommends python3 curl ca-certificates && rm -rf /var/lib/apt/lists/*
# WORKDIR /app
# COPY app.py db.py config_gen.py frp_ops.py http_server.py ./
# COPY static/ static/
# RUN mkdir -p /data/frpm-configs /var/lib/frpm/logs
# ENV FRPM_PORT=8080
# EXPOSE 8080
# CMD ["python3", "app.py"]
