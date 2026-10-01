#!/usr/bin/env bash
# frpm-setup.sh — 在新服务器上部署 frpm + frps (泛域名 + 自动续期)
# 用法: sudo bash setup-server.sh
# 幂等: 重复运行不会出错，每步先检查再做

set -euo pipefail

# ============ 颜色 ============
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()   { err "$*"; exit 1; }

# 隐藏输入,显示 * 号
read_hidden() {
    local prompt="$1" varname="$2" show_last="${3:-4}"
    local old_stty=""
    local val=""
    # stty 在管道输入时会失败,忽略即可
    old_stty=$(stty -g 2>/dev/null) || { stty -echo 2>/dev/null; }
    stty -echo 2>/dev/null
    trap 'stty "$old_stty" 2>/dev/null; echo ""; return 1' INT

    printf "%s" "$prompt"
    local key
    while IFS= read -r -s -n 1 key; do
        if [ -z "$key" ] || [ "$key" = "" ]; then
            # 回车
            echo ""
            break
        elif [ "$key" = $'\177' ] || [ "$key" = $'\b' ]; then
            # 退格
            if [ -n "$val" ]; then
                val="${val%?}"
                printf "\b \b"
            fi
        else
            val="$val$key"
            printf "*"
        fi
    done

    [ -n "$old_stty" ] && stty "$old_stty" 2>/dev/null
    trap - INT

    # 赋给变量
    if [ -n "$val" ]; then
        eval "$varname=\"\$val\""
        # 显示确认 (后 N 位)
        if [ "$show_last" != "0" ] && [ ${#val} -ge 4 ]; then
            local last="${val: -${show_last}}"
            printf "  %s已输入 ***%s%s\n" "$GREEN" "$last" "$NC"
        elif [ -n "$val" ]; then
            printf "  %s已输入 (%d 字符)%s\n" "$GREEN" "${#val}" "$NC"
        fi
    fi

    printf ""
}

CONF="/etc/frpm-setup.conf"
ACME_HOME="${HOME}/.acme.sh"

# ============ root 检查 ============
[ "$(id -u)" -eq 0 ] || die "请用 root 或 sudo 运行"

# ============ 收集参数 ============
collect_params() {
    if [ -f "$CONF" ] && [ -s "$CONF" ]; then
        # shellcheck source=/dev/null
        . "$CONF"
        read -r -p "使用上次配置 ($CONF)? [Y/n] " -e
        if [ "$REPLY" != "n" ] && [ "$REPLY" != "N" ]; then
            echo -e "${GREEN}使用已有配置:${NC}"
            echo "  DOMAIN        = $DOMAIN"
            echo "  SUBDOMAIN     = $SUBDOMAIN"
            echo "  SERVER_IP     = $SERVER_IP"
            echo "  EMAIL         = $EMAIL"
            echo "  DNS_PROVIDER  = $DNS_PROVIDER"
            echo "  FRPM_PORT     = $FRPM_PORT"
            echo "  FRPS_BIND_PORT= $FRPS_BIND_PORT"
            echo "  FRPM_TOKEN    = ${FRPM_TOKEN:0:8}..."
            return 0
        fi
    fi

    echo -e "${YELLOW}=== 收集配置参数 ===${NC}"

    read -r -p "主域名 (如 example.com): " -e DOMAIN
    read -r -p "子域名前缀 (如 service): " -e SUBDOMAIN
    read -r -p "服务器公网 IP: " -e SERVER_IP
    read -r -p "证书联系邮箱: " -e EMAIL
    read -r -p "DNS 服务商 (tencent/aliyun/cloudflare): " -e DNS_PROVIDER

    # 根据 DNS 服务商展示对应的提示文案
    case "$DNS_PROVIDER" in
        tencent)
            KEY_PROMPT="DNS API Key ID (SecretId, AKID 开头，回车跳过用环境变量)"
            SECRET_PROMPT="DNS API Secret (SecretKey, 32 位，回车跳过用环境变量)"
            ;;
        aliyun)
            KEY_PROMPT="DNS API Key ID (AccessKey ID，回车跳过用环境变量)"
            SECRET_PROMPT="DNS API Secret (AccessKey Secret，回车跳过用环境变量)"
            ;;
        cloudflare)
            KEY_PROMPT="(Cloudflare 不需要 Key ID，直接回车)"
            SECRET_PROMPT="Cloudflare API Token (回车跳过用环境变量)"
            ;;
    esac

    read_hidden "$KEY_PROMPT: " DNS_API_KEY 4
    if [ -z "$DNS_API_KEY" ]; then
        # 用户按回车跳过，从环境变量兜底
        case "$DNS_PROVIDER" in
            tencent)    DNS_API_KEY="$TENCENT_SECRET_ID" ;;
            aliyun)     DNS_API_KEY="$ALIYUN_ACCESS_KEY_ID" ;;
            cloudflare) DNS_API_KEY="" ;;
        esac
        [ -n "$DNS_API_KEY" ] && info "Key ID 从环境变量复用: ${DNS_API_KEY:0:8}..."
    fi

    read_hidden "$SECRET_PROMPT: " DNS_API_SECRET 4
    if [ -z "$DNS_API_SECRET" ]; then
        # 用户按回车跳过，从环境变量兜底
        case "$DNS_PROVIDER" in
            tencent)    DNS_API_SECRET="$TENCENT_SECRET_KEY" ;;
            aliyun)     DNS_API_SECRET="$ALIYUN_ACCESS_KEY_SECRET" ;;
            cloudflare) DNS_API_SECRET="$CF_API_KEY" ;;
        esac
        [ -n "$DNS_API_SECRET" ] && info "Secret 从环境变量复用"
    fi

    # 最后验证：两个都有值才继续
    if [ -z "$DNS_API_SECRET" ]; then
        die "DNS Secret 为空：输入时按了回车，且环境变量也没有。请重新跑脚本输入，或先 export 环境变量"
    fi
    if [ "$DNS_PROVIDER" != "cloudflare" ] && [ -z "$DNS_API_KEY" ]; then
        die "DNS Key ID 为空：输入时按了回车，且环境变量也没有。请重新跑脚本输入"
    fi
    ok "DNS API 已配置"

    read -r -p "frpm WebUI 端口 [2003]: " -e FRPM_PORT
    FRPM_PORT="${FRPM_PORT:-2003}"

    read -r -p "frps 监听端口 [2005]: " -e FRPS_BIND_PORT
    FRPS_BIND_PORT="${FRPS_BIND_PORT:-2005}"

    read -r -p "frps vhostHTTPPort [2006]: " -e FRPS_VHOST_HTTP
    FRPS_VHOST_HTTP="${FRPS_VHOST_HTTP:-2006}"

    read -r -p "frps 面板端口 [5067]: " -e FRPS_DASHBOARD_PORT
    FRPS_DASHBOARD_PORT="${FRPS_DASHBOARD_PORT:-5067}"

    read -r -p "frps 认证 Token (回车随机生成): " -e FRPM_TOKEN
    if [ -z "$FRPM_TOKEN" ]; then
        FRPM_TOKEN=$(openssl rand -hex 24)
    fi

    read -r -p "frpm 容器名 [frpm]: " -e FRPM_CONTAINER
    FRPM_CONTAINER="${FRPM_CONTAINER:-frpm}"

    echo ""
    echo "=== 即将使用以下配置 ==="
    echo "  主域名: $DOMAIN"
    echo "  子域名: $SUBDOMAIN"
    echo "  服务器 IP: $SERVER_IP"
    echo "  邮箱: $EMAIL"
    echo "  DNS 服务商: $DNS_PROVIDER"
    echo "  frpm WebUI 端口: $FRPM_PORT"
    echo "  frps 监听端口: $FRPS_BIND_PORT"
    echo "  frps vhostHTTPPort: $FRPS_VHOST_HTTP"
    echo "  frps 面板端口: $FRPS_DASHBOARD_PORT"
    echo "  frpm Token: ${FRPM_TOKEN:0:8}..."
    echo "  frpm 容器名: $FRPM_CONTAINER"
    echo ""
    read -r -p "确认无误? [Y/n] " -e
    if [ "$REPLY" = "n" ] || [ "$REPLY" = "N" ]; then
        collect_params
        return 0
    fi

    {
        echo "# frpm 部署配置 (自动生成，可手动编辑)"
        echo "DOMAIN=$DOMAIN"
        echo "SUBDOMAIN=$SUBDOMAIN"
        echo "SERVER_IP=$SERVER_IP"
        echo "EMAIL=$EMAIL"
        echo "DNS_PROVIDER=$DNS_PROVIDER"
        echo "DNS_API_KEY=$DNS_API_KEY"
        echo "DNS_API_SECRET=$DNS_API_SECRET"
        echo "FRPM_PORT=$FRPM_PORT"
        echo "FRPS_BIND_PORT=$FRPS_BIND_PORT"
        echo "FRPS_VHOST_HTTP=$FRPS_VHOST_HTTP"
        echo "FRPS_DASHBOARD_PORT=$FRPS_DASHBOARD_PORT"
        echo "FRPM_TOKEN=$FRPM_TOKEN"
        echo "FRPM_CONTAINER=$FRPM_CONTAINER"
    } > "$CONF"
    chmod 600 "$CONF"
    ok "配置已保存到 $CONF"
}

