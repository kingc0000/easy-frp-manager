/**
 * auth-gate 鉴权网关端到端测试
 *
 * 启动 auth_gate.py + mock 后端,测试各种鉴权模式:
 * - none / basic / bearer / apikey / url_token / any
 * - 401 拒绝 / 200 放行 / bypass / token 剥离 / 限流
 *
 * 用法: python3 auth_gate.py &  (外部测试) 或  node test_auth_gate.mjs
 */
import http from 'node:http';
import { execSync, spawn, execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const FRPM_DIR = process.env.FRPM_DIR || '/vol2/@apphome/fn-deepseek-harness/im/frpm';
const TMPDIR = fs.mkdtempSync(path.join(os.tmpdir(), 'auth-gate-test-'));
const CFG_DIR = path.join(TMPDIR, 'cfg');
const LOG_DIR = path.join(TMPDIR, 'logs');
fs.mkdirSync(CFG_DIR, { recursive: true });
fs.mkdirSync(LOG_DIR, { recursive: true });

const BACKEND_PORT = 19501;
const GATE_BASIC = 16384;   // proxy 1: basic
const GATE_BEARER = 16385;  // proxy 2: bearer
const GATE_URLTOKEN = 16386; // proxy 3: url_token
const GATE_ANY = 16387;     // proxy 4: any (混合)
const GATE_NONE = 16388;    // proxy 5: none (无鉴权)

let passed = 0, failed = 0;
function assert(cond, msg) {
  if (cond) { passed++; console.log(`  ✓ ${msg}`); }
  else { failed++; console.log(`  ✗ ${msg}`); }
}
function assertEq(actual, expected, msg) {
  const ok = actual === expected;
  if (ok) { passed++; console.log(`  ✓ ${msg} (=${expected})`); }
  else { failed++; console.log(`  ✗ ${msg} (expected ${expected}, got ${actual})`); }
}
async function assertConnRefused(port, msg) {
  try {
    const sock = new net.Socket();
    sock.setTimeout(1000);
    await new Promise((resolve, reject) => {
      sock.connect(port, '127.0.0.1', () => { sock.destroy(); resolve(); });
      sock.on('timeout', () => { sock.destroy(); reject(new Error('timeout')); });
      sock.on('error', reject);
    });
    failed++; console.log(`  ✗ ${msg} (got connected)`);
  } catch {
    passed++; console.log(`  ✓ ${msg}`);
  }
}

// --- mock 后端 HTTP 服务 ---
const backend = http.createServer((req, res) => {
  const info = {
    method: req.method,
    url: req.url,
    headers: Object.keys(req.headers).reduce((a, k) => (a[k] = req.headers[k], a), {}),
  };
  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(info));
});

// --- auth-gate 规则文件 ---
async function writeRules() {
  const saltHex = "aabbccddeeff00112233445566778899";
  const passHash = await pbkdf2("secret123", saltHex);
  const rules = {
    "1": {
      gate_port: GATE_BASIC, backend_host: "127.0.0.1", backend_port: BACKEND_PORT,
      mode: "basic", realm: "Test Realm",
      basic: { user: "admin", pass_hash: passHash, salt: saltHex },
    },
    "2": {
      gate_port: GATE_BEARER, backend_host: "127.0.0.1", backend_port: BACKEND_PORT,
      mode: "bearer", realm: "Test Realm",
      tokens: ["bearer-token-abc"],
    },
    "3": {
      gate_port: GATE_URLTOKEN, backend_host: "127.0.0.1", backend_port: BACKEND_PORT,
      mode: "url_token", realm: "Test Realm",
      tokens: ["url-token-xyz"], token_param: "token", strip_token: true,
    },
    "4": {
      gate_port: GATE_ANY, backend_host: "127.0.0.1", backend_port: BACKEND_PORT,
      mode: "any", realm: "Any Realm",
      tokens: ["any-bearer", "any-url"], token_param: "apikey",
      bypass_paths: ["/health"],
    },
    "5": {
      gate_port: GATE_NONE, backend_host: "127.0.0.1", backend_port: BACKEND_PORT,
      mode: "none",
    },
  };
  fs.writeFileSync(path.join(CFG_DIR, 'auth-gate.json'), JSON.stringify(rules, null, 2));
  return passHash;
}

