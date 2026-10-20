import {readBuyerProfile} from './buyer-profile.mjs';
import {uploadReplyImage} from './reply-image.mjs';
import {isAbsolute} from 'node:path';
import {createBackendRequest} from './backend-request.mjs';
import {ExecutionJournal, executeOnce, flushReceipts, inspectRecovery} from './execution-journal.mjs';
import {executeCommand} from './execute-command.mjs';
import {readOfficialMessages} from './message-history.mjs';
import { readOfficialShops } from './shops.mjs';
import { attachOrderDetail, readOfficialOrder } from './order-event.mjs';
import { serveUi } from './ui.mjs';
import http from 'node:http';
import { registerPlugin } from './register.mjs';
// ★ 交接 secret 时要带上 pluginId（便于后端核对是哪一份实现写的）。
//   和 register.mjs 用的是同一份 manifest。
import manifest from '../yumaiduo.plugin.json' with { type: 'json' };
import { createPluginClient, verifyWebhookSignature } from '../vendor/plugin-sdk-server/dist/index.js';

const cfg={backendUrl:process.env.V5_BACKEND_URL||'http://127.0.0.1:8000',secret:process.env.YUMAIDUO_WEBHOOK_SECRET||'',port:Number(process.env.PORT||4015),mode:(process.env.YUMAIDUO_COMMAND_MODE||'shadow').toLowerCase(),client:null};
if(!['shadow','live'].includes(cfg.mode)) throw new Error('YUMAIDUO_COMMAND_MODE must be shadow or live');
function verify(rawBody,req){const timestamp=req.headers['x-yumaiduo-timestamp']; const signature=req.headers['x-yumaiduo-signature']; if(!cfg.secret||!timestamp||!signature)return false; return verifyWebhookSignature({secret:cfg.secret,timestamp,signature,rawBody});}
const backend=createBackendRequest({url:cfg.backendUrl,secret:()=>cfg.secret,
  timeoutMs:Number(process.env.V5_BACKEND_TIMEOUT_MS||15000)});
if(cfg.mode==='live' && (!process.env.PLUGIN_EXECUTION_JOURNAL_DIR || !isAbsolute(process.env.PLUGIN_EXECUTION_JOURNAL_DIR)))
  throw new Error('LIVE_EXECUTION_JOURNAL_DIRECTORY_REQUIRED');
const journal=new ExecutionJournal(process.env.PLUGIN_EXECUTION_JOURNAL_DIR||'.v5-execution-journal');
// ---- platform client (used for every outbound platform API call) --------
// Build one from the env token first, so a static-token deploy still works.
// The registration below MUST replace it with the token the platform hands
// back, otherwise we keep calling with a stale token forever.
const TENANT_ID = process.env.YUMAIDUO_TENANT_ID || '';
const CORE_URL = (process.env.CORE_URL || '').replace(/\/+$/, '');
function buildClient(token) {
  // All three are required: the SDK throws inside createPluginClient when
  // coreUrl is undefined ("Cannot read properties of undefined (reading
  // 'replace')"), and that crashes the whole process at startup.
  // Shadow / local runs legitimately have no CORE_URL, so return null there.
  if (!token || !TENANT_ID || !CORE_URL) return null;
  return createPluginClient({
    coreUrl: CORE_URL,
    pluginToken: token,
    tenantId: TENANT_ID,
  });
}
cfg.client = buildClient(process.env.YUMAIDUO_PLUGIN_TOKEN);
// ---- self registration -------------------------------------------------
// The plugin re-registers on EVERY process start to obtain a fresh
// webhookSecret + platform token (official guide section 5.1 / item 110).
// That is why this is mandatory, not optional:
//   - the platform rotates the token on every registration
//   - a static token in env WILL be revoked eventually
//   - if we ignore the returned token, the plugin can never recover
// YUMAIDUO_AUTO_REGISTER defaults to ON; set it to 'false' to opt out.
const AUTO_REGISTER = (process.env.YUMAIDUO_AUTO_REGISTER || 'true') !== 'false';