# ============ 检查依赖 ============
check_deps() {
    info "检查依赖..."

    apt update -qq
    apt install -y -qq nginx ca-certificates curl openssl cron

    if ! command -v docker >/dev/null 2>&1; then
        warn "Docker 未安装，尝试安装..."
        local ok=false
        # 1) 官方源
        if curl -fsSL --max-time 15 https://get.docker.com -o /tmp/get-docker.sh 2>/dev/null; then
            sh /tmp/get-docker.sh >/dev/null 2>&1 && ok=true
        else
            warn "  官方源不通，切换腾讯云 mirror..."
        fi
        # 2) 腾讯云 mirror（国内常用）
        if ! $ok; then
            curl -fsSL --max-time 15 https://mirrors.cloud.tencent.com/docker-ce/linux/ubuntu/gpg \
              -o /usr/share/keyrings/docker-archive-keyring.gpg 2>/dev/null \
              || { gpg --dearmor < /dev/null >/dev/null 2>&1; true; }
            # 如果上面的 gpg 没成功（没有 keyring 文件），重新下载并 decode
            if [ ! -f /usr/share/keyrings/docker-archive-keyring.gpg ]; then
                curl -fsSL --max-time 15 https://mirrors.cloud.tencent.com/docker-ce/linux/ubuntu/gpg \
                  | gpg --dearmor > /usr/share/keyrings/docker-archive-keyring.gpg 2>/dev/null || true
            fi
            echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://mirrors.cloud.tencent.com/docker-ce/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
              > /etc/apt/sources.list.d/docker.list
            apt update -qq >/dev/null 2>&1
            apt install -y -qq docker-ce docker-ce-cli containerd.io >/dev/null 2>&1 && ok=true
        fi
        # 3) apt 装 docker.io（最后兜底，版本老但能用）
        if ! $ok; then
            warn "  mirror 也不通，尝试 apt 装 docker.io..."
            apt install -y -qq docker.io >/dev/null 2>&1 && ok=true
        fi
        if ! $ok; then
            die "Docker 安装失败，请手动装好后重跑脚本"
        fi
        systemctl start docker
        systemctl enable docker
        ok "Docker 已安装: $(docker --version)"
    else
        ok "Docker 已安装: $(docker --version)"
    fi

    if [ ! -x "$ACME_HOME/acme.sh" ]; then
        info "安装 acme.sh..."
        curl https://get.acme.sh | sh
    else
        ok "acme.sh 已安装"
    fi

    ok "依赖检查完成"
}

