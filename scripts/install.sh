#!/usr/bin/env bash
# FRP Manager 一键安装脚本
# 用法: curl -sSL https://example.com/frpm-install.sh | bash
#       或: bash frpm-install.sh
#
# 支持两种部署方式:
#   - native: 直接跑 Python 脚本(无依赖,最稳)
#   - docker: 用 Docker 部署(需要 Docker 已安装)

set -e

# ===== 配置(可按需修改) =====
INSTALL_DIR="${INSTALL_DIR:-/opt/frp-manager}"
DATA_DIR="${DATA_DIR:-/data/frpm}"
PORT="${FRPM_PORT:-8080}"
MODE="${FRPM_MODE:-native}"  # native 或 docker

# ===== 颜色输出 =====
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ===== 显示 banner =====
show_banner() {
    echo -e "${BLUE}"
    echo "  __  __  __  ___     _    ____ _"
    echo " |  \\/  ||  |_ _ ___| |_  / ___| |"
    echo " | |\/| || __| |/ __| __| | |   | |"
    echo " | |  | || |_| (_| | |_| |___| | |"
    echo " |_|  |_| \__|_|\__,_|\__|\____|_|"
    echo "  _    ____ _  _    _     _  __"
    echo " | |_  / ___| || |_  (_) __| |/ /"
    echo " | __|| |   | || __| | |/ _\` | |  ${YELLOW}v1.0.0${NC}"
    echo " | |_| |___| || |_| |  | (_| | |__"
    echo "  \__|_____|_|\__|_|  |\__,_|_____|${NC}"
    echo -e "  ${YELLOW}FRP Manager - 0 外部依赖 · Docker/Binary 双模式${NC}"
    echo ""
}

# ===== 检查环境 =====
check_env() {
    info "检查系统环境..."
    if [ "$(id -u)" -ne 0 ]; then
        error "请使用 root 运行(或 sudo bash $0)"
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        error "Python3 未安装,请先安装(Debian/Ubuntu: apt install python3)"
    fi
    if ! python3 --version >/dev/null 2>&1; then
        error "Python3 不可用"
    fi
    ok "Python3 $(python3 --version 2>&1)"

    if [ "$MODE" = "docker" ]; then
        if ! command -v docker >/dev/null 2>&1; then
            error "Docker 未安装,请先安装 Docker(或设置 FRPM_MODE=native)"
        fi
        if ! docker info >/dev/null 2>&1; then
            error "Docker 未运行或无权限访问"
        fi
        ok "Docker 就绪"
    fi
}

# ===== 创建目录 =====
setup_dirs() {
    info "创建安装目录..."
    mkdir -p "$INSTALL_DIR" "$DATA_DIR/configs" "$DATA_DIR/logs"
    ok "目录就绪: $INSTALL_DIR, $DATA_DIR"
}

# ===== 拷贝代码 =====
install_code() {
    info "安装代码到 $INSTALL_DIR..."
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

    if [ ! -f "$PROJECT_DIR/Dockerfile" ]; then
        # 假设源码就在当前目录(如果脚本被直接下载)
        PROJECT_DIR="$PWD"
    fi
    if [ ! -f "$PROJECT_DIR/app.py" ]; then
        error "找不到 app.py,请确保脚本在项目根目录运行"
    fi

    cp -r "$PROJECT_DIR/app.py" "$PROJECT_DIR/db.py" "$PROJECT_DIR/config_gen.py" \
          "$PROJECT_DIR/frp_ops.py" "$PROJECT_DIR/http_server.py" \
          "$PROJECT_DIR/static" "$INSTALL_DIR/"
    ok "代码已拷贝"
}

# ===== 创建 systemd 服务(native 模式) =====
install_native_service() {
    info "创建 native systemd 服务..."
    cat > /etc/systemd/system/frp-manager.service <<EOF
[Unit]
Description=FRP Manager - Web 管理工具 (native)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/app.py
Restart=always
RestartSec=5
User=root
Environment=FRPM_PORT=$PORT
Environment=FRPM_DB=$DATA_DIR/frpm.sqlite
Environment=FRPM_CONFIG_DIR=$DATA_DIR/configs
Environment=FRPM_LOG_DIR=$DATA_DIR/logs

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now frp-manager
    ok "systemd 服务 frp-manager 已启动"
}

# ===== 创建 docker 服务 =====
install_docker_service() {
    info "构建并启动 Docker 容器..."
    DOCKER_IMAGE="frpm:latest"
    if [ ! -f "$INSTALL_DIR/Dockerfile" ]; then
        cp "$INSTALL_DIR/../Dockerfile" "$INSTALL_DIR/" 2>/dev/null || \
        cp "$(dirname "${BASH_SOURCE[0]}")/../Dockerfile" "$INSTALL_DIR/" 2>/dev/null || true
    fi
    if [ -f "$INSTALL_DIR/Dockerfile" ]; then
        info "构建镜像..."
        docker build -t "$DOCKER_IMAGE" "$INSTALL_DIR"
    elif docker images -q "$DOCKER_IMAGE" | grep -q .; then
        warn "镜像已存在,跳过构建"
    else
        error "找不到 Dockerfile,无法构建镜像"
    fi

    docker rm -f frpm 2>/dev/null || true
    docker run -d \
        --name frpm \
        --restart unless-stopped \
        -p "$PORT:8080" \
        -v "$DATA_DIR:/data" \
        "$DOCKER_IMAGE"
    ok "Docker 容器 frpm 已启动"
}

# ===== 验证服务 =====
verify_service() {
    info "验证服务..."
    sleep 2
    for i in 1 2 3 4 5; do
        if curl -s --max-time 3 "http://127.0.0.1:$PORT/api/dashboard" | grep -q '"system"'; then
            ok "服务响应正常: http://127.0.0.1:$PORT"
            return
        fi
        sleep 2
    done
    warn "服务可能尚未完全启动,请手动检查"
    [ "$MODE" = "docker" ] && docker logs frpm --tail 20
}

# ===== 卸载 =====
uninstall() {
    info "卸载 FRP Manager..."
    if [ "$MODE" = "docker" ]; then
        docker rm -f frpm 2>/dev/null || true
    else
        systemctl stop frp-manager 2>/dev/null || true
        systemctl disable frp-manager 2>/dev/null || true
        rm -f /etc/systemd/system/frp-manager.service
        systemctl daemon-reload
    fi
    rm -rf "$INSTALL_DIR"
    info "如要清除数据,执行: rm -rf $DATA_DIR"
    ok "卸载完成"
}

# ===== 主流程 =====
main() {
    show_banner
    if [ "${1:-}" = "--uninstall" ]; then
        uninstall
        exit 0
    fi
    check_env
    setup_dirs
    install_code
    if [ "$MODE" = "docker" ]; then
        install_docker_service
    else
        install_native_service
    fi
    verify_service
    echo ""
    ok "✅ FRP Manager 安装完成!"
    echo ""
    echo -e "${GREEN}访问地址:${NC} http://$(hostname -I | awk '{print $1}'):${PORT}"
    echo -e "${GREEN}API 地址:${NC} http://$(hostname -I | awk '{print $1}'):${PORT}/api/dashboard"
    echo ""
    echo -e "${YELLOW}常用操作:${NC}"
    echo "  查看日志: journalctl -u frp-manager -f  (native)"
    echo "            docker logs -f frpm              (docker)"
    echo "  重启服务: systemctl restart frp-manager    (native)"
    echo "            docker restart frpm              (docker)"
    echo "  卸载:     bash $0 --uninstall"
}

main "$@"