async function registerNow() {
  const missing = ['CORE_URL', 'PLUGIN_DEVELOPER_TOKEN', 'BASE_URL']
    .filter((k) => !process.env[k]);
  if (missing.length) {
    cfg.registration = { state: 'FAILED', error: 'MISSING_ENV:' + missing.join(',') };
    console.error('[reg] FAIL missing env: ' + missing.join(', '));
    return;
  }
  try {
    const data = await registerPlugin({
      coreUrl: process.env.CORE_URL,
      baseUrl: process.env.BASE_URL,
      developerToken: process.env.PLUGIN_DEVELOPER_TOKEN,
    });

    // (1) signing secret: used for core -> plugin webhooks AND plugin -> our backend
    if (data.webhookSecret) cfg.secret = String(data.webhookSecret);

    // (2) THE PLATFORM TOKEN. This is the line that was missing and caused
    //     the 401 E_UNAUTHORIZED outage: the response token was discarded.
    const token = data.token || data.pluginToken || '';
    if (token) {
      cfg.client = buildClient(token);
      cfg.tokenSource = 'register';
    } else {
      cfg.tokenSource = 'env';
      console.error('[reg] WARN response has no token, keeping env token '
        + '(it may already be revoked -- this is the old bug symptom)');
    }

    cfg.registration = {
      state: 'registered',
      error: '',
      status: data.status || '',
      pluginId: data.pluginId || manifest.id,
      tokenSource: cfg.tokenSource,
    };
    const mask = (s) => (s ? s.slice(0, 6) + '...(' + s.length + ')' : '(none)');
    console.log('[reg] OK pluginId=' + (data.pluginId || manifest.id)
      + ' status=' + (data.status || '?')
      + ' secret=' + mask(cfg.secret) + ' token=' + mask(token)
      + ' tokenSource=' + cfg.tokenSource);

    // (3) hand the new secret to our own backend. The platform reissues the
    //     secret on every registration; if the backend keeps the old one,
    //     every forwarded request fails signature verification (401).
    //     Official architecture has no external backend, so this step is ours.
    //     Written to a 0600 file rather than the log: logs get shipped around.
    const sf = process.env.V5_BACKEND_SECRET_FILE;
    if (sf && isAbsolute(sf)) {
      try {
        const fs = await import('node:fs');
        fs.writeFileSync(sf, JSON.stringify({
          webhookSecret: cfg.secret,
          tokenSource: cfg.tokenSource,
          registeredAt: new Date().toISOString(),
          pluginId: manifest.id,
        }), { mode: 0o600 });
        console.log('[reg] OK secret handed off to ' + sf);
      } catch (e) {
        console.error('[reg] FAIL secret handoff: ' + e.message);
      }
    } else {
      cfg.secretHandoff = 'NOT_CONFIGURED';
      console.error('[reg] WARN V5_BACKEND_SECRET_FILE not set (absolute path) '
        + '-- our backend will not learn the new secret');
    }
  } catch (error) {
    cfg.registration = { state: 'FAILED', error: String(error.message || error) };
    console.error('[reg] FAIL ' + error.message);
  }
}

if (AUTO_REGISTER) {
  cfg.registration = { state: 'PENDING', error: '' };
  void registerNow();
}
let recoveryInspected=false,lastInspection=0;
async function refreshRecovery(){
  if(recoveryInspected && Date.now()-lastInspection<60000)return;
  await inspectRecovery(journal,backend);
  recoveryInspected=true;lastInspection=Date.now();
  const local=journal.inspect();
  const remote=local.backend_recovery;
  if(local.recovery_required.length || remote.commands.length || remote.events.length || remote.bookings.length)
    console.error('execution recovery required',JSON.stringify({local:local.recovery_required.length,
      commands:remote.commands.length,events:remote.events.length,bookings:remote.bookings.length}));
}

async function executePending(){
  await flushReceipts(journal,backend);
  const claimed=await backend('/api/v1/commands/claim','POST',{limit:10});
  if(claimed.status!==200 || !Array.isArray(claimed.data?.commands))throw new Error('COMMAND_CLAIM_FAILED');
  for(const command of claimed.data.commands){
    await executeOnce(journal,command,c=>executeCommand(c,cfg,backend));
    await flushReceipts(journal,backend);
  }
}

