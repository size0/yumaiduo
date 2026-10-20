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
if(process.env.YUMAIDUO_PLUGIN_TOKEN&&process.env.YUMAIDUO_TENANT_ID) cfg.client=createPluginClient({coreUrl:process.env.CORE_URL,pluginToken:process.env.YUMAIDUO_PLUGIN_TOKEN,tenantId:process.env.YUMAIDUO_TENANT_ID});
// Registration is opt-in; local Shadow runs never contact the platform.
if(process.env.YUMAIDUO_AUTO_REGISTER==='true'&&process.env.CORE_URL&&process.env.PLUGIN_DEVELOPER_TOKEN&&process.env.BASE_URL){registerPlugin({coreUrl:process.env.CORE_URL,baseUrl:process.env.BASE_URL,developerToken:process.env.PLUGIN_DEVELOPER_TOKEN}).then(async data=>{if(data.webhookSecret)cfg.secret=data.webhookSecret;
  cfg.registration={state:'registered',error:''};
  // ★★ 把自注册拿到的 webhookSecret 交给我们的后端。
  //   为什么需要这一步：官方架构里【没有外部后端】——
  //   secret 只由平台颁发给插件进程，明文只此一次。
  //   而我们是插件之外的另一个进程，要验插件发来的请求就得有同一个 secret。
  //   ⇒ 插件拿到后写到共享文件，后端读它（同机部署）。
  //   ★ 为什么落文件而不是写日志：官方 §9.1 建议调试模式打印完整 secret，
  //     但生产环境把密钥写进日志是泄露（日志会被收集/转发）。
  //   ★ 用【绝对路径】环境变量指定，避免写到不确定的位置。
  const sf=process.env.V5_BACKEND_SECRET_FILE;
  if(sf&&isAbsolute(sf)){
    try{const fs=await import('node:fs');
      fs.writeFileSync(sf,JSON.stringify({webhookSecret:cfg.secret,
        registeredAt:new Date().toISOString(),pluginId:manifest.id}),
        {mode:0o600});
      console.log('webhook secret handed off to '+sf);
    }catch(e){console.error('secret handoff failed',e.message);}
  }})
.catch(error=>{console.error('plugin registration failed',error.message);
  cfg.registration={state:'FAILED',error:String(error.message||error)};});}

// ★ 注册状态跟踪：官方 §14 要求 /healthz 返回 registered:true，
//   而只写 cfg.secret 的话，注册失败在健康检查里【看不出来】
//   （server.mjs 原来只 console.error 一行，容易被漏掉）。
//   ★ 注意：这几行必须是【独立的语句行】—— 我第一版把它们挤进一行，
//     结果 recoveryInspected 的声明被吞掉，插件每轮都报
//     "recoveryInspected is not defined"，命令队列全废。
if(process.env.YUMAIDUO_AUTO_REGISTER==='true') cfg.registration={state:'PENDING',error:''};
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

