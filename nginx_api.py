"""FRP Manager - nginx 配置 API 模块。

负责 frps 宿主机 nginx 的读取与一键配置:
- GET  /api/nginx/templates  列出宿主 conf.d 模板列表 + 探测可用证书
- POST /api/nginx/apply      写入 server block → nginx -t → HUP reload(失败回滚)

所有宿主文件系统操作都走 helper 容器(挂载宿主根目录 + chroot),
避免 frpm 容器与宿主 nginx 文件系统隔离带来的权限/路径问题。
关键参数:
  --pid host                          : 与宿主共享 PID 命名空间,HUP 信号才能到达宿主 nginx
  --security-opt apparmor=unconfined  : 宿主 nginx 有 AppArmor profile,直接 nginx -s reload 的 kill 会被拒
  -i + stdin                          : 配置内容经 stdin 传入,避免引号转义
注册方式:from nginx_api import register_nginx_routes; register_nginx_routes(app)
(用函数注册而非模块级装饰器,避免 app.py ↔ nginx_api.py 循环依赖)
"""

import os
import re
import subprocess


def _nginx_helper_image() -> str:
    """探测当前 frpm 容器镜像,用作 helper 容器镜像(保证本机存在)。"""
    for img in (os.environ.get("FRPM_IMAGE", ""), "frp-manager:local",
                "mejeor/easy-frp-manager:latest"):
        if img:
            r = subprocess.run(["docker", "images", "-q", img],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return img
    return "frp-manager:local"


def _nginx_helper(cmd: str, stdin_data: str = "") -> dict:
    """执行 nginx helper 容器命令。返回 {ok, output, exit_code}。"""
    img = _nginx_helper_image()
    full = [
        "docker", "run", "--rm", "-i",
        "--pid", "host",
        "--security-opt", "apparmor=unconfined",
        "-v", "/:/host",
        "--entrypoint", "sh", img, "-c", cmd,
    ]
    try:
        r = subprocess.run(full, input=stdin_data.encode("utf-8"),
                           capture_output=True, timeout=60)
        output = (r.stdout or b"").decode("utf-8", errors="replace")
        err = (r.stderr or b"").decode("utf-8", errors="replace")
        return {"ok": r.returncode == 0, "exit_code": r.returncode,
                "output": output, "error": err}
    except FileNotFoundError:
        return {"ok": False, "exit_code": -1, "output": "", "error": "docker 不可用"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": -1, "output": "", "error": "helper 容器超时"}


def _parse_conf_blocks(output: str):
    """解析 helper 输出:按 ===size===path 分隔的 conf 内容块。返回 [(size, path, lines)]。"""
    blocks = []
    cur = None
    for line in output.splitlines():
        if line.startswith("==="):
            parts = line.split("===")
            if len(parts) >= 3:
                cur = {"size": parts[1], "path": parts[2], "lines": []}
                blocks.append(cur)
            continue
        if line.startswith("CERTS="):
            continue
        if cur is not None:
            cur["lines"].append(line)
    return [(b["size"], b["path"], b["lines"]) for b in blocks]


def _tpl_summary(content: str) -> str:
    """从模板内容抽取 server_name / proxy_pass 摘要(一行)。跳过注释行。"""
    parts = []
    for line in content.splitlines():
        stripped = line.strip()
        # 跳过空行与注释行
        if not stripped or stripped.startswith("#"):
            continue
        for m in re.finditer(r"(server_name|proxy_pass)\s+([^;]+);", line):
            parts.append(f"{m.group(1).strip()} {m.group(2).strip()}")
    return " / ".join(parts[:6])


def _tpl_usable_reason(content: str) -> str:
    """判断模板是否可安全复用。返回 '' 表示可复用,否则给出不可复用原因。

    可安全复用的模板需同时满足:
      - 有具体域名的 server_name(非 _ / 空)
      - 所有 proxy_pass 都是 ip:port 形式(可替换成 vhostHTTPPort)
      - 不含 upstream(独立后端资源池,复制会 duplicate upstream)
      - 至少有一个反向代理语句(排除纯端口监听/工具类)
    """
    if not content.strip():
        return "配置为空"
    # upstream 检查只针对非注释行(注释里的 "upstream" 字样不算)
    has_upstream = any(
        l.strip() and not l.strip().startswith("#") and "upstream " in l
        for l in content.splitlines()
    )
    if has_upstream:
        return "含 upstream,不适合作为模板"
    # server_name 检查:非注释行;多域名任一为具体域名即可
    server_matches = []
    for line in content.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        server_matches += list(re.finditer(r"server_name\s+([^;]+);", line))
    has_concrete_domain = any(
        part not in ("_", "")
        for m in server_matches
        for part in m.group(1).strip().split()
    )
    if not has_concrete_domain:
        return "无具体域名(server_name 为 _ 或为空)"
    # proxy_pass 检查:只取非注释行
    proxy_lines = [l.strip() for l in content.splitlines()
                   if l.strip() and not l.strip().startswith("#") and "proxy_pass " in l]
    if not proxy_lines:
        return "无反向代理语句(纯端口监听/工具类)"
    # 每个 proxy_pass 都必须是 ip:port,否则无法替换成 vhostHTTPPort
    bad = [l for l in proxy_lines
           if not re.search(r"proxy_pass\s+http://(127\.0\.0\.1|localhost|\[::1\]):(\d+)", l)]
    if bad:
        return "proxy_pass 含非 IP:端口形式(无法自动替换端口)"
    return ""


def register_nginx_routes(app):
    """注册 nginx 相关路由到 frpm App 实例(避免模块间循环依赖)。"""

    @app.get("/api/nginx/templates")
    def api_nginx_templates(ctx):
        """列出 frps 宿主 /etc/nginx/conf.d/ 下的 .conf 服务模板。

        供前端「复用已有模板」下拉使用。返回 {templates: [{filename, size, summary, is_default}]}。
        可选 query ?filename=xxx.conf 读取该模板完整内容。
        走 helper 容器读宿主目录(经 chroot)。
        """
        query = ctx.get("query") or {}
        # query 是 {k: [v]} 形式(parse_qs),取第一个值
        want = (query.get("filename") or [""])
        want = want[0] if isinstance(want, list) else want
        want = (want or "").strip()

        # 若指定 filename,读取内容
        if want:
            if not re.fullmatch(r"[A-Za-z0-9._-]+\.conf", want):
                return 400, {"error": "非法文件名"}
            fr = _nginx_helper(f"cat /host/etc/nginx/conf.d/{want} 2>/dev/null")
            if not fr["ok"] or not fr["output"].strip():
                return 404, {"error": f"模板 {want} 不存在或不可读"}
            return {"success": True, "filename": want, "content": fr["output"]}

        # 一次 helper 调用:遍历 conf.d,输出 每个文件大小+路径+完整内容(以空行分隔),并探测证书
        r = _nginx_helper(
            "for f in /host/etc/nginx/conf.d/*.conf; do "
            "[ -f \"$f\" ] || continue; "
            "sz=$(wc -c < \"$f\"); echo \"===$sz===${f#/host}\"; "
            "cat \"$f\"; echo; "
            "done; "
            # 证书探测:优先 /etc/nginx/certs,次 /data/frpm/certs,输出 CERTS=<crt>|<key>
            "for d in /host/etc/nginx/certs /host/data/frpm/certs; do "
            "for c in $d/*.crt; do [ -f \"$c\" ] || continue; "
            "[ -f \"${c%.crt}.key\" ] && { echo \"CERTS=${c#/host}|${c%.crt}.key\"; break 2; }; "
            "done; done"
        )
        if not r["ok"]:
            return 200, {"success": False, "error": r["error"] or "无法读取 conf.d", "templates": []}
        files = []
        default_cert = default_key = ""
        # 解析 helper 输出:CERTS= 行在最后(证书探测),===size===path 块在前(conf 内容)
        for line in r["output"].splitlines():
            if line.startswith("CERTS="):
                c, k = line[len("CERTS="):].split("|", 1)
                if not default_cert:
                    default_cert, default_key = c, k
        for size, path, lines in _parse_conf_blocks(r["output"]):
            filename = os.path.basename(path)
            # 白名单:只处理普通 .conf 文件名(防文件名注入 shell)
            if not re.fullmatch(r"[A-Za-z0-9._-]+\.conf", filename):
                continue
            if filename.startswith(".bak") or "~" in filename or filename.startswith("zz-"):
                continue
            content = "\n".join(lines)
            reason = _tpl_usable_reason(content)
            usable = reason == ""
            files.append({
                "filename": filename, "size": size,
                "summary": _tpl_summary(content),
                "is_default": filename == "service-frpm.conf",
                "usable": usable,
                "reason": reason,
            })
        files.sort(key=lambda x: (not x["usable"], x["is_default"] is False, x["filename"]))
        return 200, {"success": True, "templates": files,
                     "default_cert": default_cert, "default_key": default_key}

    @app.post("/api/nginx/apply")
    def api_nginx_apply(ctx):
        """一键写入 nginx server block 到 frps 宿主机并 reload。

        入参:
          - filename: 写入 /etc/nginx/conf.d/ 的文件名(如 vault-server.conf)
          - config: 完整 nginx server block 文本(由前端生成)

        流程:
          1. helper 容器(chroot 宿主根目录)写入 /etc/nginx/conf.d/<filename>
          2. nginx -t 校验;失败则删除刚写的文件(回滚),不 reload
          3. 通过则 kill -HUP 宿主 nginx master(reload 新配置)
        """
        body = ctx["body"] or {}
        filename = (body.get("filename") or "").strip()
        config = body.get("config") or ""
        overwrite = body.get("overwrite") is True

        # 文件名白名单:只允许 conf.d 下的普通 .conf 名,防路径穿越
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.conf", filename):
            return 400, {"error": f"非法文件名: {filename!r}(只允许 xxx.conf)"}
        if not config.strip():
            return 400, {"error": "配置内容为空"}
        if len(config) > 65536:
            return 400, {"error": "配置内容过大(>64KB)"}
        # 必须包含 server 块,防止误提交
        if "server {" not in config:
            return 400, {"error": "配置中未找到 server 块"}

        dest = f"/host/etc/nginx/conf.d/{filename}"
        # 覆盖保护与写入合并为一步:文件已存在且未显式允许覆盖时,输出 EXISTS 并中止
        # (避免单独起一个 helper 容器做存在性检查,省一次容器启动)
        exists_guard = f"if [ -f {dest} ] && [ '{overwrite}' != 'True' ]; then echo 'FILE_EXISTS'; exit 9; fi; " if not overwrite else ""
        # 一步完成:写入→nginx -t→(成功) HUP reload / (失败) 删除回滚。
        # 注意:用分号分隔,绝不能依赖换行(helper 经 stdin sh -c, 换行会丢)
        cmd = (
            f"{exists_guard}"
            f"cat > {dest}; "
            f"if chroot /host /usr/sbin/nginx -t >/host/tmp/frpm-nginx-t.log 2>&1; then "
            f"kill -HUP $(cat /host/run/nginx.pid); echo 'RELOAD_OK'; "
            f"else "
            f"rm -f {dest}; echo 'TEST_FAILED_ROLLED_BACK'; cat /host/tmp/frpm-nginx-t.log; exit 1; "
            f"fi"
        )
        r = _nginx_helper(cmd, stdin_data=config)
        output = (r["output"] + ("\n" + r["error"] if r["error"] else "")).strip()

        # 覆盖保护触发:文件已存在且未显式覆盖
        if "FILE_EXISTS" in output:
            return 409, {
                "error": f"目标文件 {filename} 已存在,如要覆盖请在前端确认(overwrite=true)",
                "exists": True,
            }

        # 超时:helper 容器超时(如宿主 nginx -t 卡死),文件可能已写入但未验证/未回滚,如实告知
        if "helper 容器超时" in (r.get("error") or ""):
            return 200, {
                "success": False,
                "message": f"操作超时(60s),文件 {filename} 可能已写入但未完成校验,请到宿主检查 /etc/nginx/conf.d/{filename}",
                "output": output,
            }

        if not r["ok"]:
            return 200, {
                "success": False,
                "message": "nginx -t 校验失败,已回滚(未写入)",
                "output": output,
            }

        return 200, {
            "success": True,
            "message": f"已写入 {filename} 并重载 nginx",
            "output": output,
            "filename": filename,
        }