// ============ 域名穿透指南(独立模块) ============
// 从 index.html 拆出:nginx 配置指南相关函数。
// 依赖 index.html 主脚本的全局:api / toast / escapeHtml / closeModal /
// frpsStatusData / detailInstance / nginxGuideState 等(请保证本文件在 index.html 主 <script> 之后加载)。
// 依赖的全局变量:nginxGuideState(本文件定义), frpsStatusData/detailInstance(主脚本定义)。

// ============ 域名穿透指南 ============
let nginxGuideState = { instanceId: null, proxyId: null, proxy: null, instance: null, vhostPort: null, selectedTemplateContent: null };

function showNginxConfigForFrpsProxy(instanceId, proxyIndex) {
  // 重置覆盖确认状态
  nginxForceOverwrite = false;
  // 从 frps 实时状态里取指定索引的代理(nginx 配置指南的入口在 frps 详情页代理列表)
  const proxies = (frpsStatusData && frpsStatusData.proxies) || [];
  const p = proxies[proxyIndex];
  if (!p) {
    toast('未找到该代理,请先刷新实时状态', 'error');
    return;
  }
  const conf = p.conf || {};
  const domains = conf.customDomains || (conf.subdomain ? [conf.subdomain] : []);
  if (!domains.length) {
    toast('该代理未配置域名', 'error');
    return;
  }
  const ptype = p._type || 'http';
  // https 代理走 vhostHTTPSPort,http 代理走 vhostHTTPPort
  const vhostPort = ptype === 'https'
    ? parseVhostPortFromConfig('vhostHTTPSPort')
    : parseVhostPortFromConfig('vhostHTTPPort');
  // https 代理需要 frps 配置 vhostHTTPSPort
  if (ptype === 'https' && !vhostPort) {
    toast('该代理是 HTTPS 类型,但 frps 未配置 vhostHTTPSPort,无法生成 nginx 配置', 'error');
    return;
  }
  // 构造 frpm 代理结构(兼容 generateNginxConfigLive)
  nginxGuideState = {
    instanceId,
    vhostPort,
    proxy: {
      name: p.name || 'proxy',
      type: ptype,
      id: '-',
      custom_domains: domains,
      local_ip: conf.localIP || '127.0.0.1',
      local_port: conf.localPort || 80,
    },
    instance: null,
  };
  renderNginxGuideModal(nginxGuideState.proxy);
}

// 从 frps 实例配置里解析 vhostHTTPPort / vhostHTTPSPort(当前详情页的 frps 实例)
function parseVhostPortFromConfig(key = 'vhostHTTPPort') {
  if (!detailInstance || !detailInstance.config_content) return null;
  const content = detailInstance.config_content;
  // camelCase(frp TOML 新格式):vhostHTTPPort = 2006
  const m1 = content.match(new RegExp(key + '\\s*=\\s*(\\d+)'));
  if (m1) return parseInt(m1[1], 10);
  // 全小写下划线(frp INI 旧格式):vhostHTTPPort -> vhost_http_port = 2006
  const iniKey = key
    .replace(/([A-Z]+)([A-Z][a-z])/g, '_$1_$2')  // HTTPPort -> _HTTP_Port
    .replace(/([a-z])([A-Z])/g, '$1_$2')         // vhostHTTP -> vhost_HTTP
    .toLowerCase();
  const m2 = content.match(new RegExp(iniKey + '\\s*=\\s*(\\d+)'));
  return m2 ? parseInt(m2[1], 10) : null;
}

// frps 对 http/https 代理不返回 localPort, 此时不显示端口避免误导
function frpsProxyBackendText(proxy) {
  const ip = proxy.local_ip || '127.0.0.1';
  const port = proxy.local_port;
  return (port != null && port !== '' && port !== 0) ? `${ip}:${port}` : ip;
}