// ==================================================================== 发送能力自检
// ★★ 为什么要有它：
//   LIVE_SEND_GLOBAL_ENABLED 没设 ⇒ SEND_MESSAGE 一律
//   LIVE_CAPABILITY_GATE_REJECTED ⇒ 买家一个字收不到，而且【不报错】。
//   命令"成功执行"了，只是结果是 REJECTED —— 这是最难查的一类问题：
//   链路每一环都没报错。所以启动时就把闸门状态打出来。
//
// ★ 只警告不抛错：shadow 模式下闸门本来就该关着，抛错会让人以为插件坏了。
export function sendCapability(env=process.env){
  const allowlist=Boolean(env.LIVE_SEND_ALLOWLIST_ACCOUNT_UNB &&
                          env.LIVE_SEND_ALLOWLIST_TENANT_ID);
  const globalSend=env.LIVE_SEND_GLOBAL_ENABLED==='true';
  const gates=[
    {name:'YUMAIDUO_COMMAND_MODE', ok:['shadow','live'].includes(env.YUMAIDUO_COMMAND_MODE),
     need:'shadow 或 live', why:'没设插件根本起不来'},
    {name:'V5_BACKEND_URL', ok:Boolean(env.V5_BACKEND_URL),
     need:'后端地址', why:'没设就默认 127.0.0.1:8000（可能是错的）'},
    {name:'YUMAIDUO_PLUGIN_TOKEN', ok:Boolean(env.YUMAIDUO_PLUGIN_TOKEN),
     need:'平台 token', why:'没设就没有官方客户端 ⇒ 所有发送命令 OFFICIAL_CLIENT_UNAVAILABLE'},
    {name:'YUMAIDUO_TENANT_ID', ok:Boolean(env.YUMAIDUO_TENANT_ID),
     need:'租户号', why:'同上（两个都要）'},
    {name:'LIVE_SEND_GLOBAL_ENABLED', ok:globalSend||allowlist,
     need:'true（或配 allowlist）',
     why:'没开 ⇒ SEND_MESSAGE 返回 LIVE_CAPABILITY_GATE_REJECTED，买家收不到消息'},
    {name:'LIVE_REPRICE_ENABLED', ok:env.LIVE_REPRICE_ENABLED==='true',
     need:'true', why:'没开 ⇒ 改价一律被拦'},
  ];
  if(env.YUMAIDUO_COMMAND_MODE==='live'){
    gates.push({name:'PLUGIN_EXECUTION_JOURNAL_DIR',
      ok:Boolean(env.PLUGIN_EXECUTION_JOURNAL_DIR),
      need:'绝对路径', why:'live 模式没这个目录会直接抛错（故意：真发消息必须可回查）'});
  }
  const blocked=gates.filter(g=>!g.ok);
  return {mode:env.YUMAIDUO_COMMAND_MODE||'(未设)',
          can_send:globalSend||allowlist, gates,
          blocked:blocked.map(g=>g.name)};
}

function logSendCapability(cap){
  if(cap.blocked.length===0){
    console.log('[发送能力] 全部闸门已开'+(cap.mode==='shadow'?'（但 mode=shadow，只演练不发出去）':''));
    return;
  }
  console.warn('[发送能力] ★ 有 '+cap.blocked.length+' 项未配好：');
  cap.gates.filter(g=>!g.ok).forEach(g=>{
    console.warn('  · '+g.name+'（需要 '+g.need+'）—— '+g.why);
  });
  if(cap.mode==='live') console.warn('  ⇒ live 模式下买家收不到消息，且不会报错。');
  else console.warn('  ⇒ 当前 mode='+cap.mode+'，本来就只演练。');
}


let polishing=false;
setInterval(async()=>{if(polishing||cfg.mode!=='live')return;polishing=true;try{await backend('/api/v1/products/polish/run','POST',{});}catch{console.error('product polish scheduling failed');}finally{polishing=false;}},60000);
let readingDue=false;
setInterval(async()=>{if(readingDue)return;readingDue=true;try{await backend('/api/v1/events/run-due','POST',{});}catch(error){console.error('conversation poll failed',error.message);}finally{readingDue=false;}},1000);
let draining=false;
async function drain(){if(draining)return;draining=true;try{await refreshRecovery();await executePending();if(cfg.mode==='live')await backend('/api/v1/fulfillment/auto/run','POST',{});}finally{draining=false;}}
setInterval(()=>drain().catch(error=>console.error('pending command drain failed',error.message)),1000);

// ★ 启动时把发送能力打出来（见上面 sendCapability 的注释）
logSendCapability(sendCapability());