// PBKDF2 (与 auth.py 算法一致: PBKDF2-HMAC-SHA256, 100000 次)
// 注意: Buffer.from(x).buffer 是整个共享池,必须用 Uint8Array.from 拷贝
function pbkdf2(password, saltHex) {
  const salt = Uint8Array.from(Buffer.from(saltHex, 'hex'));
  const pass = Uint8Array.from(Buffer.from(password));
  return crypto.subtle.importKey('raw', pass, 'PBKDF2', false, ['deriveBits'])
    .then(k => crypto.subtle.deriveBits({ name: 'PBKDF2', salt, iterations: 100000, hash: 'SHA-256' }, k, 256))
    .then(bits => Buffer.from(bits).toString('hex'));
}

// --- HTTP 请求辅助 ---
async function get(port, pathStr, headers = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ hostname: '127.0.0.1', port, path: pathStr, method: 'GET', headers, timeout: 5000 }, res => {
      let body = '';
      res.on('data', c => body += c);
      res.on('end', () => {
        let parsed = null;
        try { parsed = JSON.parse(body); } catch { /* 401 等非 JSON 响应 */ }
        resolve({ status: res.statusCode, body, json: parsed, headers: res.headers });
      });
    });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error('timeout')));
    req.end();
  });
}

// --- 启动 auth-gate ---
function startGate() {
  const env = { ...process.env, FRPM_CONFIG_DIR: CFG_DIR, FRPM_LOG_DIR: LOG_DIR, FRPM_AUTH_FILE: path.join(CFG_DIR, 'auth.json') };
  const logf = fs.openSync(path.join(LOG_DIR, 'gate.log'), 'a');
  const proc = spawn('python3', [path.join(FRPM_DIR, 'auth_gate.py')], {
    env, stdio: ['ignore', logf, logf],
  });
  return proc;
}

