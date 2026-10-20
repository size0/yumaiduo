// 本地验收：自注册闭环 + 事件转发（不依赖真实平台）
//
// 判据（核心是第 3 条，那正是原来的 bug）：
//   1. 插件启动后会去自注册
//   2. 它接受了注册返回的 webhookSecret（用旧 secret 签的请求要被拒、用新的要被收）
//   3. ★★★ 它拿着【注册返回的新令牌】去调平台 API（而不是 env 里那把旧的）
//      —— 旧代码把 data.token 丢掉了，所以它永远用 env 令牌。
//   4. 注册返回的新 secret 会写到交接文件（我们的后端靠它验签）
//   5. 事件转发：平台 POST webhook → 插件验签 → 转发到后端
//
// 用法：node test/acceptance.mjs
import { createServer } from 'node:http';
import { createHmac } from 'node:crypto';
import { spawn } from 'node:child_process';
import { existsSync, readFileSync, rmSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const pluginDir = join(here, '..');

const SECRET_FROM_REGISTER = 'secret-from-register-' + 'a'.repeat(40);
const STALE_ENV_TOKEN = 'yp_stale_env_token_should_never_be_used';
const FRESH_TOKEN = 'yp_fresh_token_from_register_abcdef0123456789';

let pass = 0;
let fail = 0;
function check(label, ok, detail = '') {
  if (ok) { pass++; console.log(`[PASS] ${label}`); }
  else { fail++; console.log(`[FAIL] ${label}  ${detail}`); }
}

// ───────────────────────── mock 平台 ─────────────────────────
const platformCalls = [];
let registerCalls = 0;

const platform = createServer(async (req, res) => {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const raw = Buffer.concat(chunks).toString('utf8');
  const url = new URL(req.url, 'http://x');

  if (url.pathname === '/api/v1/plugin/runtime/register') {
    registerCalls++;
    // 校验开发者令牌头（官方：x-plugin-developer-token）
    const dev = req.headers['x-plugin-developer-token'];
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({
      code: 0, message: 'ok',
      data: {
        pluginId: 'wanda-seat-autoquote',
        status: 'approved',
        // ★ 平台每次注册都重新颁发这两样
        webhookSecret: SECRET_FROM_REGISTER,
        token: FRESH_TOKEN,
      },
    }));
    console.log(`  [平台] register 被调用（第 ${registerCalls} 次）devToken=${dev ? 'yes' : 'NO'}`);
    return;
  }

  // 其它平台调用（插件 → 平台）
  platformCalls.push({ path: url.pathname, token: req.headers['x-plugin-token'] || '' });
  res.writeHead(200, { 'content-type': 'application/json' });
  res.end(JSON.stringify({ code: 0, message: 'ok', data: { ok: true } }));
});

// ───────────────────────── mock 我们的后端 ─────────────────────────
const backendEvents = [];
const backend = createServer(async (req, res) => {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const raw = Buffer.concat(chunks).toString('utf8');
  const ts = req.headers['x-yumaiduo-timestamp'];
  const sig = req.headers['x-yumaiduo-signature'];
  // 后端按它自己持有的 secret 验签
  const want = createHmac('sha256', SECRET_FROM_REGISTER)
    .update(`${ts}.${raw}`).digest('hex');
  const ok = want === sig;

  const url = new URL(req.url, 'http://x');
  if (url.pathname === '/api/v1/events') {
    backendEvents.push({ ok, body: JSON.parse(raw || '{}') });
    res.writeHead(ok ? 200 : 401, { 'content-type': 'application/json' });
    res.end(JSON.stringify(ok ? { ok: true } : { error: 'bad_sig' }));
    return;
  }
  // claim / run-due / recovery / result 一律空成功
  const empty = {
    '/api/v1/commands/claim': { commands: [] },
    '/api/v1/events/run-due': { ok: true, processed: 0, commands: 0, failed: [] },
    '/api/v1/recovery/inspect': { ok: true, commands: [], events: [], bookings: [], count: 0 },
  }[url.pathname] || { ok: true };
  res.writeHead(ok ? 200 : 401, { 'content-type': 'application/json' });
  res.end(JSON.stringify(empty));
});