const server=http.createServer(async(req,res)=>{const chunks=[]; let size=0; for await(const chunk of req){size+=chunk.length;if(size>8*1024*1024){res.writeHead(413);res.end('request too large');return;}chunks.push(Buffer.isBuffer(chunk)?chunk:Buffer.from(chunk));} const rawBody=Buffer.concat(chunks); const raw=rawBody.toString('utf8'); try{if(await serveUi(req,res,rawBody,cfg.backendUrl,cfg.client,process.env.YUMAIDUO_TENANT_ID,{secret:cfg.secret,pluginId:manifest.id}))return;if(req.method==='GET'&&req.url==='/healthz'){res.writeHead(200,{'content-type':'application/json'});const cap=sendCapability();return res.end(JSON.stringify({ok:true,registered:!cfg.registration||cfg.registration.state==='registered',mode:cfg.mode,registration:cfg.registration||null,can_send:cap.can_send,blocked_gates:cap.blocked,gates:cap.gates}));} if(req.method==='POST'&&req.url==='/internal/ticket-image/upload'){if(!verify(rawBody,req)){res.writeHead(401);return res.end('invalid signature');}try{const body=JSON.parse(raw);if(cfg.mode!=='live'||body.tenant_id!==process.env.YUMAIDUO_TENANT_ID)throw new Error('scope');const image=await uploadReplyImage(body,cfg.client,process.env.YUMAIDUO_TENANT_ID);res.writeHead(200,{'content-type':'application/json'});return res.end(JSON.stringify(image));}catch{res.writeHead(400);return res.end(JSON.stringify({error:'TICKET_IMAGE_UPLOAD_FAILED'}));}} if(req.method==='POST'&&req.url==='/internal/buyers/read'){if(!verify(rawBody,req)){res.writeHead(401);return res.end('invalid signature');}try{const profile=await readBuyerProfile(JSON.parse(raw),cfg.client,process.env.YUMAIDUO_TENANT_ID);res.writeHead(200,{'content-type':'application/json'});return res.end(JSON.stringify(profile));}catch(e){res.writeHead(e?.status===403?403:502,{'content-type':'application/json'});return res.end(JSON.stringify({error:'BUYER_PROFILE_UNAVAILABLE'}));}} if(req.method==='POST'&&req.url==='/internal/shops/read'){if(!verify(rawBody,req)){res.writeHead(401);return res.end('invalid signature');}try{const shops=await readOfficialShops(cfg.client,process.env.YUMAIDUO_TENANT_ID,true);res.writeHead(200,{'content-type':'application/json'});return res.end(JSON.stringify(shops));}catch(e){res.writeHead(e?.status===403?403:502,{'content-type':'application/json'});return res.end(JSON.stringify({error:e?.status===403?'SHOP_SCOPE_REQUIRED':'OFFICIAL_SHOP_READ_FAILED'}));}} if(req.method==='POST'&&req.url==='/internal/orders/read'){if(!verify(rawBody,req)){res.writeHead(401);return res.end('invalid signature');} try{const detail=await readOfficialOrder(JSON.parse(raw),cfg.client,process.env.YUMAIDUO_TENANT_ID,true);res.writeHead(200,{'content-type':'application/json'});return res.end(JSON.stringify(detail));}catch{res.writeHead(502,{'content-type':'application/json'});return res.end(JSON.stringify({error:'OFFICIAL_ORDER_READ_FAILED'}));}} if(req.method==='POST'&&req.url==='/internal/messages/read'){if(!verify(rawBody,req)){res.writeHead(401);return res.end('invalid signature');}try{const rows=await readOfficialMessages(JSON.parse(raw),cfg.client,process.env.YUMAIDUO_TENANT_ID,true);res.writeHead(200,{'content-type':'application/json'});return res.end(JSON.stringify(rows));}catch(e){res.writeHead(e?.status===403?403:502,{'content-type':'application/json'});return res.end(JSON.stringify({error:e?.status===403?'IM_HISTORY_SCOPE_REQUIRED':'IM_HISTORY_READ_FAILED'}));}} if(req.method==='POST'&&req.url==='/internal/blacklist/query'){if(!verify(rawBody,req)){res.writeHead(401);return res.end('invalid signature');}try{const body=JSON.parse(raw);const peer=String(body.peer_unb||'').trim();if(!peer)throw new Error('PEER_UNB_REQUIRED');if(cfg.mode!=='live'||body.tenant_id!==process.env.YUMAIDUO_TENANT_ID)throw new Error('scope');if(!cfg.client||!cfg.client.im||!cfg.client.im.blacklist)throw new Error('BLACKLIST_CAPABILITY_MISSING');const st=await cfg.client.im.blacklist.query(peer);res.writeHead(200,{'content-type':'application/json'});return res.end(JSON.stringify({ok:true,blacklisted:!!(st&&st.blacklisted),reason:(st&&st.reason)||'',peerNick:(st&&st.peerNick)||'',createdAt:(st&&st.createdAt)||''}));}catch(e){const code=e?.status===403?'BLACKLIST_SCOPE_REQUIRED':(e?.code||'BLACKLIST_QUERY_FAILED');res.writeHead(e?.status===403?403:502,{'content-type':'application/json'});return res.end(JSON.stringify({ok:false,error:String(code)}));}} if(req.method==='POST'&&req.url.startsWith('/__plugin__/webhook/')){if(!verify(rawBody,req)){res.writeHead(401);return res.end('invalid signature');} const event=JSON.parse(raw); await attachOrderDetail(event,cfg.client,process.env.YUMAIDUO_TENANT_ID); const out=await backend('/api/v1/events','POST',event); if(out.status<400) await drain(); res.writeHead(out.status>=400?502:202,{'content-type':'application/json'});return res.end(JSON.stringify(out.data));} res.writeHead(404);res.end();}catch(e){res.writeHead(500);res.end(JSON.stringify({error:String(e)}));}});
// ★ 官方 §14 要求生产部署 HOST=0.0.0.0（让平台能访问）。
//   原来只 server.listen(cfg.port) —— 默认监听所有接口，
//   但显式绑 host 才符合清单，也避免某些环境只绑 localhost。
server.listen(cfg.port, process.env.HOST || undefined);