const server=http.createServer(async(req,res)=>{const chunks=[]; let size=0; for await(const chunk of req){size+=chunk.length;if(size>8*1024*1024){res.writeHead(413);res.end('request too large');return;}chunks.push(Buffer.isBuffer(chunk)?chunk:Buffer.from(chunk));} const rawBody=Buffer.concat(chunks); const raw=rawBody.toString('utf8'); try{if(await serveUi(req,res,rawBody,cfg.backendUrl,cfg.client,process.env.YUMAIDUO_TENANT_ID))return;if(req.method==='GET'&&req.url==='/healthz'){res.writeHead(200,{'content-type':'application/json'});const cap=sendCapability();return res.end(JSON.stringify({ok:true,registered:!cfg.registration||cfg.registration.state==='registered',mode:cfg.mode,registration:cfg.registration||null,can_send:cap.can_send,blocked_gates:cap.blocked,gates:cap.gates}));} if(req.method==='POST'&&req.url==='/internal/ticket-image/upload'){if(!verify(rawBody,req)){res.writeHead(401);return res.end('invalid signature');}try{const body=JSON.parse(raw);if(cfg.mode!=='live'||body.tenant_id!==process.env.YUMAIDUO_TENANT_ID)throw new Error('scope');const image=await uploadReplyImage(body,cfg.client,process.env.YUMAIDUO_TENANT_ID);res.writeHead(200,{'content-type':'application/json'});return res.end(JSON.stringify(image));}catch{res.writeHead(400);return res.end(JSON.stringify({error:'TICKET_IMAGE_UPLOAD_FAILED'}));}} if(req.method==='POST'&&req.url==='/internal/shops/read'){if(!verify(rawBody,req)){res.writeHead(401);return res.end('invalid signature');}try{const shops=await readOfficialShops(cfg.client,process.env.YUMAIDUO_TENANT_ID,true);res.writeHead(200,{'content-type':'application/json'});return res.end(JSON.stringify(shops));}catch(e){res.writeHead(e?.status===403?403:502,{'content-type':'application/json'});return res.end(JSON.stringify({error:e?.status===403?'SHOP_SCOPE_REQUIRED':'OFFICIAL_SHOP_READ_FAILED'}));}} if(req.method==='POST'&&req.url==='/internal/orders/read'){if(!verify(rawBody,req)){res.writeHead(401);return res.end('invalid signature');} try{const detail=await readOfficialOrder(JSON.parse(raw),cfg.client,process.env.YUMAIDUO_TENANT_ID,true);res.writeHead(200,{'content-type':'application/json'});return res.end(JSON.stringify(detail));}catch{res.writeHead(502,{'content-type':'application/json'});return res.end(JSON.stringify({error:'OFFICIAL_ORDER_READ_FAILED'}));}} if(req.method==='POST'&&req.url==='/internal/messages/read'){if(!verify(rawBody,req)){res.writeHead(401);return res.end('invalid signature');}try{const rows=await readOfficialMessages(JSON.parse(raw),cfg.client,process.env.YUMAIDUO_TENANT_ID,true);res.writeHead(200,{'content-type':'application/json'});return res.end(JSON.stringify(rows));}catch(e){res.writeHead(e?.status===403?403:502,{'content-type':'application/json'});return res.end(JSON.stringify({error:e?.status===403?'IM_HISTORY_SCOPE_REQUIRED':'IM_HISTORY_READ_FAILED'}));}} if(req.method==='POST'&&req.url==='/internal/shops/read'){if(!verify(rawBody,req)){res.writeHead(401);return res.end('invalid signature');}try{const shops=await readOfficialShops(cfg.client,process.env.YUMAIDUO_TENANT_ID,true);res.writeHead(200,{'content-type':'application/json'});return res.end(JSON.stringify(shops));}catch(e){res.writeHead(e?.status===403?403:502,{'content-type':'application/json'});return res.end(JSON.stringify({error:e?.status===403?'SHOP_SCOPE_REQUIRED':'OFFICIAL_SHOP_READ_FAILED'}));}} if(req.method==='POST'&&req.url==='/internal/orders/read'){if(!verify(rawBody,req)){res.writeHead(401);return res.end('invalid signature');} try{const detail=await readOfficialOrder(JSON.parse(raw),cfg.client,process.env.YUMAIDUO_TENANT_ID,true);res.writeHead(200,{'content-type':'application/json'});return res.end(JSON.stringify(detail));}catch{res.writeHead(502,{'content-type':'application/json'});return res.end(JSON.stringify({error:'OFFICIAL_ORDER_READ_FAILED'}));}} if(req.method==='POST'&&req.url.startsWith('/__plugin__/webhook/')){if(!verify(rawBody,req)){res.writeHead(401);return res.end('invalid signature');} const event=JSON.parse(raw); await attachOrderDetail(event,cfg.client,process.env.YUMAIDUO_TENANT_ID); const out=await backend('/api/v1/events','POST',event); if(out.status<400) await drain(); res.writeHead(out.status>=400?502:202,{'content-type':'application/json'});return res.end(JSON.stringify(out.data));} res.writeHead(404);res.end();}catch(e){res.writeHead(500);res.end(JSON.stringify({error:String(e)}));}});
// ★ 官方 §14 要求生产部署 HOST=0.0.0.0（让平台能访问）。
//   原来只 server.listen(cfg.port) —— 默认监听所有接口，
//   但显式绑 host 才符合清单，也避免某些环境只绑 localhost。
server.listen(cfg.port, process.env.HOST || undefined);