function renderNginxGuideModal(proxy) {
  const domains = proxy.custom_domains || [];
  // 默认证书路径占位(模板列表加载后会探测宿主真实证书并覆盖):
  // 优先 /etc/nginx/certs 下的通配符证书(service.235300.xyz 体系)
  const zone = (domains[0] || '').split('.').slice(-2).join('.');
  const defaultCertPath = `/etc/nginx/certs/service.${zone || 'example.com'}.crt`;
  const defaultKeyPath = defaultCertPath.slice(0, -4) + '.key';

  // 占位配置:先把 nginx 配置塞到变量,后面更新
  const initialConfig = generateNginxConfigLive();

  document.getElementById('modal-container').innerHTML = `
    <div class="modal-card" style="width:52rem;max-width:100%;" id="nginx-modal">
      <div class="modal-head">
        <h3>🌐 nginx 配置(复制粘贴到 frps 主机)</h3>
        <button onclick="closeModal()" class="modal-close">&times;</button>
      </div>
      <div class="modal-body" style="max-height:84vh;overflow-y:auto;">

        <div class="form-sec" style="margin-bottom:.9rem;">
          <div class="form-grid2" style="margin:0;">
            <div><span class="form-label" style="display:inline">代理:</span> <span class="mono">${escapeHtml(proxy.name)}</span></div>
            <div><span class="form-label" style="display:inline">类型:</span> <span class="mono">${escapeHtml(proxy.type)}</span></div>
            <div><span class="form-label" style="display:inline">域名:</span> <span class="mono" style="color:var(--info-fg)">${escapeHtml(domains.join(', ') || '未配置')}</span></div>
            <div><span class="form-label" style="display:inline">frpc 后端:</span> <span class="mono">${escapeHtml(frpsProxyBackendText(proxy))}</span></div>
          </div>
        </div>

        <!-- 步骤 1: nginx 配置 -->
        <details class="form-sec" style="margin-bottom:.9rem;" open>
          <summary class="form-sec-title">① nginx 反向代理配置(frp 链路)</summary>
          <div style="padding-top:.6rem;">
            ${nginxGuideState.vhostPort ? `
            <div class="info-note green" style="margin-bottom:.7rem;">
              ✅ frps 的 <span class="mono">vhostHTTPPort = ${nginxGuideState.vhostPort}</span>。<br>
              nginx 把请求转发到该端口,frps 再按域名(Host 头)路由到 frpc 代理。
            </div>` : `
            <div class="info-note red" style="margin-bottom:.7rem;">
              ⚠️ 未找到 frps 的 <span class="mono">vhostHTTPPort</span> 配置,无法生成 nginx 配置。<br>
              请先编辑本 frps 实例,添加 <span class="mono">vhostHTTPPort = 2006</span>(或其他空闲端口)。
            </div>`}
            <!-- 复用已有模板:从 frps 宿主 conf.d 选模板 -->
            <div class="form-sec" style="margin-bottom:.7rem;background:#f6f7ff;">
              <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:.4rem;">
                <label class="form-label" style="margin:0;display:inline">复用已有 conf 模板 <span style="color:var(--muted);font-weight:400">(可选,套用现有证书/超时/gzip 配置)</span></label>
                <button id="nginx-refresh-tpl" onclick="loadNginxTemplates()" style="font-size:.78rem;color:var(--info-fg);background:none;border:none;cursor:pointer;">↻ 刷新</button>
              </div>
              <select id="nginx-template-select" onchange="onNginxTemplateSelect()" class="input" style="font-size:.8rem;">
                <option value="">— 不使用模板(用下方通用配置) —</option>
              </select>
              <div id="nginx-tpl-sum" class="hidden" style="margin-top:.4rem;font-size:.78rem;color:var(--muted-light);"></div>
            </div>
            <!-- SSL 开关 + 证书路径 -->
            <div class="form-sec" style="margin-bottom:.7rem;background:#f6f7ff;">
              <div style="display:flex;align-items:center;gap:.8rem;flex-wrap:wrap;">
                <label style="display:flex;align-items:center;gap:.45rem;cursor:pointer;font-size:.85rem;font-weight:600;">
                  <input type="checkbox" id="nginx-ssl-toggle" checked onchange="updateNginxConfigPreview()" style="width:1.05rem;height:1.05rem;accent-color:var(--accent);cursor:pointer">
                  启用 HTTPS
                </label>
                <div id="nginx-cert-fields" style="flex:1;display:grid;grid-template-columns:1fr 1fr;gap:.5rem;min-width:18rem;">
                  <div>
                    <label class="form-label" style="font-size:.72rem;">证书路径</label>
                    <input type="text" id="nginx-cert-path" value="${escapeHtml(defaultCertPath)}" oninput="updateNginxConfigPreview(); this.dataset.userEdited='1'" class="input" style="font-size:.78rem;">
                  </div>
                  <div>
                    <label class="form-label" style="font-size:.72rem;">密钥路径</label>
                    <input type="text" id="nginx-key-path" value="${escapeHtml(defaultKeyPath)}" oninput="updateNginxConfigPreview(); this.dataset.userEdited='1'" class="input" style="font-size:.78rem;">
                  </div>
                </div>
              </div>
            </div>

            <div style="margin-bottom:.7rem;">
              <label class="form-label">nginx 配置(可复制)</label>
              <textarea id="nginx-config-preview" class="input mono nginx-preview-box" style="font-size:.78rem;line-height:1.55;">${escapeHtml(initialConfig)}</textarea>
            </div>

            <div style="display:flex;gap:.6rem;">
              <button onclick="applyNginxConfig()" class="btn-primary" style="flex:1;font-size:.85rem;" id="nginx-apply-btn">⚡ 一键配置</button>
              <button onclick="copyNginxConfig()" class="btn-ghost" style="flex:1;font-size:.85rem;">复制配置</button>
            </div>
            <div id="nginx-apply-result" class="hidden" style="margin-top:.6rem;padding:.7rem .9rem;border-radius:.7rem;background:#f6f7ff;border:1px solid var(--border);font-size:.78rem;font-family:ui-monospace,monospace;white-space:pre-wrap;"></div>
          </div>
        </details>

        <!-- 步骤 2: 部署 -->
        <details class="form-sec" style="margin-bottom:.6rem;">
          <summary class="form-sec-title">② 部署到 frps 主机</summary>
          <div style="padding-top:.6rem;">
            <pre class="code-block" style="margin:0;font-size:.76rem;">
# 1. 点击上方「⚡ 一键配置」自动完成:
写入 /etc/nginx/conf.d/${escapeHtml((proxy.name || 'proxy').toLowerCase().replace(/[^a-z0-9._-]/g, '_'))}.conf
# 2. 自动执行:
nginx -t(校验失败自动回滚,不会写入)
# 3. 校验通过后自动:
kill -HUP nginx master(重载新配置)
# 4. 手动验证(证书应与域名匹配)
curl -I https://${escapeHtml(domains[0] || 'example.com')}</pre>
          </div>
        </details>
      </div>
    </div>
  `;
  // 显示模态框(修复:之前忘了移除 hidden,导致内容渲染了但页面空白)
  document.getElementById('modal-container').classList.remove('hidden');
  // 加载宿主 conf.d 模板列表(供「复用已有模板」下拉)
  loadNginxTemplates();
}