// ───────────────────────── 启动 ─────────────────────────
const tmp = mkdtempSync(join(tmpdir(), 'taplug-'));
const secretFile = join(tmp, 'backend-secret.json');
const journalDir = join(tmp, 'journal');
const PLUGIN_PORT = 4711;
const BACKEND_PORT = 4712;

await new Promise((r) => platform.listen(0, '127.0.0.1', r));
const platformPort = platform.address().port;
await new Promise((r) => backend.listen(BACKEND_PORT, '127.0.0.1', r));

const env = {
  ...process.env,
  CORE_URL: `http://127.0.0.1:${platformPort}`,
  PLUGIN_DEVELOPER_TOKEN: 'pdk_mock_developer_token',
  BASE_URL: `http://127.0.0.1:${PLUGIN_PORT}`,
  PORT: String(PLUGIN_PORT),
  HOST: '127.0.0.1',
  YUMAIDUO_TENANT_ID: '107',
  YUMAIDUO_COMMAND_MODE: 'shadow',
  YUMAIDUO_AUTO_REGISTER: 'true',
  // ★ 故意给一把【过期的】env 令牌：注册成功后必须被换掉
  YUMAIDUO_PLUGIN_TOKEN: STALE_ENV_TOKEN,
  YUMAIDUO_WEBHOOK_SECRET: '',
  V5_BACKEND_URL: `http://127.0.0.1:${BACKEND_PORT}`,
  V5_BACKEND_SECRET_FILE: secretFile,
  PLUGIN_EXECUTION_JOURNAL_DIR: journalDir,
};

console.log('启动插件进程…');
const child = spawn(process.execPath, [join(pluginDir, 'src', 'server.mjs')], {
  env, cwd: pluginDir, stdio: ['ignore', 'pipe', 'pipe'],
});
let pluginLog = '';
child.stdout.on('data', (d) => { pluginLog += d; });
child.stderr.on('data', (d) => { pluginLog += d; });

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

