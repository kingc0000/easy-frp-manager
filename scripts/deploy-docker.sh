#!/usr/bin/env bash
# frpm Docker 部署脚本
# 用法: bash deploy-docker.sh
#
# frpm 用 Docker 跑,通过挂载 docker.sock 管理宿主机的 frp 实例(容器)。
# 关键点:
#   - --network host: frpm 直接监听宿主端口(默认 2003),并能让 frpc 继承宿主 DNS
#   - 挂载 /var/run/docker.sock: frpm 容器通过 docker.sock 管理 frp 容器
#   - 挂载宿主 docker CLI + libonion.so: frp_ops 用 docker 子进程命令管理实例
#   - 挂载 /data/frpm: 持久化实例数据库、frp 配置、证书、日志

set -e

IMAGE="${FRPM_IMAGE:-frp-manager:local}"
CONTAINER="frpm"
FRPM_PORT="${FRPM_PORT:-2003}"

echo "==> 停止并删除旧容器(若存在)"
docker rm -f "$CONTAINER" 2>/dev/null || true

echo "==> 启动 frpm 容器"
docker run -d \
  --name "$CONTAINER" \
  --network host \
  -v /data/frpm:/data/frpm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /usr/bin/docker:/usr/bin/docker:ro \
  -v /usr/lib/x86_64-linux-gnu/libonion.so:/lib/x86_64-linux-gnu/libonion.so:ro \
  -e FRPM_PORT="$FRPM_PORT" \
  -e FRPM_DB=/data/frpm/frpm.sqlite \
  -e FRPM_CONFIG_DIR=/data/frpm/configs \
  -e FRPM_LOG_DIR=/data/frpm/logs \
  --restart unless-stopped \
  "$IMAGE"

echo "==> 等待启动..."
sleep 3

echo "==> 健康检查"
docker inspect --format '健康状态: {{.State.Health.Status}}' "$CONTAINER"

echo "==> 验证容器内 docker CLI 可用(管理 frp 实例用)"
docker exec "$CONTAINER" docker version --format 'docker CLI: {{.Client.Version}}' 2>&1 || \
  echo "  [警告] 容器内 docker CLI 不可用,frpm 将无法创建/管理 frp 实例"

echo ""
echo "部署完成: http://<本机IP>:$FRPM_PORT"
echo "登录: admin / admin123"
echo "查看日志: docker logs -f $CONTAINER"