function generateNginxConfigLive() {
  const { proxy } = nginxGuideState;
  if (!proxy) return '';
  const ssl = document.getElementById('nginx-ssl-toggle')?.checked ?? true;
  const certPath = document.getElementById('nginx-cert-path')?.value || '';
  const keyPath = document.getElementById('nginx-key-path')?.value || '';
  const domains = proxy.custom_domains || [];

  // 若已选择「复用已有模板」,用模板内容 + 替换域名/端口
  const tplSel = document.getElementById('nginx-template-select');
  const tplContent = nginxGuideState.selectedTemplateContent;
  if (tplSel && tplSel.value && tplContent) {
    return applyTemplateToConfig(tplContent);
  }

  // frp 链路架构: nginx -> frps 的 vhostHTTPPort(按域名路由) -> frpc -> 后端服务
  // vhostHTTPPort 从 frps 实例配置里取;取不到就不生成配置
  const vhostPort = nginxGuideState.vhostPort;
  if (!vhostPort) return '';

  let cfg = `# 由 frpm 生成 - ${new Date().toLocaleString()}\n`;
  cfg += `# 架构: 浏览器 -> nginx(${ssl?'TLS终结':'HTTP转发'}) -> frps vhostHTTPPort:${vhostPort} -> frpc -> ${frpsProxyBackendText(proxy)}\n`;
  cfg += `# 域名: ${domains.join(', ')}\n\n`;
  cfg += `map $http_upgrade $connection_upgrade {\n    default upgrade;\n    '' close;\n}\n\n`;
  if (ssl) {
    // HTTPS 启用:先放一个 80 端口 server 块,强制 301 跳转到 https
    cfg += `# --- HTTP(80) -> HTTPS(443) 强制跳转 ---\n`;
    cfg += `server {\n`;
    cfg += `    listen 80;\n`;
    cfg += `    server_name ${domains.join(' ')};\n`;
    cfg += `    # 启用 HTTPS 后,HTTP 请求一律 301 跳转到 HTTPS\n`;
    cfg += `    return 301 https://$host$request_uri;\n`;
    cfg += `}\n\n`;
  }
  cfg += `server {\n`;
  if (ssl) {
    cfg += `    listen 443 ssl;\n`;
    cfg += `    server_name ${domains.join(' ')};\n\n`;
    cfg += `    ssl_certificate     ${certPath || '/etc/nginx/certs/service.example.crt'};\n`;
    cfg += `    ssl_certificate_key ${keyPath || '/etc/nginx/certs/service.example.key'};\n`;
    cfg += `    ssl_protocols TLSv1.2 TLSv1.3;\n    ssl_ciphers HIGH:!aNULL:!MD5;\n\n`;
  } else {
    cfg += `    listen 80;\n`;
    cfg += `    server_name ${domains.join(' ')};\n\n`;
    cfg += `    # HTTPS 未启用,如需请修改证书路径后勾选"启用 HTTPS"\n\n`;
  }
  cfg += `    location / {\n`;
  cfg += `        # frps 的 vhostHTTPPort,按 Host 头(域名)路由到 frpc 代理\n`;
  cfg += `        proxy_pass http://127.0.0.1:${vhostPort};\n`;
  cfg += `        proxy_set_header Host $host;\n`;
  cfg += `        proxy_set_header X-Real-IP $remote_addr;\n`;
  cfg += `        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n`;
  cfg += `        proxy_set_header X-Forwarded-Proto $scheme;\n`;
  cfg += `        proxy_http_version 1.1;\n`;
  cfg += `        proxy_set_header Upgrade $http_upgrade;\n`;
  cfg += `        proxy_set_header Connection $connection_upgrade;\n`;
  cfg += `        proxy_read_timeout 3600s;\n`;
  cfg += `        proxy_send_timeout 3600s;\n`;
  cfg += `        proxy_buffering off;\n`;
  cfg += `    }\n`;
  cfg += `}\n`;
  return cfg;
}