try {
  // 等注册完成 + 就绪
  let hz = null;
  for (let i = 0; i < 40; i++) {
    await wait(300);
    try {
      const r = await fetch(`http://127.0.0.1:${PLUGIN_PORT}/healthz`);
      hz = await r.json();
      if (hz.registration && hz.registration.state !== 'PENDING') break;
    } catch { /* not up yet */ }
  }

  console.log('\n──── 1. 自注册 ────');
  check('插件启动后调用了 /api/v1/plugin/runtime/register', registerCalls >= 1,
        `calls=${registerCalls}`);
  check('/healthz 报告 registered=true', hz?.registered === true, JSON.stringify(hz));
  check('注册状态里能看出令牌来源是 register（不是 env）',
        hz?.registration?.tokenSource === 'register',
        JSON.stringify(hz?.registration));

  console.log('\n──── 2. 接受注册返回的 webhookSecret ────');
  // 用【旧/空】secret 签 → 应被拒
  const badTs = Date.now();
  const badBody = JSON.stringify({ event: 'im.message.received', id: 'bad', tenantId: '107', payload: {} });
  const badSig = createHmac('sha256', 'wrong-secret').update(`${badTs}.${badBody}`).digest('hex');
  const badRes = await fetch(`http://127.0.0.1:${PLUGIN_PORT}/__plugin__/webhook/im.message.received`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-yumaiduo-timestamp': String(badTs), 'x-yumaiduo-signature': badSig },
    body: badBody,
  });
  check('错误签名的 webhook 被拒（401）', badRes.status === 401, `got ${badRes.status}`);

  // 用【注册返回的】secret 签 → 应被收
  const goodTs = Date.now();
  const goodBody = JSON.stringify({ event: 'im.message.received', id: 'good-1', tenantId: '107', payload: { content: 'hi' } });
  const goodSig = createHmac('sha256', SECRET_FROM_REGISTER).update(`${goodTs}.${goodBody}`).digest('hex');
  const goodRes = await fetch(`http://127.0.0.1:${PLUGIN_PORT}/__plugin__/webhook/im.message.received`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-yumaiduo-timestamp': String(goodTs), 'x-yumaiduo-signature': goodSig },
    body: goodBody,
  });
  check('用注册返回的 secret 签名 → 被接受（2xx）',
        goodRes.status >= 200 && goodRes.status < 300, `got ${goodRes.status}`);

  console.log('\n──── 3. ★★★ 热替换：用注册返回的新令牌调平台 ────');
  // 触发一次平台调用：/internal/shops/read 会走 client.shops.list()
  const shopsBody = JSON.stringify({ tenant_id: '107' });
  const shopsTs = Date.now();
  const shopsSig = createHmac('sha256', SECRET_FROM_REGISTER).update(`${shopsTs}.${shopsBody}`).digest('hex');
  await fetch(`http://127.0.0.1:${PLUGIN_PORT}/internal/shops/read`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-yumaiduo-timestamp': String(shopsTs), 'x-yumaiduo-signature': shopsSig },
    body: shopsBody,
  }).catch(() => {});
  await wait(500);

  const usedFresh = platformCalls.some((c) => c.token === FRESH_TOKEN);
  const usedStale = platformCalls.some((c) => c.token === STALE_ENV_TOKEN);
  check('平台收到了带【新令牌】的调用', usedFresh,
        `calls=${JSON.stringify(platformCalls.slice(0, 3))}`);
  check('平台【没有】收到带旧 env 令牌的调用（原 bug 就是这里）', !usedStale,
        `stale used=${usedStale}`);

  console.log('\n──── 4. secret 交接文件 ────');
  check('交接文件已生成', existsSync(secretFile), secretFile);
  if (existsSync(secretFile)) {
    const j = JSON.parse(readFileSync(secretFile, 'utf8'));
    check('文件里是注册返回的那个 secret', j.webhookSecret === SECRET_FROM_REGISTER,
          `got ${String(j.webhookSecret).slice(0, 12)}…`);
    check('文件里记了令牌来源', j.tokenSource === 'register', String(j.tokenSource));
  }

  console.log('\n──── 4.5 没有 CORE_URL 时不能崩（影子/本地跑）────');
  // 用最小环境单独起一个进程，只给 PORT，不给 CORE_URL / tenant / token。
  const bareDir = mkdtempSync(join(tmpdir(), 'taplug-bare-'));
  const bare = spawn(process.execPath, [join(pluginDir, 'src', 'server.mjs')], {
    env: {
      PATH: process.env.PATH,
      PORT: '4713', HOST: '127.0.0.1',
      YUMAIDUO_COMMAND_MODE: 'shadow',
      YUMAIDUO_AUTO_REGISTER: 'false',
      PLUGIN_EXECUTION_JOURNAL_DIR: bareDir,
    },
    cwd: pluginDir, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let bareLog = '';
  bare.stdout.on('data', (d) => { bareLog += d; });
  bare.stderr.on('data', (d) => { bareLog += d; });
  await wait(2500);
  const bareAlive = bare.exitCode === null;
  check('没有 CORE_URL 时进程仍然存活（不崩）', bareAlive,
        bareAlive ? '' : 'exit=' + bare.exitCode + ' log=' + bareLog.slice(-200));
  let bareHz = null;
  try { bareHz = await (await fetch('http://127.0.0.1:4713/healthz')).json(); } catch { /* */ }
  check('没有 CORE_URL 时 /healthz 仍可访问', bareHz?.ok === true,
        JSON.stringify(bareHz));
  bare.kill('SIGKILL');
  rmSync(bareDir, { recursive: true, force: true });

  console.log('\n──── 5. 事件转发到后端 ────');
  check('后端收到了 /api/v1/events', backendEvents.length >= 1, `n=${backendEvents.length}`);
  check('后端按新 secret 验签通过', backendEvents.every((e) => e.ok),
        JSON.stringify(backendEvents.map((e) => e.ok)));
  check('事件内容被原样转发', backendEvents[0]?.body?.id === 'good-1',
        JSON.stringify(backendEvents[0]?.body || null));
} finally {
  child.kill('SIGKILL');
  platform.close();
  backend.close();
  rmSync(tmp, { recursive: true, force: true });
  console.log('\n──────── 插件日志（尾部）────────');
  console.log(pluginLog.split('\n').slice(-14).join('\n'));
  console.log(`\n结果：${pass} 通过 / ${fail} 失败`);
  process.exit(fail === 0 ? 0 : 1);
}