# ============ DNS 泛解析 (用户手动添加) ============
dns_guide() {
    echo ""
    echo -e "${YELLOW}=== 请在 DNS 服务商后台添加以下记录 ===${NC}"
    echo ""
    echo "  类型: A"
    echo "  主机记录: *.$SUBDOMAIN"
    echo "  记录值: $SERVER_IP"
    echo "  TTL: 600"
    echo ""
    echo "  (可选) 类型: A"
    echo "  主机记录: $SUBDOMAIN"
    echo "  记录值: $SERVER_IP"
    echo ""
    echo "  说明: 添加 *.$SUBDOMAIN 泛解析后，frpm.$SUBDOMAIN.$DOMAIN 等子域都能解析"
    echo ""
    read -r -p "已在 DNS 后台添加? [Y/n] " -e
    if [ "$REPLY" = "n" ] || [ "$REPLY" = "N" ]; then
        die "请先添加 DNS 记录再继续"
    fi
}

# ============ 申请证书 ============
issue_cert() {
    local cert_dir="/etc/nginx/certs/$SUBDOMAIN.$DOMAIN"
    mkdir -p "$cert_dir"

    info "申请证书: $DOMAIN + *.$SUBDOMAIN.$DOMAIN ..."

    local dns_arg
    case "$DNS_PROVIDER" in
        tencent)
            # DNS_API_KEY/SECRET 已经是最终值(用户输入优先,空值时已用环境变量兜底过)
            export TENCENT_SECRET_ID="$DNS_API_KEY"
            export TENCENT_SECRET_KEY="$DNS_API_SECRET"
            dns_arg="dns_tencent"
            ;;
        aliyun)
            export ALIYUN_ACCESS_KEY_ID="$DNS_API_KEY"
            export ALIYUN_ACCESS_KEY_SECRET="$DNS_API_SECRET"
            dns_arg="dns_aliyun"
            ;;
        cloudflare)
            export CF_API_EMAIL="$EMAIL"
            export CF_API_KEY="$DNS_API_SECRET"
            dns_arg="dns_cf"
            ;;
        *)
            die "不支持的 DNS 服务商: $DNS_PROVIDER"
            ;;
    esac

    # 验证 key 非空,并把前 8 位/后 4 位打出来确认
    case "$DNS_PROVIDER" in
        tencent)
            [ -n "$TENCENT_SECRET_ID" ] && [ -n "$TENCENT_SECRET_KEY" ] || die "TENCENT_SECRET_ID/KEY 为空"
            ok "TENCENT_SECRET_ID: ${TENCENT_SECRET_ID:0:8}...${TENCENT_SECRET_ID: -4}"
            ok "TENCENT_SECRET_KEY: ***${TENCENT_SECRET_KEY: -4}"
            ;;
        aliyun)
            [ -n "$ALIYUN_ACCESS_KEY_ID" ] && [ -n "$ALIYUN_ACCESS_KEY_SECRET" ] || die "ALIYUN_ACCESS_KEY 为空"
            ok "ALIYUN_ACCESS_KEY_ID: ${ALIYUN_ACCESS_KEY_ID:0:8}..."
            ok "ALIYUN_ACCESS_KEY_SECRET: ***${ALIYUN_ACCESS_KEY_SECRET: -4}"
            ;;
        cloudflare)
            [ -n "$CF_API_KEY" ] || die "CF_API_KEY 为空"
            ok "CF_API_KEY: ***${CF_API_KEY: -4}"
            ;;
    esac

    "$ACME_HOME/acme.sh" --issue \
        -d "$DOMAIN" \
        -d "*.$SUBDOMAIN.$DOMAIN" \
        --dns "$dns_arg" \
        -m "$EMAIL"

    "$ACME_HOME/acme.sh" --install-cert -d "$DOMAIN" -d "*.$SUBDOMAIN.$DOMAIN" \
        --key-file "$cert_dir/$SUBDOMAIN.key" \
        --fullchain-file "$cert_dir/$SUBDOMAIN.crt" \
        --reloadcmd "systemctl reload nginx"

    ok "证书已安装到 $cert_dir"
}