function updateNginxConfigPreview() {
  // 模板模式:SSL 开关/证书字段无意义,禁用并提示
  const tplSel = document.getElementById('nginx-template-select');
  const tplActive = !!(tplSel && tplSel.value && nginxGuideState.selectedTemplateContent);
  const sslToggle = document.getElementById('nginx-ssl-toggle');
  const certFields = document.getElementById('nginx-cert-fields');
  const sslChecked = sslToggle?.checked;
  if (sslToggle) sslToggle.disabled = tplActive;
  if (certFields) {
    certFields.style.display = (tplActive || !sslChecked) ? 'none' : 'grid';
    certFields.style.opacity = tplActive ? '0.5' : '1';
    // 模板模式:证书输入框禁用
    for (const inp of certFields.querySelectorAll('input')) {
      inp.disabled = tplActive;
      inp.title = tplActive ? '模板模式:证书路径由所选模板决定' : '';
    }
  }
  // 更新配置预览
  const preview = document.getElementById('nginx-config-preview');
  if (preview) preview.value = generateNginxConfigLive();
}

// 加载 frps 宿主 conf.d 模板列表(填充下拉)
async function loadNginxTemplates() {
  const sel = document.getElementById('nginx-template-select');
  if (!sel) return;
  // 记住当前选择
  const prev = sel.value;
  try {
    const res = await api('/api/nginx/templates');
    const tpls = (res && res.templates) || [];
    // 预填宿主真实证书路径(通用配置模式):仅当证书框还是初始占位值时覆盖
    if (res && res.default_cert) {
      const cInput = document.getElementById('nginx-cert-path');
      const kInput = document.getElementById('nginx-key-path');
      // 初始占位值特征:/etc/ssl/frpm/(旧) 或含 /certs/service. 前缀(新占位,由 renderNginxGuideModal 生成)
      if (cInput && !cInput.dataset.userEdited &&
          (cInput.value.includes('/etc/ssl/frpm/') || cInput.value.includes('/certs/service.'))) {
        cInput.value = res.default_cert;
        if (kInput) kInput.value = res.default_key || (res.default_cert ? res.default_cert.slice(0, -4) + '.key' : '');
      }
    }
    sel.innerHTML = '<option value="">— 不使用模板(用下方通用配置) —</option>';
    for (const t of tpls) {
      const opt = document.createElement('option');
      opt.value = t.filename;
      opt.textContent = t.usable
        ? `${t.filename} (${t.size}B)${t.is_default ? ' ⭐默认' : ''}`
        : `⛔ ${t.filename} (不可复用: ${t.reason || '不适用'})`;
      opt.title = t.summary || '';
      if (!t.usable) opt.disabled = true;
      sel.appendChild(opt);
    }
    if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
  } catch (e) {
    toast('加载模板列表失败: ' + ((e && e.message) || e), 'error');
  }
}

