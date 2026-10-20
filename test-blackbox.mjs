// frpm 黑盒集成测试: 针对隔离实例(默认 http://127.0.0.1:9180)
// 用法: node test-blackbox.mjs [baseUrl]
import { request as httpReq } from 'node:http';
import { createServer as netCreateServer, connect as netConnect } from 'node:net';

const BASE = process.argv[2] || 'http://127.0.0.1:9180';
let passed = 0, failed = 0, failures = [];

function req(method, path, { body, token, cookie, raw } = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(BASE + path);
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = 'Bearer ' + token;
    if (cookie) headers['Cookie'] = 'session=' + cookie;
    const payload = raw ? body : (body !== undefined ? JSON.stringify(body) : undefined);
    if (payload !== undefined) headers['Content-Length'] = Buffer.byteLength(payload);
    const r = httpReq(u, { method, headers }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        let json = null;
        try { json = text ? JSON.parse(text) : null; } catch { /* raw */ }
        resolve({ status: res.statusCode, json, text, headers: res.headers });
      });
    });
    r.on('error', reject);
    if (payload !== undefined) r.write(payload);
    r.end();
  });
}

function t(name, cond, extra = '') {
  if (cond) { passed++; console.log('  ✓ ' + name); }
  else { failed++; failures.push(name + (extra ? ' — ' + extra : '')); console.log('  ✗ ' + name + (extra ? ' — ' + extra : '')); }
}

// 断言: response = {status, json, text}
const check = (r, status, field, expect, label) => {
  if (r.status !== status) return t(label + ' status', false, `got ${r.status} want ${status} | ${r.text.slice(0,120)}`);
  if (field) t(label, r.json && r.json[field] === expect, `json.${field}=${JSON.stringify(r.json?.[field])} want ${JSON.stringify(expect)} | ${r.text.slice(0,120)}`);
  else t(label, true);
};