# ============ 防火墙 ============
setup_firewall() {
    if command -v ufw >/dev/null 2>&1; then
        ufw allow 80/tcp >/dev/null 2>&1 || true
        ufw allow 443/tcp >/dev/null 2>&1 || true
        ufw allow "$FRPM_PORT/tcp" >/dev/null 2>&1 || true
        ufw allow "$FRPS_BIND_PORT/tcp" >/dev/null 2>&1 || true
        ok "防火墙放行 80/443/$FRPM_PORT/$FRPS_BIND_PORT"
    else
        warn "未检测到 ufw，跳过防火墙配置"
        warn "请手动放行: 80, 443, $FRPM_PORT, $FRPS_BIND_PORT"
    fi
}

# ============ nginx ============
setup_nginx() {
    local conf="/etc/nginx/conf.d/service-frpm.conf"
    local cert_dir="/etc/nginx/certs/$SUBDOMAIN.$DOMAIN"

    info "配置 nginx 反代 frpm.$SUBDOMAIN.$DOMAIN -> 127.0.0.1:$FRPM_PORT ..."

    cat > "$conf" <<EOF
server {
    listen 80;
    listen 443 ssl;
    http2 on;
    server_name frpm.$SUBDOMAIN.$DOMAIN;

    ssl_certificate     $cert_dir/$SUBDOMAIN.crt;
    ssl_certificate_key $cert_dir/$SUBDOMAIN.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    client_max_body_size 500m;

    location / {
        proxy_pass http://127.0.0.1:$FRPM_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
EOF

    nginx -t && systemctl reload nginx

    ok "nginx 配置完成: $conf"
}

# ============ 部署 frpm ============
deploy_frpm() {
    info "部署 frpm 容器..."

    mkdir -p /data/frpm /data/frpm-configs

    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${FRPM_CONTAINER}$"; then
        warn "容器 $FRPM_CONTAINER 已存在"
        read -r -p "删除并重建? [y/N] " -e
        if [ "$REPLY" = "y" ] || [ "$REPLY" = "Y" ]; then
            docker stop "$FRPM_CONTAINER" >/dev/null 2>&1 || true
            docker rm -f "$FRPM_CONTAINER" >/dev/null 2>&1 || true
        else
            warn "跳过部署，保留现有容器"
            return 0
        fi
    fi

    docker run -d \
        --name "$FRPM_CONTAINER" \
        --restart unless-stopped \
        -p "$FRPM_PORT:8080" \
        -v /data/frpm:/data/frpm \
        -v /data/frpm-configs:/data/frpm-configs \
        -v /var/run/docker.sock:/var/run/docker.sock \
        --cap-add=SYS_PTRACE \
        --security-opt seccomp=unconfined \
        mejeor/easy-frp-manager:latest

    sleep 5
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${FRPM_CONTAINER}$"; then
        ok "frpm 容器已启动 (端口 $FRPM_PORT)"
    else
        err "frpm 启动失败，查看日志: docker logs $FRPM_CONTAINER"
    fi
}

# ============ 主流程 ============
main() {
    echo ""
    echo -e "${GREEN}=== frpm 一键部署脚本 ===${NC}"
    echo ""

    collect_params
    check_deps
    dns_guide
    issue_cert
    setup_firewall
    setup_nginx
    deploy_frpm

    echo ""
    echo -e "${GREEN}=== 部署完成 ===${NC}"
    echo ""
    echo "下一步："
    echo ""
    echo "1. 访问 https://frpm.$SUBDOMAIN.$DOMAIN 登录 frpm"
    echo "   默认账号: admin/admin123"
    echo ""
    echo "2. 在 frpm 里新建 frps 实例，配置："
    echo "   - 实例名: frps-main"
    echo "   - 监听地址: 0.0.0.0"
    echo "   - 监听端口: $FRPS_BIND_PORT"
    echo "   - 认证 Token: $FRPM_TOKEN"
    echo "   - 面板地址: 127.0.0.1"
    echo "   - 面板端口: $FRPS_DASHBOARD_PORT"
    echo "   - vhostHTTPPort: $FRPS_VHOST_HTTP"
    echo ""
    echo "3. frps 启动后，在 frpm 里新建 frpc 实例（指向这个 frps）"
    echo "   frpc 的 serverAddr: frpm.$SUBDOMAIN.$DOMAIN"
    echo "   frpc 的 serverPort: $FRPS_BIND_PORT"
    echo "   frpc 的 token:      $FRPM_TOKEN"
    echo ""
    echo "4. 自动续期检查："
    echo "   crontab -l   # 确认有 acme.sh 续期任务"
    echo "   证书到期前 30 天自动续期，无需手动"
    echo ""
    echo -e "${YELLOW}配置文件: $CONF${NC}"
    echo -e "${YELLOW}下次运行: sudo bash setup-server.sh  (会问是否复用配置)${NC}"
    echo ""
}

main "$@"