// 用户选中模板后,读取完整内容供替换
let nginxTplReqSeq = 0;  // 请求序号:防止快速切换模板时旧响应覆盖新选择
async function onNginxTemplateSelect() {
  const sel = document.getElementById('nginx-template-select');
  const sumBox = document.getElementById('nginx-tpl-sum');
  if (!sel || !sumBox) return;
  const filename = sel.value;
  const reqSeq = ++nginxTplReqSeq;
  nginxGuideState.selectedTemplateContent = null;
  if (!filename) {
    sumBox.classList.add('hidden');
    sumBox.textContent = '';
  } else {
    sumBox.classList.remove('hidden');
    sumBox.textContent = '⏳ 读取模板...';
    try {
      const res = await api('/api/nginx/templates?filename=' + encodeURIComponent(filename));
      // 若期间用户又切换了模板/清空,丢弃本次结果
      if (reqSeq !== nginxTplReqSeq || sel.value !== filename) {
        updateNginxConfigPreview();
        return;
      }
      if (res && res.content) {
        nginxGuideState.selectedTemplateContent = res.content;
        sumBox.textContent = `✅ 已加载 ${filename},将自动用 ${(nginxGuideState.proxy?.custom_domains || []).join(' ')} 替换 server_name、用 ${nginxGuideState.vhostPort||'?'} 替换反代端口。可改下方预览后应用。`;
        sumBox.style.color = '#059669';
      } else {
        sumBox.textContent = '❌ 读取失败';
        sumBox.style.color = '#dc2626';
      }
    } catch (e) {
      // 竞态丢弃:切换后旧请求报错不再显示
      if (reqSeq !== nginxTplReqSeq) {
        updateNginxConfigPreview();
        return;
      }
      sumBox.textContent = '❌ 读取失败: ' + ((e && e.message) || e);
      sumBox.style.color = '#dc2626';
    }
  }
  updateNginxConfigPreview();
}

// 把模板内容套用到当前代理:替换 server_name 与 proxy_pass 端口
function applyTemplateToConfig(tplContent) {
  const { proxy } = nginxGuideState;
  const domains = (proxy.custom_domains || []).filter(Boolean);
  const domain = domains[0] || '';
  const vhostPort = nginxGuideState.vhostPort || '2006';
  let cfg = tplContent;
  // 1. 提取模板原 server_name 里的旧域名(用于清理注释残留)
  const oldMatch = cfg.match(/server_name\s+([a-zA-Z0-9.-]+\.[a-z]{2,})/);
  const oldDomain = oldMatch ? oldMatch[1] : null;
  // 2. 替换所有 server_name(模板常有 80 与 443 两个 server 块,都要改)
  cfg = cfg.replace(/server_name\s+[^;]+;/g, `server_name ${domain};`);
  // 3. 替换 proxy_pass 端口(指向 frps vhostHTTPPort)。匹配 127.0.0.1:数字 或 |localhost:数字
  cfg = cfg.replace(/proxy_pass\s+http:\/\/(127\.0\.0\.1|localhost|\[::1\]):\d+/g, `proxy_pass http://127.0.0.1:${vhostPort}`);
  // 4. 若识别到旧域名且不等于新域名,把注释行里的旧域名一并替换(避免误导)
  if (oldDomain && oldDomain !== domain) {
    cfg = cfg.split(oldDomain).join(domain);
  }
  return cfg;
}