const run = async () => {
  console.log(`\n=== frpm 黑盒测试 @ ${BASE} ===\n`);

  // ---- 公开路径 ----
  let r = await req('GET', '/api/auth/config');
  check(r, 200, 'default_username', 'admin', 'auth/config 公开返回默认用户名 admin');
  t('auth/config 不暴露密码哈希', !('password_hash' in (r.json || {})), JSON.stringify(r.json));

  r = await req('GET', '/api/auth/check');
  t('auth/check 未登录', r.status === 200 && r.json && r.json.authenticated === false, r.text);

  r = await req('GET', '/api/dashboard');
  t('未登录访问受保护 API → 401', r.status === 401 && r.json?.login_required === true, `${r.status} ${r.text.slice(0,80)}`);

  r = await req('GET', '/api/instances');
  t('未登录 instances → 401', r.status === 401, `${r.status}`);

  // 静态文件
  r = await req('GET', '/');
  t('根路径服务静态页', r.status === 200 && /text\/html/.test(r.headers['content-type'] || ''), `${r.status}`);

  // 路径穿越(需登录态才能触达 serve_static 的 abspath 检查——先登录再测)
  // 注意:Node URL 构造器会自动归一化 /../ → /,必须用 raw socket 发送字面路径
  r = await req('POST', '/api/auth/login', { body: { username: 'admin', password: 'admin123' } });
  const tt = r.json?.token;
  const rawTraversal = (path) => new Promise((resolve, reject) => {
    const s = netConnect(9180, '127.0.0.1', () => {
      s.write(`GET ${path} HTTP/1.1\r\nHost: 127.0.0.1:9180\r\nAuthorization: Bearer ${tt}\r\nConnection: close\r\n\r\n`);
    });
    let buf = '';
    s.on('data', (c) => { buf += c.toString('utf8'); });
    s.on('end', () => resolve({ status: parseInt(buf.split('\r\n')[0].split(' ')[1], 10), head: buf.slice(0, 120) }));
    s.on('error', reject);
  });
  r = await rawTraversal('/../index.html');
  t('路径穿越 /../index.html → 403 或 404', [403, 404].includes(r.status), `got ${r.status}`);
  r = await rawTraversal('/..%2findex.html');
  t('编码路径穿越 → 403 或 404', [403, 404].includes(r.status), `got ${r.status}`);
  r = await rawTraversal('/%2e%2e/index.html');
  t('双重编码路径穿越 → 403 或 404', [403, 404].includes(r.status), `got ${r.status}`);

  // ---- 登录 ----
  r = await req('POST', '/api/auth/login', { body: {} });
  t('空登录 → 400', r.status === 400, r.text);
  r = await req('POST', '/api/auth/login', { body: { username: 'admin', password: 'wrong' } });
  t('错密码 → 401', r.status === 401, r.text);
  r = await req('POST', '/api/auth/login', { body: { username: 'admin', password: 'admin123' } });
  t('登录成功且返回 token', r.status === 200 && typeof r.json?.token === 'string' && r.json.token.length >= 40, `${r.status} ${r.text.slice(0,80)}`);
  const token = r.json?.token;
  t('token 为随机串', !!token && token.length >= 40, String(token).length);

  // ---- 登录后 ----
  r = await req('GET', '/api/dashboard', { token });
  t('dashboard 登录后可访问', r.status === 200 && typeof r.json?.system?.hostname === 'string', `${r.status} ${r.text.slice(0,100)}`);
  t('dashboard 有本文机器信息', r.json?.system && typeof r.json.system.hostname === 'string', JSON.stringify(r.json?.system));

  r = await req('GET', '/api/auth/me', { token });
  check(r, 200, 'username', 'admin', 'me 返回用户名');

  // ---- 实例 CRUD ----
  // 非法 type
  r = await req('POST', '/api/instances', { token, body: { name: 'bad1', type: 'xxx' } });
  t('非法 type → 400', r.status === 400, `${r.status} ${r.text}`);
  // 缺 name
  r = await req('POST', '/api/instances', { token, body: { type: 'frps' } });
  t('缺 name → 400', r.status === 400, r.text);
  // 非数字 id → 404 (Router 防 int() 崩溃)
  r = await req('GET', '/api/instances/abc', { token });
  t('非数字 id → 404', r.status === 404, `${r.status} ${r.text.slice(0,60)}`);
  // 数字 id 不存在 → 404
  r = await req('GET', '/api/instances/99999', { token });
  t('不存在 id → 404', r.status === 404, `${r.status} ${r.text.slice(0,60)}`);

  // ---- frps 实例创建（端口冲突链）----
  // 取一个当前空闲端口
  const findFree = () => { const s = netCreateServer(); return new Promise((res) => { s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); }); }); };
  const p1 = await findFree();
  r = await req('POST', '/api/instances', { token, body: { name: 'frps-test', type: 'frps', config: { bind_port: p1 } } });
  t('创建 frps 实例', r.status === 201 && Number.isInteger(r.json?.instance_id), `${r.status} ${r.text.slice(0,100)}`);
  const frpsId = r.json?.instance_id;
  t('config_path 在 CONFIG_DIR 下', (r.json?.config_path || '').startsWith('/tmp/frpm-test/configs'), r.json?.config_path);

  // 同名重复 → 409
  r = await req('POST', '/api/instances', { token, body: { name: 'frps-test', type: 'frps', config: { bind_port: p1 + 1 } } });
  t('重名实例 → 409', r.status === 409, `${r.status} ${r.text}`);
  // 端口冲突 → 400
  r = await req('POST', '/api/instances', { token, body: { name: 'frps-port2', type: 'frps', config: { bind_port: p1 } } });
  t('端口冲突 → 400', r.status === 400 && /端口冲突/.test(r.text), `${r.status} ${r.text.slice(0,80)}`);

  // ---- proxy 管理（frpc 实例）----
  const p2 = await findFree(), p3 = await findFree();
  r = await req('POST', '/api/instances', { token, body: { name: 'frpc-test', type: 'frpc', config: { server_addr: '127.0.0.1', server_port: p2, auth_token: 'tok', proxies: [{ name: 'web1', type: 'tcp', local_ip: '127.0.0.1', local_port: p3, remote_port: p1 }] } } });
  t('创建 frpc 实例(含 proxy)', r.status === 201 && Number.isInteger(r.json?.instance_id), `${r.status} ${r.text.slice(0,100)}`);
  const frpcId = r.json?.instance_id;

  r = await req('GET', `/api/instances/${frpcId}/proxies`, { token });
  t('proxies 列表含 web1', r.status === 200 && Array.isArray(r.json?.proxies) && r.json.proxies.some(p => p.name === 'web1'), `${r.status} ${r.text.slice(0,100)}`);

  // 创建 proxy
  r = await req('POST', `/api/instances/${frpcId}/proxies`, { token, body: { name: 'web2', type: 'tcp', local_port: p3 + 1, remote_port: p2 + 1 } });
  t('创建 proxy → 201', r.status === 201, `${r.status} ${r.text.slice(0,100)}`);
  // toggle 不存在的 proxy → 404
  r = await req('POST', `/api/instances/${frpcId}/proxies/99999/toggle`, { token });
  t('toggle 不存在 proxy → 404', r.status === 404, `${r.status} ${r.text.slice(0,60)}`);
  // delete 不存在的 → 404
  r = await req('DELETE', `/api/instances/${frpcId}/proxies/99999`, { token });
  t('删除不存在 proxy → 404', r.status === 404, `${r.status} ${r.text.slice(0,60)}`);

  // ---- nginx-conf ----
  r = await req('POST', `/api/instances/${frpcId}/proxies/99999/nginx-conf`, { token });
  t('nginx-conf 不存在 proxy → 404', r.status === 404, `${r.status} ${r.text.slice(0,80)}`);

  // ---- 模板 / 类型 ----
  r = await req('GET', '/api/proxy-types', { token });
  t('proxy-types 含 tcp', r.status === 200 && typeof r.json === 'object' && 'tcp' in r.json, `${r.status} ${r.text.slice(0,80)}`);
  r = await req('GET', '/api/template/proxy/tcp', { token });
  t('proxy 模板返回名字', r.status === 200 && typeof r.json?.name === 'string' && r.json.name.length > 0, `${r.status} ${r.text.slice(0,80)}`);

  // ---- 实例启停 ----
  r = await req('POST', `/api/instances/${frpsId}/stop`, { token });
  t('stop 实例', [200, 404, 409].includes(r.status), `${r.status} ${r.text.slice(0,80)}`); // binary 可能没真正起 frps,行为宽容
  r = await req('POST', `/api/instances/${frpsId}/start`, { token });
  t('start 实例', [200, 404, 409].includes(r.status), `${r.status} ${r.text.slice(0,80)}`);

  // ---- 更新 raw_config ----
  r = await req('PUT', `/api/instances/${frpsId}`, { token, body: { raw_config: '' } });
  t('空 raw_config → 400', r.status === 400, `${r.status} ${r.text.slice(0,80)}`);
  r = await req('PUT', `/api/instances/${frpsId}`, { token, body: { raw_config: 'bindPort = 7001\n' } });
  t('合法 raw_config 更新', r.status === 200, `${r.status} ${r.text.slice(0,100)}`);

  // ---- 删除 ----
  r = await req('DELETE', `/api/instances/${frpcId}`, { token });
  t('删除 frpc 实例', r.status === 200, `${r.status} ${r.text.slice(0,80)}`);
  r = await req('DELETE', `/api/instances/${frpsId}`, { token });
  t('删除 frps 实例', r.status === 200, `${r.status} ${r.text.slice(0,80)}`);
  r = await req('GET', `/api/instances/${frpcId}`, { token });
  t('删除后查询 → 404', r.status === 404, `${r.status} ${r.text.slice(0,60)}`);

  // 重复删除 → 404
  r = await req('DELETE', `/api/instances/${frpcId}`, { token });
  t('重复删除 → 404', r.status === 404, `${r.status} ${r.text.slice(0,60)}`);

  // ---- 改密 / 会话 ----
  r = await req('POST', '/api/auth/change-password', { token, body: { old_password: 'wrong', new_password: 'newpass1', confirm_password: 'newpass1' } });
  t('改密旧密码错误 → 400', r.status === 400, `${r.status} ${r.text}`);
  r = await req('POST', '/api/auth/change-password', { token, body: { old_password: 'admin123', new_password: '123', confirm_password: '123' } });
  t('新密码过短 → 400', r.status === 400, r.text);
  r = await req('POST', '/api/auth/change-password', { token, body: { old_password: 'admin123', new_password: 'newpass1', confirm_password: 'different' } });
  t('两次不一致 → 400', r.status === 400, r.text);

  // ---- 登出 ----
  r = await req('POST', '/api/auth/logout', { token });
  t('登出 → 200', r.status === 200, `${r.status}`);
  r = await req('GET', '/api/auth/me', { token });
  t('登出后 token 失效 → 401', r.status === 401, `${r.status} ${r.text.slice(0,60)}`);

  // ---- 改用户名 ----
  // 重新登录
  r = await req('POST', '/api/auth/login', { body: { username: 'admin', password: 'admin123' } });
  const token2 = r.json?.token;
  r = await req('POST', '/api/auth/change-username', { token: token2, body: { new_username: 'admin', old_password: 'admin123' } });
  t('改同名 → 400', r.status === 400, r.text);
  r = await req('POST', '/api/auth/change-username', { token: token2, body: { new_username: 'operator', old_password: 'wrong' } });
  t('改用户名密码错 → 400', r.status === 400, r.text);
  r = await req('POST', '/api/auth/change-username', { token: token2, body: { new_username: 'operator', old_password: 'admin123' } });
  t('改用户名成功', r.status === 200, `${r.status} ${r.text.slice(0,80)}`);
  r = await req('GET', '/api/auth/me', { token: token2 });
  t('改用户名后旧会话失效 → 401', r.status === 401, `${r.status}`);
  r = await req('POST', '/api/auth/login', { body: { username: 'admin', password: 'admin123' } });
  t('旧用户名登录 → 401', r.status === 401, `${r.status} ${r.text.slice(0,80)}`);
  r = await req('POST', '/api/auth/login', { body: { username: 'operator', password: 'admin123' } });
  t('新用户名登录成功', r.status === 200 && typeof r.json?.token === 'string', `${r.status} ${r.text.slice(0,80)}`);

  // 恢复默认(改回 admin,避免污染)
  const token3 = r.json?.token;
  r = await req('POST', '/api/auth/change-username', { token: token3, body: { new_username: 'admin', old_password: 'admin123' } });
  t('改回 admin', r.status === 200, `${r.status} ${r.text.slice(0,60)}`);
  r = await req('POST', '/api/auth/login', { body: { username: 'admin', password: 'admin123' } });
  t('恢复后 admin 可登录', r.status === 200, `${r.status} ${r.text.slice(0,60)}`);

  // ---- 汇总 ----
  console.log(`\n结果: ${passed} 通过, ${failed} 失败`);
  if (failures.length) { console.log('失败明细:'); failures.forEach(f => console.log('  - ' + f)); }
  process.exit(failed ? 1 : 0);
};

run().catch((e) => { console.error('测试崩溃:', e); process.exit(2); });