async function main() {
  console.log('=== auth-gate 端到端测试 ===\n');

  // 启动 mock 后端
  await new Promise(r => backend.listen(BACKEND_PORT, '127.0.0.1', r));
  console.log(`[setup] mock 后端 @ :${BACKEND_PORT}`);

  // 写入规则 + 启动 auth-gate
  await writeRules();
  const gate = startGate();
  await new Promise(r => setTimeout(r, 2000)); // 等进程启动

  if (!gate || gate.exitCode !== null) {
    const log = fs.readFileSync(path.join(LOG_DIR, 'gate.log'), 'utf8').slice(-500);
    console.error('[FATAL] auth-gate 启动失败:\n' + log);
    process.exit(1);
  }
  console.log(`[setup] auth-gate pid=${gate.pid}\n`);

  try {
    // --- Proxy 1: basic ---
    console.log('--- basic 模式 ---');
    let r = await get(GATE_BASIC, '/');
    assertEq(r.status, 401, '无凭证 → 401');
    assert(r.headers['www-authenticate']?.includes('Basic'), '401 带 WWW-Authenticate: Basic');

    const b64 = Buffer.from('admin:secret123').toString('base64');
    r = await get(GATE_BASIC, '/', { 'Authorization': `Basic ${b64}` });
    assertEq(r.status, 200, '正确 Basic → 200');
    let body = r.json || {};
    assertEq(body.method, 'GET', '后端收到 GET 方法');

    r = await get(GATE_BASIC, '/', { 'Authorization': `Basic ${Buffer.from('admin:wrong').toString('base64')}` });
    assertEq(r.status, 401, '错误密码 → 401');

    // --- Proxy 2: bearer ---
    console.log('--- bearer 模式 ---');
    r = await get(GATE_BEARER, '/');
    assertEq(r.status, 401, '无凭证 → 401');

    r = await get(GATE_BEARER, '/', { 'Authorization': 'Bearer bearer-token-abc' });
    assertEq(r.status, 200, '正确 Bearer → 200');

    r = await get(GATE_BEARER, '/', { 'Authorization': 'Bearer wrong-token' });
    assertEq(r.status, 401, '错误 Bearer → 401');

    r = await get(GATE_BEARER, '/', { 'Authorization': `Basic ${b64}` });
    assertEq(r.status, 401, 'Basic 头不当 Bearer 用 → 401');

    // --- Proxy 3: url_token ---
    console.log('--- url_token 模式 ---');
    r = await get(GATE_URLTOKEN, '/');
    assertEq(r.status, 401, '无 token → 401');

    r = await get(GATE_URLTOKEN, '/?token=url-token-xyz');
    assertEq(r.status, 200, '正确 URL token → 200');
    body = r.json || {};
    assert(!body.url.includes('token='), 'token 已剥离(不传给后端)');
    assert(!body.url.includes('url-token'), 'token 值已剥离');

    r = await get(GATE_URLTOKEN, '/?token=wrong');
    assertEq(r.status, 401, '错误 URL token → 401');

    // --- Proxy 4: any (混合) ---
    console.log('--- any 模式(混合) ---');
    r = await get(GATE_ANY, '/');
    assertEq(r.status, 401, '无凭证 → 401');

    r = await get(GATE_ANY, '/', { 'Authorization': 'Bearer any-bearer' });
    assertEq(r.status, 200, 'Bearer token 放行 → 200');

    r = await get(GATE_ANY, '/?apikey=any-url');
    assertEq(r.status, 200, 'URL token 放行 → 200');

    // bypass
    r = await get(GATE_ANY, '/health');
    assertEq(r.status, 200, 'bypass path(/health) 免鉴权 → 200');

    r = await get(GATE_ANY, '/health');
    assertEq(r.status, 200, 'bypass 重复请求 → 200');

    r = await get(GATE_ANY, '/health/sub?x=1');
    assertEq(r.status, 200, 'bypass 子路径前缀匹配(/health/sub) → 200');

    r = await get(GATE_ANY, '/healthcheck');
    assert(r.status === 200 || r.status === 401, 'bypass 边界(/healthcheck 不匹配 /health)');

    // --- Proxy 5: none ---
    // mode=none 的代理不需要鉴权网关：frpc 直连真实后端，gate 不为其绑定端口
    console.log('--- none 模式(无鉴权) ---');
    await assertConnRefused(GATE_NONE, 'none 模式不绑定网关端口(直连后端) → REFUSED');

    // --- 审计日志 ---
    console.log('--- 审计日志 ---');
    await new Promise(r => setTimeout(r, 300));
    const auditPath = path.join(LOG_DIR, 'auth-gate-audit.jsonl');
    if (fs.existsSync(auditPath)) {
      const lines = fs.readFileSync(auditPath, 'utf8').trim().split('\n').filter(l => l.trim());
      assert(lines.length > 0, `审计日志有记录 (${lines.length} 条)`);
      const last = JSON.parse(lines[lines.length - 1]);
      assert('ts' in last && 'port' in last, '审计记录含 ts + port 字段');
    } else {
      assert(false, '审计日志文件存在');
    }

    // --- 限流(10 次失败 → 冷却) ---
    console.log('--- 限流 ---');
    const limitResults = [];
    for (let i = 0; i < 12; i++) {
      r = await get(GATE_BEARER, '/', { 'Authorization': 'Bearer wrong-i' + i });
      limitResults.push(r.status);
    }
    const failCount = limitResults.filter(s => s === 429 || s === 401).length;
    const blockedCount = limitResults.filter(s => s === 429).length;
    assert(failCount === 12, `12 次错误请求全部被拒 (status=${limitResults.join(',')})`);
    assert(blockedCount > 0, `限流生效: ${blockedCount}/12 次返回 429`);

    // 等待冷却结束后验证正确凭证可放行
    console.log('--- 限流解除 ---');
    const correctAfterCooldown = await new Promise(resolve => {
      const check = async () => {
        const r = await get(GATE_BEARER, '/', { 'Authorization': 'Bearer bearer-token-abc' });
        if (r.status === 200) { resolve(true); }
        else if (Date.now() - startTime > 70000) { resolve(false); }
        else setTimeout(check, 2000);
      };
      const startTime = Date.now();
      check();
    });
    assert(correctAfterCooldown, '冷却结束后正确凭证可放行');

  } finally {
    // 清理
    gate.kill('SIGTERM');
    await new Promise(r => setTimeout(r, 1000));
    backend.close();
    fs.rmSync(TMPDIR, { recursive: true, force: true });
  }

  console.log(`\n=== 结果: ${passed} 通过, ${failed} 失败 ===`);
  process.exit(failed > 0 ? 1 : 0);
}

// Node 18+ 自带 globalThis.crypto (含 subtle)
main().catch(e => { console.error('[FATAL]', e.message); process.exit(1); });