function copyNginxConfig() {
  // 所见即所得:复制预览框当前内容(含用户手动微调)
  const ta = document.getElementById('nginx-config-preview');
  const cfg = (ta && ta.value) ? ta.value : generateNginxConfigLive();
  if (!cfg) {
    toast('配置为空,无法复制', 'error');
    return;
  }
  const doCopy = () => {
    if (ta) {
      ta.focus();
      ta.select();
      document.execCommand('copy');
    }
    toast('已复制到剪贴板', 'success');
  };
  // 优先用异步剪贴板 API;不可用或失败时回退 textarea 选中
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(cfg).then(() => toast('已复制到剪贴板', 'success')).catch(doCopy);
  } else {
    doCopy();
  }
}

// 一键写入 nginx 配置到 frps 宿主机并 reload
let nginxForceOverwrite = false;
async function applyNginxConfig() {
  // 所见即所得:以预览框当前内容为准(用户可手动微调)
  const preview = document.getElementById('nginx-config-preview');
  const cfg = (preview && preview.value) ? preview.value : generateNginxConfigLive();
  if (!cfg) {
    toast('配置为空,请先确认 frps 已配置 vhostHTTPPort', 'error');
    return;
  }
  const { proxy } = nginxGuideState;
  const filename = `${(proxy.name || 'proxy').toLowerCase().replace(/[^a-z0-9._-]/g, '_')}.conf`;
  const btn = document.getElementById('nginx-apply-btn');
  const resultBox = document.getElementById('nginx-apply-result');
  if (!btn || !resultBox) return;
  btn.disabled = true;
  btn.textContent = '⏳ 写入中...';
  resultBox.classList.remove('hidden');
  resultBox.style.color = 'var(--muted)';
  resultBox.textContent = '正在写入配置 + nginx -t + reload...';
  try {
    const res = await api('/api/nginx/apply', {
      method: 'POST',
      body: JSON.stringify({ filename, config: cfg, overwrite: nginxForceOverwrite }),
    });
    // 409:目标文件已存在,询问是否覆盖
    if (res && res.exists && !nginxForceOverwrite) {
      nginxForceOverwrite = true;
      btn.disabled = false;
      btn.textContent = '⚠️ 确认覆盖?';
      resultBox.style.color = 'var(--warn-fg)';
      resultBox.textContent = `⚠️ 目标文件 ${filename} 已存在,再次点击将覆盖它(会保留原文件至应用成功,失败自动回滚)。`;
      toast(`文件 ${filename} 已存在,再点一次确认覆盖`, 'warning');
      return;
    }
    if (res && res.success) {
      nginxForceOverwrite = false;
      btn.textContent = '✅ 已应用';
      resultBox.style.color = '#059669';
      resultBox.textContent = (res.output || '').trim() || res.message || '配置已应用';
      toast(`nginx 配置已应用: ${filename}`, 'success');
    } else {
      btn.textContent = '⚡ 一键配置';
      resultBox.style.color = '#dc2626';
      resultBox.textContent = (res && res.output ? res.output : (res && res.message) || '应用失败') || '应用失败';
      const failedMsg = (res && res.message) || '';
      // 超时/未知状态不能断言"已回滚",按后端消息提示
      toast(failedMsg.includes('超时') ? 'nginx 配置操作超时,请到宿主检查' : 'nginx 配置应用失败(已回滚)', 'error');
    }
  } catch (e) {
    btn.textContent = '⚡ 一键配置';
    resultBox.style.color = '#dc2626';
    resultBox.textContent = `请求失败: ${(e && e.message) || e}`;
    toast('nginx 配置应用失败', 'error');
  } finally {
    setTimeout(() => {
      if (!nginxForceOverwrite) { btn.disabled = false; btn.textContent = '⚡ 一键配置'; }
    }, 3000);
  }
}
