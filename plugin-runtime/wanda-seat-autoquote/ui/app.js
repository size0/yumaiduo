import { createPluginSdk } from './sdk.js';

const localMode=window.parent===window;
const fishMoreSdk=localMode?{ready:()=>Promise.resolve(null),authedFetch:(path,options={})=>localGatewayFetch(path,options),dispose:()=>{}}:createPluginSdk();
const fishMoreReady=localMode?Promise.resolve(null):fishMoreSdk.ready();
function bytesToBase64(bytes){let binary='';const chunk=0x8000;for(let index=0;index<bytes.length;index+=chunk){binary+=String.fromCharCode(...bytes.subarray(index,index+chunk));}return btoa(binary);}
async function gatewaySafeImage(file){
  const maxSourceBytes=68_000;
  if(file.size<=maxSourceBytes)return file;
  const image=await createImageBitmap(file);const maxSide=1900;const scale=Math.min(1,maxSide/Math.max(image.width,image.height));
  let canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(image.width*scale));canvas.height=Math.max(1,Math.round(image.height*scale));let context=canvas.getContext('2d',{alpha:false});context.fillStyle='#fff';context.fillRect(0,0,canvas.width,canvas.height);context.drawImage(image,0,0,canvas.width,canvas.height);image.close();
  let blob=null;
  for(let resize=0;resize<4&&(!blob||blob.size>maxSourceBytes);resize+=1){
    for(const quality of [.88,.8,.72,.64]){blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/webp',quality));if(blob&&blob.size<=maxSourceBytes)break;}
    if(blob&&blob.size<=maxSourceBytes)break;
    const smaller=document.createElement('canvas');smaller.width=Math.max(720,Math.round(canvas.width*.82));smaller.height=Math.max(720,Math.round(canvas.height*.82));const smallerContext=smaller.getContext('2d',{alpha:false});smallerContext.fillStyle='#fff';smallerContext.fillRect(0,0,smaller.width,smaller.height);smallerContext.drawImage(canvas,0,0,smaller.width,smaller.height);canvas=smaller;context=smallerContext;
  }
  if(!blob||blob.size>maxSourceBytes)throw new Error('图片体积过大，请裁剪后重试。');
  return new File([blob],`${file.name.replace(/\.[^.]+$/,'')||'movie-ticket'}.webp`,{type:'image/webp',lastModified:file.lastModified});
}
async function serializeFormData(form){const entries=[];for(const [name,value] of form.entries()){if(value instanceof File){const image=await gatewaySafeImage(value);entries.push({name,file:{name:image.name,type:image.type,base64:bytesToBase64(new Uint8Array(await image.arrayBuffer()))}});}else{entries.push({name,value:String(value)});}}return {_wanda_v4_formdata:true,entries};}
async function localGatewayFetch(path,options={}){const normalized=String(path).replace(/^\/+/, '');if(normalized.startsWith('ui/v4/'))return fetch(`/${normalized.slice('ui/v4/'.length)}`,options);if(normalized.startsWith('ui/api/orders'))return new Response(JSON.stringify({orders:[],observedCount:0}),{status:200,headers:{'content-type':'application/json'}});return fetch(`/${normalized.replace(/^ui\//,'')}`,options);}
async function gatewayFetch(path,options={}){if(localMode)return localGatewayFetch(path,options);await fishMoreReady;return fishMoreSdk.authedFetch(path,options);}
async function v4Fetch(path,options={}){await fishMoreReady;const normalized=String(path).replace(/^\/+/, '');const request={...options};if(request.body instanceof FormData){request.headers={...(request.headers||{}),'content-type':'application/json'};request.body=JSON.stringify(await serializeFormData(request.body));}return gatewayFetch(`ui/v4/${normalized}`,request);}
async function v4ImageFetch(form){
  await fishMoreReady;
  if(localMode)return fetch('/api/chat/image-messages',{method:'POST',body:form});
  const started=await gatewayFetch('ui/v4/jobs/image-message',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(await serializeFormData(form))});
  if(started.status!==202)return started;
  const job=await started.json();if(!job.job_id)throw new Error('图片识别任务创建失败');
  for(let attempt=0;attempt<100;attempt+=1){
    await new Promise(resolve=>setTimeout(resolve,900));
    const result=await gatewayFetch(`ui/v4/jobs/${encodeURIComponent(job.job_id)}`);
    if(result.status!==202)return result;
  }
  return new Response(JSON.stringify({detail:'图片识别超时，请稍后重试。'}),{status:504,headers:{'content-type':'application/json'}});
}
window.addEventListener('beforeunload',()=>fishMoreSdk.dispose(),{once:true});

// Stable customer-service boundary for the future agent integration:
  // POST /api/chat/image-messages and POST /api/chat/text-messages
  const $ = id => document.getElementById(id);
  const allowed = ['image/jpeg','image/png','image/webp'];
  const state = {
    file: null,
    pendingUrl: null,
    sending: false,
    conversationId: (crypto.randomUUID ? crypto.randomUUID() : `chat-${Date.now()}`)
  };
  const chat = $('chatWindow');
  let loadedPrompt = '';
  const headerActions=document.querySelector('.header-actions');
  if(headerActions&&!headerActions.querySelector('.agent-simulation-badge')){
    const badge=element('span','agent-simulation-badge','Agent仿真 · 完整编排');
    badge.title='工作台请求使用后端 Agent 仿真，不执行真实平台写操作';
    headerActions.prepend(badge);
  }
  const modelBadge=$('currentModel'); if(modelBadge)modelBadge.textContent='上传千问 · URL良票优先';

  function now() { return new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date()); }
  function money(value) { return Number.isFinite(value) ? `¥${value.toFixed(2).replace(/\.00$/,'')}` : '—'; }
  function centsAmount(value) { if(value===null||value===undefined||value==='')return '—';const amount=Number(value);return Number.isSafeInteger(amount)?`¥${(amount/100).toFixed(2)}`:'—'; }
  function scrollBottom() { requestAnimationFrame(()=>chat.scrollTo({top:chat.scrollHeight,behavior:'smooth'})); }
  function element(tag,className,text) { const node=document.createElement(tag); if(className) node.className=className; if(text!==undefined) node.textContent=text; return node; }

  function appendMessage(role, text, options={}) {
    const row=element('article',`message ${role} dynamic-message`); row.dataset.role=role;
    if(role==='assistant') row.append(element('div','message-avatar','AI'));
    const stack=element('div','message-stack');
    if(role==='assistant') stack.append(element('div','sender','AI 客服'));
    const bubble=element('div','bubble');
    if(options.imageUrl) { const image=element('img','bubble-image'); image.src=options.imageUrl; image.alt='买家发送的电影票截图'; bubble.append(image); }
    if(text) bubble.append(document.createTextNode(text));
    if(options.recognition) bubble.append(buildRecognitionCard(options.recognition));
    if(options.quote) bubble.append(buildQuoteCard(options.quote));
    if(options.agentTrace) bubble.append(buildAgentTraceCard(options.agentTrace));
    stack.append(bubble,element('div','message-time',now())); row.append(stack); chat.append(row); scrollBottom(); return row;
  }

  // Render only the safe, structured execution summary returned by the Agent.
  // Private chain-of-thought, raw provider payloads and credentials are never shown.
  function buildAgentTraceCard(trace) {
    const card=element('section','agent-trace-card');
    card.append(element('div','agent-trace-head', 'Agent执行轨迹 · 仿真编排'));
    const rows=Array.isArray(trace)?trace:(trace?.steps||trace?.events||trace?.rounds||[]);
    if(!rows.length){card.append(element('div','agent-trace-empty','本次请求未返回可展示的执行阶段。'));return card;}
    const list=element('ol','agent-trace-list');
    rows.slice(0,40).forEach((item,index)=>{
      if(!item||typeof item!=='object')return;
      const row=element('li','agent-trace-row');
      const stage=String(item.stage??item.phase??item.type??item.name??'执行阶段').slice(0,80);
      const round=item.round??item.round_index??item.turn??index;
      const tool=String(item.tool??item.tool_name??item.action??'—').slice(0,100);
      const status=String(item.status??item.state??'completed').slice(0,40);
      const summary=String(item.summary??item.safe_summary??item.result_summary??item.message??'').slice(0,240);
      row.append(element('div','agent-trace-meta',`阶段：${stage} · 第 ${Number(round)+1} 轮`),element('div','agent-trace-tool',`工具：${tool} · 状态：${status}`));
      if(summary)row.append(element('div','agent-trace-summary',`摘要：${summary}`));
      list.append(row);
    });
    card.append(list); return card;
  }

  function extractAgentTrace(body){
    return body?.agent_trace||body?.trace||body?.message?.agent_trace||body?.message?.trace||null;
  }

  function appendTyping(label) {
    const row=element('article','message assistant dynamic-message'); row.dataset.role='assistant'; row.id='typingMessage';
    row.append(element('div','message-avatar','AI'));
    const stack=element('div','message-stack'); stack.append(element('div','sender','AI 客服'));
    const bubble=element('div','bubble typing'); const dots=element('span','typing-dots'); for(let i=0;i<3;i++) dots.append(document.createElement('i'));
    bubble.append(dots,element('span','typing-status',label)); stack.append(bubble); row.append(stack); chat.append(row); scrollBottom(); return row;
  }

  function buildQuoteCard(quote) {
    const card=element('section','recognition-card quote-result-card');
    const ruleApplied=Boolean(quote.pricing_rule_version);
    const exactTitle=ruleApplied?(quote.same_type_probe_used?'后台同类型核验报价':'后台实时报价'):(quote.same_type_probe_used?'W+ 会员同类型核验报价':'W+ 会员锁座报价');
    const areaTitle=ruleApplied?'中间 W+ 区域后台报价':'中间 W+ 区域会员锁座报价';
    const head=element('div','recognition-head'); head.append(element('strong','',quote.quote_scope==='exact_seats'?exactTitle:areaTitle),element('span','',quote.pricing_source||'万达官方实时区域价格')); card.append(head);
    const grid=element('div','fact-grid');
    const cents=value=>Number.isInteger(value)?`¥${(value/100).toFixed(2)}`:'—';
    const facts=[['W+会员活动价',cents(quote.member_unit_price_cents)],['官方计价基准',cents(quote.base_unit_cents)],['计价基准合计',cents(quote.base_total_cents)],['最终报价单价',cents(quote.unit_quote_cents)],['报价合计',cents(quote.total_quote_cents)],['价格来源',quote.price_source],['区域',quote.seat_zone_type],['规则版本',quote.pricing_rule_version],['匹配影院',quote.matched_cinema_name,'wide']];
    facts.filter(([,value])=>value&&value!=='—').forEach(([label,value,width])=>{const item=element('div',`fact ${width||''}`);item.append(element('small','',label),element('b','',value));grid.append(item)}); card.append(grid);
    if(quote.detail)card.append(element('div','recognition-note',quote.detail)); return card;
  }

  function buildRecognitionCard(data) {
    const card=element('section','recognition-card');
    const head=element('div','recognition-head'); head.append(element('strong','', '截图识别详情'),element('span','',`置信度 ${Math.round((data.confidence||0)*100)}%`)); card.append(head);
    const grid=element('div','fact-grid');
    const facts=[
      ['影片',data.movie_name],['影院',data.cinema_name],['日期',data.date||data.date_text],
      ['场次',[data.showtime_start,data.showtime_end].filter(Boolean).join(' – ')],['影厅',data.hall_name],
      ['语言 / 制式',[data.language,data.format].filter(Boolean).join(' · ')],
      ['座位',data.seat_display||'W+座位','wide'],
      ['截图总额',money(data.displayed_total)]
    ];
    facts.filter(([,value])=>value).forEach(([label,value,width])=>{const item=element('div',`fact ${width||''}`);item.append(element('small','',label),element('b','',value));grid.append(item)});
    card.append(grid);
    const notes=[...(data.warnings||[]),...(data.missing_fields?.length?[`未识别：${data.missing_fields.join('、')}`]:[])];
    if(notes.length) card.append(element('div','recognition-note',notes.join('；')));
    return card;
  }

  function selectFile(file) {
    if(!file) return;
    if(!allowed.includes(file.type)) return appendMessage('assistant','请上传 JPG、PNG 或 WebP 格式的图片。');
    if(file.size>10*1024*1024) return appendMessage('assistant','图片不能超过 10 MB，请压缩后再发送。');
    if(state.pendingUrl) URL.revokeObjectURL(state.pendingUrl);
    state.file=file; state.pendingUrl=URL.createObjectURL(file); $('pendingImage').src=state.pendingUrl; $('pendingName').textContent=file.name;
    $('pendingSize').textContent=`${(file.size/1024/1024).toFixed(2)} MB · 等待发送`; $('pending').classList.add('show'); updateSend();
  }
  function clearFile() { if(state.pendingUrl) URL.revokeObjectURL(state.pendingUrl); state.file=null;state.pendingUrl=null;$('fileInput').value='';$('pending').classList.remove('show');updateSend(); }
  function updateSend() { $('send').disabled=state.sending||(!state.file&&!$('composer').value.trim()); }

  async function sendMessage() {
    const text=$('composer').value.trim(); const file=state.file;
    if(state.sending||(!text&&!file)) return;
    const buyerImage=file?URL.createObjectURL(file):null; appendMessage('buyer',text,{imageUrl:buyerImage});
    $('composer').value=''; $('composer').style.height='42px'; clearFile(); state.sending=true; updateSend();
    const progressLabel=file?'正在识别图片':'正在生成回复'; const typing=appendTyping(`${progressLabel} · 0 秒`); const progressStarted=Date.now();
    const progressTimer=setInterval(()=>{const status=typing.querySelector('.typing-status');if(status)status.textContent=`${progressLabel} · ${Math.floor((Date.now()-progressStarted)/1000)} 秒`;},1000);
    try {
      let response;
      if(file) {
        const form=new FormData(); form.append('conversation_id',state.conversationId); form.append('message_text',text); form.append('simulation','true'); form.append('image',file);
        response=await v4ImageFetch(form);
      } else {
        response=await v4Fetch('/api/chat/text-messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({conversation_id:state.conversationId,text,simulation:true})});
      }
      const raw=await response.text();let body={};try{body=raw?JSON.parse(raw):{};}catch{body={};}
      if(!response.ok){const detail=typeof body.detail==='string'?body.detail:body?.detail?.[0]?.msg;throw new Error(body?.error?.message||detail||`回复生成失败（HTTP ${response.status}），请稍后重试。`);}
      typing.remove(); appendMessage('assistant',body.message.text,{recognition:body.message.recognition,quote:body.message.quote,agentTrace:extractAgentTrace(body)});
    } catch(error) { typing.remove(); appendMessage('assistant',`暂时没有处理成功：${error.message}`); }
    finally { clearInterval(progressTimer);state.sending=false;updateSend();$('composer').focus();refreshDiagnostics(); }
  }

  $('attach').addEventListener('click',()=>$('fileInput').click());
  $('fileInput').addEventListener('change',event=>selectFile(event.target.files[0]));
  $('removeFile').addEventListener('click',clearFile);
  $('send').addEventListener('click',sendMessage);
  $('composer').addEventListener('input',event=>{event.target.style.height='42px';event.target.style.height=`${Math.min(event.target.scrollHeight,116)}px`;updateSend()});
  $('composer').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendMessage()}});
  $('composer').addEventListener('paste',event=>{const image=[...event.clipboardData.files].find(file=>file.type.startsWith('image/'));if(image){event.preventDefault();selectFile(image)}});
  let dragDepth=0;
  chat.addEventListener('dragenter',event=>{event.preventDefault();dragDepth++;chat.classList.add('dragging')});
  chat.addEventListener('dragover',event=>event.preventDefault());
  chat.addEventListener('dragleave',event=>{event.preventDefault();dragDepth=Math.max(0,dragDepth-1);if(!dragDepth)chat.classList.remove('dragging')});
  chat.addEventListener('drop',event=>{event.preventDefault();dragDepth=0;chat.classList.remove('dragging');selectFile(event.dataTransfer.files[0])});
  $('clearChat').addEventListener('click',()=>{chat.querySelectorAll('.dynamic-message').forEach(node=>node.remove());clearFile();$('composer').value='';state.conversationId=(crypto.randomUUID?crypto.randomUUID():`chat-${Date.now()}`);updateSend()});

  let diagnosticsLoading=false;
  const diagnosticLabels=Object.freeze({
    liangpiao_recognition_completed:'良票主识别完成',liangpiao_recognition_fallback_to_qwen:'良票不可用，切换千问备用',
    vision_provider_response:'千问图片识别响应',vision_fast_path_completed:'图片识别快速路径完成',
    vision_fast_path_rejected:'图片识别需要格式修复',vision_interpreter_provider_response:'GPT识别格式修复响应',
    vision_fallback_completed:'图片识别兜底完成',vision_conversation_context_merged:'多图会话信息已合并',
    chat_conversation_context_used:'AI客服已使用会话上下文',chat_provider_response:'AI客服模型响应',chat_redundant_followup_replaced:'已拦截AI重复追问并使用权威报价回复',
    wanda_showtimes_response:'查询万达官方场次',wanda_realtime_seats_response:'查询万达实时座位',
    wanda_temporary_order_response:'创建临时锁座订单',wanda_temporary_order_status_response:'确认临时锁座状态',
    wanda_member_offers_response:'查询W+会员活动价',wanda_temporary_cancel_response:'取消临时订单',
    wanda_cancelled_order_status_response:'确认订单取消状态',wanda_release_recheck_response:'读取释放后的实时座位',wanda_release_verification:'判定座位释放结果',
    wanda_same_type_probe_used:'使用相同座位类型核价',wanda_same_type_probe_retry:'更换同类型座位重试',
    wanda_order_status_short_retry:'订单状态响应过慢，正在短超时重试',
    pricing_rules_saved:'运营报价规则已保存',pricing_rules_applied:'后台报价规则已应用',
    wanda_direct_quote_completed:'万达会员报价流程完成',quote_unavailable:'万达实时报价未取得',
    request_completed:'本次请求处理完成'
  });
  function diagnosticClass(entry) { const event=entry.event;if(event==='wanda_release_verification'&&entry.details?.seat_released===false)return 'diagnostic-error';if(event.includes('invalid')||event.includes('failed'))return event.includes('schema')?'diagnostic-schema':'diagnostic-error'; return ''; }
  function diagnosticStatus(entry) {
    if(entry.event==='wanda_release_verification')return entry.details?.seat_released?'目标座位已恢复可售':'目标座位仍不可选';
    const data=entry.details?.response?.data;
    const status=data&&typeof data==='object'?String(data.orderStatus??''):'';
    if(status==='10')return '订单处理中'; if(status==='40')return '锁座成功'; if(status==='60')return '订单已取消';
    if(entry.details?.response?.code===0)return '官方返回成功'; return '';
  }
  function renderDiagnostics(entries) {
    const root=$('diagnosticsEntries'); root.replaceChildren();
    if(!entries.length){root.append(element('div','diagnostic-empty','暂无日志。发送一张图片后会显示完整模型响应。'));return;}
    [...entries].reverse().forEach(entry=>{
      const detail=element('details','diagnostic-entry'); if(['vision_schema_invalid','vision_fast_path_rejected','vision_provider_response','vision_interpreter_provider_response','chat_provider_response'].includes(entry.event))detail.open=true;
      const summary=element('summary'); const eventName=diagnosticLabels[entry.event]||entry.event; const eventLabel=element('span',`diagnostic-event ${diagnosticClass(entry)}`,eventName); eventLabel.title=entry.event; summary.append(eventLabel);
      const duration=entry.details?.duration_ms!==undefined?`${entry.details.duration_ms} ms`:''; const status=diagnosticStatus(entry); const stamp=new Date(entry.timestamp).toLocaleTimeString('zh-CN',{hour12:false});
      summary.append(element('span','diagnostic-meta',[duration,status,stamp,entry.request_id].filter(Boolean).join(' · ')));
      const pre=element('pre','diagnostic-json'); pre.textContent=JSON.stringify(entry, null, 2); detail.append(summary,pre); root.append(detail);
    });
  }
  async function refreshDiagnostics() {
    if(diagnosticsLoading)return; diagnosticsLoading=true;
    try { const response=await v4Fetch('/api/diagnostics/recent?limit=50'); const body=await response.json(); if(response.ok)renderDiagnostics(body.entries||[]); }
    finally { diagnosticsLoading=false; }
  }
  async function clearDiagnostics() { await v4Fetch('/api/diagnostics/recent',{method:'DELETE'}); await refreshDiagnostics(); }
  let loadedAgentToolAudits=[];let loadedEventAudits=[];let loadedManualTasks=[];
  const auditToolLabels=Object.freeze({recognize_screenshot:'识别买家截图',get_authoritative_quote:'获取权威报价',get_quote:'获取报价',cinema_list:'查询影院',resolve_cinema:'确认买家选择的影院','cinema.list':'查询影院候选','show.list':'查询场次候选',resolve_showtime:'确认买家选择的场次',resolve_image_conflict:'确认买家澄清的信息','seat.list':'查询座位','show.detail':'查询场次详情','order.preflight':'订单预检','order.detail':'查询订单状态',get_order_state:'查询订单状态'});
  const auditEventLabels=Object.freeze({'im.message.received':'收到买家消息','order.created':'买家已拍下订单','order.paid':'订单已付款','order.updated':'订单状态更新','liangpiao.callback':'良票出票回调'});
  function auditStatusLabel(value){return({pending:'待处理',claimed:'处理中',completed:'已完成',succeeded:'成功',failed:'失败',unknown:'结果待核验',cancelled:'已取消'}[String(value||'')]||String(value||'未知状态'));}
  function displayBuyer(record){return String(record.buyer_name||'').trim()||'昵称暂未同步';}
  function displayShop(record){return String(record.shop_name||'').trim()||'店铺名称暂未同步';}
  function auditMatches(record,query){if(!query)return true;return[record.buyer_name,record.buyer_id,record.shop_name,record.shop_id,record.chat_id,record.event_id,record.tool_name,record.event_type,record.reason,record.rule_code].some(value=>String(value||'').toLowerCase().includes(query));}
  function renderAgentAudit(records,eventAudits=[]) {
    const query=String($('agentAuditSearch')?.value||'').trim().toLowerCase();records=records.filter(record=>auditMatches(record,query));eventAudits=eventAudits.filter(record=>auditMatches(record,query));
    const root=$('agentAuditEntries'); root.replaceChildren();
    root.append(element('h3','audit-section-title','事件路由与规则结果'));
    if(!eventAudits.length)root.append(element('div','diagnostic-empty','暂无事件路由审计。'));
    eventAudits.forEach(record=>{
      const detail=element('details','diagnostic-entry');const summary=element('summary');
      summary.append(element('span','diagnostic-event',auditEventLabels[record.event_type]||record.event_type||'未知事件'));
      summary.append(element('span','diagnostic-meta',[record.reply_route==='agent'?'Agent回复':record.reply_route==='rule'?'规则处理':'未发送回复',record.ai_called?'已调用AI':'未调用AI',displayBuyer(record)].filter(Boolean).join(' · ')));
      const body=element('div','audit-facts');body.append(recordFact('买家昵称',displayBuyer(record)),recordFact('买家ID',record.buyer_id),recordFact('店铺名称',displayShop(record)),recordFact('店铺ID',record.shop_id),recordFact('聊天ID',record.chat_id),recordFact('处理规则',record.rule_code||'无'),recordFact('订单状态',record.order_state||'无订单'),recordFact('未回复原因',record.suppressed_reason||'无'));detail.append(summary,body);root.append(detail);
    });
    root.append(element('h3','audit-section-title','Agent 工具调用'));
    if(!records.length){root.append(element('div','diagnostic-empty','暂无 Agent 工具调用记录。'));return;}
    records.forEach(record=>{
      const detail=element('details','diagnostic-entry');
      const summary=element('summary');
      summary.append(element('span','diagnostic-event',auditToolLabels[record.tool_name]||record.tool_name||'未知工具'));
      summary.append(element('span','diagnostic-meta',[auditStatusLabel(record.status),record.round_index!==undefined?`第${Number(record.round_index)+1}轮`:'' ,displayBuyer(record)].filter(Boolean).join(' · ')));
      const body=element('div','audit-facts');
      body.append(recordFact('买家昵称',displayBuyer(record)),recordFact('买家ID',record.buyer_id),recordFact('店铺名称',displayShop(record)),recordFact('店铺ID',record.shop_id),recordFact('聊天ID',record.chat_id),recordFact('事件ID',record.event_id));
      detail.append(summary,body); root.append(detail);
    });
  }
  async function loadAgentAudit() {
    const root=$('agentAuditEntries'); root.replaceChildren(element('div','diagnostic-empty','正在读取 Agent 审计…'));
    try { const[toolResponse,eventResponse]=await Promise.all([v4Fetch('/api/rules-first/agent-tool-calls?limit=200'),v4Fetch('/api/rules-first/event-audits?limit=200')]);const toolData=await toolResponse.json();const eventData=await eventResponse.json();if(!toolResponse.ok)throw new Error(toolData?.detail||'读取 Agent 工具审计失败');if(!eventResponse.ok)throw new Error(eventData?.detail||'读取事件路由审计失败');loadedAgentToolAudits=Array.isArray(toolData.records)?toolData.records:[];loadedEventAudits=Array.isArray(eventData.records)?eventData.records:[];renderAgentAudit(loadedAgentToolAudits,loadedEventAudits); }
    catch(error) { root.replaceChildren(element('div','diagnostic-empty',error.message)); }
  }

  function manualReasonLabel(value) {
    const labels={external_write_fuse_open:'系统写入保护已开启',fulfillment_required:'等待人工出票或发货',multiple_pending_orders:'发现多个待处理订单',order_unverified:'订单归属尚未核验',send_message_failed:'消息发送失败，请人工联系买家',paid_amount_unverified:'已付款，但付款金额尚未核验',paid_amount_mismatch:'付款金额与报价不一致',order_already_paid:'订单已付款，禁止自动改价',platform_price_change_rejected:'平台拒绝自动改价',conversation_snapshot_unavailable:'暂时无法读取买家会话'};return labels[value]||`需要人工核验（${String(value||'未分类')}）`;
  }
  async function claimManualTask(task,button) {
    button.disabled=true;
    try {const response=await v4Fetch(`/api/rules-first/manual-tasks/${encodeURIComponent(task.task_id)}/claim`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:task.transaction_revision})});const data=await response.json();if(!response.ok)throw new Error(data?.detail||'领取任务失败');await loadManualTasks();}catch(error){button.textContent=error.message;button.disabled=false;}
  }
  async function completeManualTask(task,resolution,button) {
    const warning=resolution==='resume'?'恢复后自动化会从 ORDER_UNVERIFIED 状态继续，仍会重新执行全部安全门禁。确认恢复吗？':'只有平台订单事件或其他受控流程已经解除人工暂停时才能关闭任务；该按钮本身不会改订单、履约或交易状态。确认检查并关闭吗？';
    if(!window.confirm(warning))return;button.disabled=true;
    try {const response=await v4Fetch(`/api/rules-first/manual-tasks/${encodeURIComponent(task.task_id)}/complete`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:task.transaction_revision,lease_token:task.lease_token,resolution})});const data=await response.json();if(!response.ok)throw new Error(data?.detail||'处理任务失败');await loadManualTasks();}catch(error){button.textContent=error.message;button.disabled=false;}
  }
  function renderManualTasks(tasks) {
    const root=$('manualTaskEntries');root.replaceChildren();const query=String($('manualTaskSearch')?.value||'').trim().toLowerCase();tasks=tasks.filter(task=>auditMatches(task,query));
    if(!tasks.length){root.append(element('div','diagnostic-empty',query?'没有找到匹配的人工任务。':'暂无人工任务。'));return;}
    tasks.forEach(task=>{const detail=element('details','diagnostic-entry');const summary=element('summary');summary.append(element('span','diagnostic-event',manualReasonLabel(task.reason)),element('span','diagnostic-meta',[auditStatusLabel(task.status),displayBuyer(task)].filter(Boolean).join(' · ')));const body=element('div','audit-facts');body.append(recordFact('店铺名称',displayShop(task)),recordFact('店铺ID',task.shop_id),recordFact('买家昵称',displayBuyer(task)),recordFact('买家ID',task.buyer_id),recordFact('聊天ID',task.chat_id),recordFact('处理进度',auditStatusLabel(task.status)),recordFact('领取人',task.claimed_by||'未领取'),recordFact('任务编号',task.task_id));const actions=element('div','manual-task-actions');if(task.status==='pending'){const claim=element('button','diagnostics-button','领取任务');claim.type='button';claim.disabled=task.reason==='fulfillment_required';claim.title=claim.disabled?'该任务等待官方履约事件，不允许人工领取':'';claim.addEventListener('click',()=>claimManualTask(task,claim));actions.append(claim);}else if(task.status==='claimed'&&task.lease_token){const resolved=element('button','diagnostics-button','状态已更新，关闭任务');resolved.type='button';resolved.addEventListener('click',()=>completeManualTask(task,'resolved',resolved));const resume=element('button','diagnostics-button','恢复自动化');resume.type='button';resume.addEventListener('click',()=>completeManualTask(task,'resume',resume));actions.append(resolved,resume);}detail.append(summary,body);if(actions.childElementCount)detail.append(actions);root.append(detail);});
  }
  async function loadManualTasks() {
    const root=$('manualTaskEntries');root.replaceChildren(element('div','diagnostic-empty','正在读取人工任务…'));
    try {const response=await v4Fetch('/api/rules-first/manual-tasks');const data=await response.json();if(!response.ok)throw new Error(data?.detail||'读取人工任务失败');loadedManualTasks=Array.isArray(data.tasks)?data.tasks:[];renderManualTasks(loadedManualTasks);}catch(error){root.replaceChildren(element('div','diagnostic-empty',error.message));}
  }

  const workspaceMeta=Object.freeze({
    chat:['客服工作台','识别买家截图、查询官方报价并延续安全会话。'],
    shops:['店铺开关','按当前鱼麦多租户控制各店铺的自动回复和自动改价。'],
    pricing:['运营报价','配置确定性整数分报价规则，保存后下一次核价立即使用。'],
    'quote-records':['报价记录','查看当前租户通过官方实时核价取得的历史报价。'],
    orders:['订单管理','通过鱼麦多权威订单接口查看插件已观察订单的最新状态。'],
    templates:['回复话术','编辑识图、报价、引导和改价成功通知；变量名称全部使用中文。'],
    conversation:['会话策略','按买家会话配置上下文记忆、业务介入区间与人工接管等待。'],
    model:['模型与接口','独立管理识图与AI回复服务，保存后下一次请求立即使用。'],
    knowledge:['知识库','审核生产知识与人工会话候选，禁止未经审批直接进入运行时。'],
    'agent-audit':['Agent审计','查看当前租户的 Agent 工具调用与规则边界结果。'],
    'manual-tasks':['人工任务','查看当前租户被安全门禁转入人工处理的任务。']
  });
  let activeWorkspace='chat';
  function showWorkspace(name,focus=false) {
    if(!workspaceMeta[name])name='chat'; activeWorkspace=name; $('siteNavigation').scrollTop=0;
    const settingsWorkspace=['shops','pricing','quote-records','orders','templates','conversation','model','knowledge'].includes(name);
    document.body.classList.toggle('settings-active',settingsWorkspace);
    document.documentElement.classList.toggle('settings-active',settingsWorkspace);
    $('mainPage').hidden=settingsWorkspace;
    $('workspaceChat').hidden=name!=='chat'; $('diagnosticsPanel').hidden=name!=='logs'; $('agentAuditPanel').hidden=name!=='agent-audit'; $('manualTasksPanel').hidden=name!=='manual-tasks';
    $('settingsDrawer').classList.toggle('show',settingsWorkspace); $('settingsDrawer').setAttribute('aria-hidden',String(!settingsWorkspace));
    document.querySelectorAll('[data-workspace]').forEach(button=>{const selected=button.dataset.workspace===name;button.classList.toggle('active',selected);button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1});
    if(settingsWorkspace){
      const model=name==='model'; $('modelSettingsPanel').hidden=!model; $('operationsSettingsPanel').hidden=model;
      document.querySelectorAll('[data-operation-view]').forEach(section=>section.hidden=model||section.dataset.operationView!==name);
      $('saveSettings').hidden=!model; $('settingsFooter').hidden=!model;
      $('workspaceSettingsTitle').textContent=workspaceMeta[name][0]; $('workspaceSettingsDescription').textContent=workspaceMeta[name][1]; settingsMessage('');
    }
    if(name==='shops')loadShops();
    if(name==='quote-records')loadQuoteRecords();
    if(name==='orders')loadOrders();
    if(name==='templates'){loadTemplates();loadReminderSettings();loadReminderTasks();}
    if(name==='conversation')loadConversationPolicy();
    if(name==='knowledge')loadKnowledge();
    if(name==='agent-audit')loadAgentAudit();
    if(name==='manual-tasks')loadManualTasks();
    history.replaceState(null,'',`#${name}`);
    if(focus){const target=document.querySelector(`[data-workspace="${name}"]`);requestAnimationFrame(()=>target?.focus())}
  }
  function switchSettingsTab(tab) { showWorkspace(tab==='operations'?'pricing':'model',true); }
  async function setShopEnabled(shop,input,state) {
    input.disabled=true;
    try {
      const response=await v4Fetch(`/api/plugin/shops/${encodeURIComponent(shop.shop_id)}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:input.checked})});
      const data=await response.json(); if(!response.ok)throw new Error(data?.detail||'店铺开关保存失败');
      state.textContent=data.shop.enabled?'自动回复与改价已开启':'自动化已关闭'; state.classList.toggle('disabled',!data.shop.enabled);
    } catch(error) { input.checked=!input.checked; state.textContent=error.message; state.classList.add('disabled'); }
    finally { input.disabled=false; }
  }
  async function setShopMode(shop,select) {
    const previous=shop.automation_mode||'hybrid'; select.disabled=true;
    try {
      const response=await v4Fetch(`/api/plugin/shops/${encodeURIComponent(shop.shop_id)}/automation-mode`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:select.value})});
      const data=await response.json(); if(!response.ok)throw new Error(data?.detail||'自动化模式保存失败');
      shop.automation_mode=data.mode; select.value=data.mode; settingsMessage(`店铺模式已切换为 ${data.mode}，下一条消息生效。`);
    } catch(error) { select.value=previous; settingsMessage(error.message,true); }
    finally { select.disabled=false; }
  }
  function renderShops(shops) {
    const root=$('shopList');root.replaceChildren();
    if(!shops.length){root.append(element('div','diagnostic-empty','当前租户暂未同步到店铺。收到下一条平台事件后会自动同步，也可稍后刷新。'));return;}
    shops.forEach(shop=>{
      const row=element('div','shop-row');const copy=element('div','shop-copy');copy.append(element('strong','',shop.shop_name||shop.shop_id),element('small','',`店铺标识 ${shop.shop_id}`));
      const controls=element('div','shop-controls');const mode=document.createElement('select');mode.className='shop-mode-select';[['rules','规则执行'],['hybrid','规则 + Agent'],['agent','Agent 执行（含门禁）'],['full','Agent 完整编排（含门禁）']].forEach(([value,label])=>{const option=document.createElement('option');option.value=value;option.textContent=label;mode.append(option)});mode.value=shop.automation_mode||'hybrid';const state=element('span',`shop-state${shop.enabled?'':' disabled'}`,shop.enabled?'自动回复与改价已开启':'自动化已关闭');const label=element('label','switch');const input=document.createElement('input');input.type='checkbox';input.checked=Boolean(shop.enabled);const slider=element('span','slider');label.append(input,slider);controls.append(mode,state,label);row.append(copy,controls);root.append(row);input.addEventListener('change',()=>setShopEnabled(shop,input,state));mode.addEventListener('change',()=>setShopMode(shop,mode));
    });
  }
  async function loadShops(sync=false) {
    const root=$('shopList');root.replaceChildren(element('div','diagnostic-empty',sync?'正在从鱼麦多同步店铺…':'正在读取店铺…'));
    try {
      if(sync){
        await fishMoreReady;
        const synced=await gatewayFetch('ui/api/shops/sync',{method:'POST'});
        const syncData=await synced.json();
        if(!synced.ok)throw new Error(syncData?.error||'鱼麦多店铺同步失败');
      }
      const response=await v4Fetch('/api/plugin/shops');const data=await response.json();if(!response.ok)throw new Error(data?.detail||'读取店铺失败');renderShops(data.shops||[]);
    }
    catch(error){root.replaceChildren(element('div','diagnostic-empty',error.message));}
  }

  const RECORD_PAGE_SIZE=20;
  let quoteRecords=[]; let quotePage=1; let liangpiaoQuoteRecords=[]; let liangpiaoPage=1;let activeQuoteSource='wanda';
  function showQuoteSource(source){activeQuoteSource=source==='liangpiao'?'liangpiao':'wanda';$('wandaQuoteSection').hidden=activeQuoteSource!=='wanda';$('liangpiaoQuoteSection').hidden=activeQuoteSource!=='liangpiao';[['showWandaQuotes','wanda'],['showLiangpiaoQuotes','liangpiao']].forEach(([id,value])=>{const button=$(id);const active=value===activeQuoteSource;button.classList.toggle('active',active);button.setAttribute('aria-selected',String(active));});}
  let loadedOrderRows=[]; let linkedOrderFacts=new Map(); let observedOrderCount=0; let activeOrderFilter='all'; let orderSourceWarning=''; let orderPage=1;
  function recordFact(label,value,className='') { const node=element('div',`record-fact ${className}`.trim());node.append(element('span','',label),element('b','',value||'—'));return node; }
  function formatRecordTime(value) { const parsed=Date.parse(String(value||''));return Number.isFinite(parsed)?new Date(parsed).toLocaleString('zh-CN',{hour12:false}):'—'; }
  function renderPagination(id,page,total,onPage) {
    const root=$(id); root.replaceChildren(); const pages=Math.max(1,Math.ceil(total/RECORD_PAGE_SIZE));
    if(total<=RECORD_PAGE_SIZE)return;
    const previous=element('button','pagination-button','上一页'); previous.type='button'; previous.disabled=page<=1; previous.addEventListener('click',()=>onPage(page-1));
    const next=element('button','pagination-button','下一页'); next.type='button'; next.disabled=page>=pages; next.addEventListener('click',()=>onPage(page+1));
    root.append(previous,element('span','pagination-info',`第 ${page} / ${pages} 页 · 共 ${total} 条`),next);
  }
  function recordPrimary(record) {
    const primary=element('div','record-primary');
    primary.append(element('strong','',`${record.city?`${record.city} · `:''}${record.cinema||'影院待确认'}`),element('small','',`${record.movie||'影片待确认'} · ${record.quote_date||record.date_text||'日期待确认'} ${record.showtime_start||''}\n${record.seat_display||record.seat_zone_type||'座位待确认'}`));
    return primary;
  }
  function offerLabel(offer) { const mode=String(offer.price_mode||offer.offer_id||'报价').toUpperCase();return `${mode} · ${centsAmount(offer.total_quote_cents)}`; }
  async function selectRecordOffer(record,offerId,button) {
    if(!window.confirm('请确认：买家已明确选择该报价档位。此操作会写入报价审计记录。'))return;
    button.disabled=true;
    try { const response=await v4Fetch(`/api/plugin/quote-records/${encodeURIComponent(record.record_id)}/selected-offer`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({offer_id:offerId})});const data=await response.json();if(!response.ok)throw new Error(data?.detail||'报价档位选择失败');quoteRecords=quoteRecords.map(item=>item.record_id===data.record_id?data:item);renderQuoteRecords();$('quoteRecordSummary').textContent='已完成权威重新预检，请让买家确认刷新后的最终金额。'; }
    catch(error){$('quoteRecordSummary').textContent=error.message;button.disabled=false;}
  }
  function appendOfferSelector(record,card) {
    const offers=Array.isArray(record.offers)?record.offers.filter(offer=>offer&&offer.offer_id):[];
    if(offers.length<2)return;
    const panel=element('div','offer-selector');const title=element('div','offer-selector-title');const awaitingConfirmation=record.selected_offer_id&&!record.confirmation_id;title.append(element('b','',record.selected_offer_id?'已选择报价档位':'等待买家选择报价档位'),element('small','',awaitingConfirmation?'已重新预检，等待买家确认最终金额':record.selection_source?`选择来源：${record.selection_source}`:'多档报价必须明确选择后才能改价或出票'));
    const controls=element('div','offer-selector-controls');const select=document.createElement('select');offers.forEach(offer=>{const option=document.createElement('option');option.value=offer.offer_id;option.textContent=offerLabel(offer);select.append(option)});select.value=record.selected_offer_id||offers[0].offer_id;const button=element('button','inline-setting-button',record.selected_offer_id?'更新选择':'确认买家选择');button.type='button';const expired=Number.isFinite(Date.parse(String(record.quote_expires_at||'')))&&Date.parse(record.quote_expires_at)<=Date.now();button.disabled=expired;button.title=expired?'报价已过期，不能选择':'';button.addEventListener('click',()=>selectRecordOffer(record,select.value,button));controls.append(select,button);panel.append(title,controls);card.append(panel);
  }
  function renderWandaRecord(record,root) {
    const card=element('article','record-card'); card.append(recordPrimary(record));
    if(record.status==='failed'){
      const status=recordFact('核价状态','失败'); status.querySelector('b').classList.add('record-status','record-status-failed');
      card.append(status,recordFact('失败原因',record.failure_reason||'未取得权威报价','wide'),recordFact('买家 / 买家ID',record.buyer_id));
    } else {
      card.append(recordFact('原价',centsAmount(record.original_unit_price_cents)),recordFact('会员价',centsAmount(record.member_unit_price_cents)),recordFact('报价单价',centsAmount(record.unit_quote_cents)),recordFact('报价合计',centsAmount(record.total_quote_cents)),recordFact('买家 / 买家ID',record.buyer_id));
    }
    appendOfferSelector(record,card);root.append(card);
  }
  function renderLiangpiaoRecord(record,root) {
    const card=element('article','record-card liangpiao-record-card'); card.append(recordPrimary(record));
    card.append(recordFact('原价',centsAmount(record.market_amount_fen)),recordFact(record.price_mode_label||'良票预估价',centsAmount(record.provider_base_amount_fen)),recordFact('报价单价',centsAmount(record.buyer_unit_amount_fen)),recordFact('报价合计',centsAmount(record.buyer_amount_fen)),recordFact('买家 / 买家ID',record.buyer_id));
    root.append(card);
  }
  function renderQuoteRecords() {
    const records=quoteRecords; const root=$('quoteRecordList'); root.replaceChildren(); const failed=records.filter(record=>record.status==='failed').length;
    const pages=Math.max(1,Math.ceil(records.length/RECORD_PAGE_SIZE)); quotePage=Math.min(quotePage,pages);
    const visible=records.slice((quotePage-1)*RECORD_PAGE_SIZE,quotePage*RECORD_PAGE_SIZE);
    $('quoteRecordSummary').textContent=`共 ${records.length} 条核价记录 · 成功 ${records.length-failed} 条 · 失败 ${failed} 条 · 当前 ${visible.length} 条`;$('showWandaQuotes').textContent=`万达报价（${records.length}）`;$('showLiangpiaoQuotes').textContent=`良票报价（${liangpiaoQuoteRecords.length}）`;showQuoteSource(activeQuoteSource);
    if(!records.length)root.append(element('div','diagnostic-empty','暂无万达报价记录。'));
    else visible.forEach(record=>renderWandaRecord(record,root));
    renderPagination('quoteRecordPagination',quotePage,records.length,next=>{quotePage=next;renderQuoteRecords();});

    const liangpiaoRoot=$('liangpiaoQuoteRecordList'); liangpiaoRoot.replaceChildren();
    const liangpiaoPages=Math.max(1,Math.ceil(liangpiaoQuoteRecords.length/RECORD_PAGE_SIZE)); liangpiaoPage=Math.min(liangpiaoPage,liangpiaoPages);
    const liangpiaoVisible=liangpiaoQuoteRecords.slice((liangpiaoPage-1)*RECORD_PAGE_SIZE,liangpiaoPage*RECORD_PAGE_SIZE);
    if(!liangpiaoQuoteRecords.length)liangpiaoRoot.append(element('div','diagnostic-empty','暂无良票报价记录。'));
    else liangpiaoVisible.forEach(record=>renderLiangpiaoRecord(record,liangpiaoRoot));
    renderPagination('liangpiaoQuoteRecordPagination',liangpiaoPage,liangpiaoQuoteRecords.length,next=>{liangpiaoPage=next;renderQuoteRecords();});
  }
  async function loadQuoteRecords() {
    $('quoteRecordList').replaceChildren(element('div','diagnostic-empty','正在读取报价记录…')); $('liangpiaoQuoteRecordList').replaceChildren(element('div','diagnostic-empty','正在读取报价记录…')); $('quoteRecordPagination').replaceChildren(); $('liangpiaoQuoteRecordPagination').replaceChildren();
    try{const response=await v4Fetch('/api/plugin/quote-records?limit=500');const data=await response.json();if(!response.ok)throw new Error(data?.detail||'读取报价记录失败');quoteRecords=Array.isArray(data.records)?data.records:[];liangpiaoQuoteRecords=Array.isArray(data.liangpiao_records)?data.liangpiao_records:[];quotePage=1;liangpiaoPage=1;renderQuoteRecords();}catch(error){$('quoteRecordSummary').textContent='读取失败';$('quoteRecordList').replaceChildren(element('div','diagnostic-empty',error.message));$('liangpiaoQuoteRecordList').replaceChildren(element('div','diagnostic-empty',error.message));}
  }
  function orderCategory(order){const status=String((order.status||order.orderStatus)??'').toLowerCase();const label=String(order.statusText||order.orderStatusText||'').toLowerCase();if(status==='1'||/待付款|等待付款|pending|wait_pay|created/.test(label)||status==='created')return'unpaid';if(status==='2'||/已付款|支付成功|paid/.test(label)||status==='paid')return'paid';if(status==='3'||/已发货|待收货|shipped|ticket_sent/.test(label)||['shipped','ticket_sent'].includes(status))return'shipped';if(['4','5','6','60','99','completed','cancelled','closed'].includes(status)||/完成|关闭|取消|退款|completed|closed|cancel|refund/.test(label))return'closed';return'other';}
  function linkedFactMap(records){const map=new Map();records.forEach(record=>{const orderId=String(record.order_id||'').trim();if(!orderId||record.status==='failed'||map.has(orderId))return;map.set(orderId,record)});return map;}
  function orderShowtime(row){return row?.showtime||'—';}
  function seatText(value){if(Array.isArray(value))return value.map(item=>item?.seatNo||item?.seat_no||`${item?.rowNo||item?.row_no||''}排${item?.colNo||item?.col_no||''}座`).filter(Boolean).join('、')||'—';return String(value||'—');}
  function shopNameMap(shops){const map=new Map();(shops||[]).forEach(shop=>{const id=String(shop.shop_id||shop.account_unb||shop.shopId||'').trim();const name=String(shop.shop_name||shop.shopName||'').trim();if(id&&name)map.set(id,name);});return map;}
  function wandaOrderRow(order,fact,shopNames=new Map()){const shopId=String(order.accountUnb||order.account_unb||'').trim();return{source:'wanda',shopName:shopNames.get(shopId)||order.shopName||order.shop_name||'—',detailId:String(order.orderId||''),orderId:String(order.orderId||''),movie:(order.fulfillment?.movie_name||fact?.movie||order.productTitle||order.sku||'—'),city:(order.fulfillment?.city||fact?.city||'—'),cinema:(order.fulfillment?.cinema_name||fact?.cinema||'—'),showtime:orderShowtime({showtime:order.fulfillment?.showtime_start?`${order.fulfillment.show_date?`${order.fulfillment.show_date} `:''}${order.fulfillment.showtime_start}${order.fulfillment.showtime_end?` - ${order.fulfillment.showtime_end}`:''}`:`${fact?.quote_date||fact?.date_text||''}${fact?.showtime_start?` ${fact.showtime_start}`:''}`.trim()}),seats:Array.isArray(order.fulfillment?.seats)&&order.fulfillment.seats.length?order.fulfillment.seats.join('、'):fact?.seat_display||fact?.seat_zone_type||'—',ticketMode:'手动出票',marketAmountFen:fact?.base_total_cents,quoteAmountFen:fact?.total_quote_cents,dealAmountFen:order.payment,status:order.orderStatusText||String(order.orderStatus??'未知'),statusText:order.orderStatusText||String(order.orderStatus??'未知'),createdAt:order.createTime||order.observedAt,buyerNick:order.buyerNick||'—',accountUnb:shopId,raw:order,fact,fulfillment:order.fulfillment||null};}
  function liangpiaoOrderRow(order,shopNames=new Map()){const shopId=String(order.shop_id||order.shopId||'').trim();return{source:'liangpiao',shopName:shopNames.get(shopId)||order.shopName||order.shop_name||'—',detailId:String(order.orderNo||order.order_no||order.provider_order_no||''),orderId:String(order.orderNo||order.order_no||order.provider_order_no||''),movie:order.movieName||'—',city:order.cityName||'—',cinema:order.cinemaName||'—',showtime:`${order.startTime||''}${order.endTime?` - ${order.endTime}`:''}`.trim()||'—',seats:seatText(order.seats),ticketMode:order.ticketMode||order.priceMode||'—',marketAmountFen:order.marketAmount,quoteAmountFen:order.quoteAmount,dealAmountFen:order.settleAmount??order.estimatedSettleAmount,status:order.status||'未知',statusText:order.status||'未知',createdAt:order.createdAt,buyerNick:order.buyer_nick||order.buyer_id||'—',outOrderNo:order.out_order_no,providerOrderNo:order.provider_order_no||order.orderNo,raw:order};}
  function orderMatches(row,query){if(!query)return true;return[row.shopName,row.orderId,row.outOrderNo,row.providerOrderNo,row.buyerNick,row.movie,row.city,row.cinema,row.showtime,row.seats,row.ticketMode,row.statusText].some(value=>String(value||'').toLowerCase().includes(query));}
  function renderOrders(){
    const query=String($('orderSearch').value||'').trim().toLowerCase();const visible=loadedOrderRows.filter(row=>(activeOrderFilter==='all'||orderCategory(row)===activeOrderFilter)&&orderMatches(row,query));const pages=Math.max(1,Math.ceil(visible.length/RECORD_PAGE_SIZE));orderPage=Math.min(orderPage,pages);const pageRows=visible.slice((orderPage-1)*RECORD_PAGE_SIZE,orderPage*RECORD_PAGE_SIZE);const root=$('orderList');root.replaceChildren();$('orderSummary').textContent=`共 ${loadedOrderRows.length} 笔订单 · 万达手动出票 ${loadedOrderRows.filter(row=>row.source==='wanda').length} 笔 · 良票 ${loadedOrderRows.filter(row=>row.source==='liangpiao').length} 笔 · 当前显示 ${pageRows.length} 笔${orderSourceWarning?` · ${orderSourceWarning}`:''}`;
    if(!visible.length){root.append(element('div','diagnostic-empty',loadedOrderRows.length?'没有符合当前筛选条件的订单。':'暂无可读取订单。'));renderPagination('orderPagination',1,0,()=>{});return;}
    const table=document.createElement('table');table.className='order-table';const headers=['来源店铺','影片','城市','影院','场次','座位','出票方式','票面价','报价','成交价','状态','下单时间','咸鱼买家','操作'];const thead=document.createElement('thead');const headRow=document.createElement('tr');headers.forEach(label=>headRow.append(element('th','',label)));thead.append(headRow);const tbody=document.createElement('tbody');pageRows.forEach(row=>{const tr=document.createElement('tr');const sourceCell=element('td','order-source-cell');sourceCell.append(element('strong','',row.shopName||'—'),element('small','',row.source==='liangpiao'?'良票':'万达'));tr.append(sourceCell);const values=[row.movie,row.city,row.cinema,row.showtime,row.seats,row.ticketMode,centsAmount(row.marketAmountFen),centsAmount(row.quoteAmountFen),centsAmount(row.dealAmountFen),row.statusText,formatRecordTime(row.createdAt),row.buyerNick];values.forEach((value,index)=>{const cell=element('td',index===9?'order-table-status':'',value);if(index===9)cell.classList.add(`order-status-${orderCategory(row)}`);tr.append(cell);});const actionCell=document.createElement('td');const detail=element('button','order-detail-button','详情');detail.type='button';detail.addEventListener('click',()=>openOrderDetail(row));actionCell.append(detail);tr.append(actionCell);tbody.append(tr);});table.append(thead,tbody);root.append(table);renderPagination('orderPagination',orderPage,visible.length,next=>{orderPage=next;renderOrders();});
  }
  let orderRefreshInFlight=false;
  async function loadOrders() {
    if(orderRefreshInFlight)return; orderRefreshInFlight=true;
    $('orderList').replaceChildren(element('div','diagnostic-empty','正在回读订单、报价和订单来源…'));orderSourceWarning='';
    try{await fishMoreReady;const[orderResponse,factResponse,liangpiaoResponse,shopsResponse]=await Promise.all([fishMoreSdk.authedFetch('ui/api/orders?limit=25'),v4Fetch('/api/plugin/quote-records?limit=500'),v4Fetch('/api/plugin/liangpiao-orders?page=1&page_size=100'),v4Fetch('/api/plugin/shops')]);const data=await orderResponse.json();const facts=await factResponse.json();const liangpiaoData=await liangpiaoResponse.json();const shopsData=await shopsResponse.json();if(!orderResponse.ok)throw new Error(data?.error||'读取万达订单失败');if(!factResponse.ok)throw new Error(facts?.detail||'读取订单观影信息失败');if(!liangpiaoResponse.ok)orderSourceWarning='良票订单暂不可用';if(!shopsResponse.ok)orderSourceWarning=orderSourceWarning||'店铺名称暂不可用';const shopNames=shopNameMap(shopsResponse.ok?shopsData.shops:[]);const factsByOrder=linkedFactMap(facts.records||[]);loadedOrderRows=(data.orders||[]).map(order=>wandaOrderRow(order,factsByOrder.get(String(order.orderId)),shopNames)).concat(liangpiaoResponse.ok?(liangpiaoData.orders||[]).map(order=>liangpiaoOrderRow(order,shopNames)):[]);loadedOrderRows.sort((a,b)=>String(b.createdAt||'').localeCompare(String(a.createdAt||'')));observedOrderCount=Number(data.observedCount)||0;orderPage=1;renderOrders();}catch(error){$('orderSummary').textContent='读取失败';$('orderList').replaceChildren(element('div','diagnostic-empty',error.message));}finally{orderRefreshInFlight=false;}
  }
  function safeMediaUrl(value) {
    const url=String(value||'').trim();
    return /^(https?:\/\/|data:image\/)/u.test(url)?url:'';
  }
  function detailPair(parent,label,value,{wide=false,copy=false}={}) {
    const text=Array.isArray(value)?seatText(value):String(value??'—');
    const row=element('div',`order-detail-field${wide?' wide':''}${copy&&text!=='—'?' copyable':''}`);
    row.append(element('span','',label));
    const valueNode=element('b','',text); row.append(valueNode);
    if(copy&&text!=='—') { const button=element('button','order-copy','□'); button.type='button'; button.title='复制'; button.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(text);button.textContent='✓';setTimeout(()=>{button.textContent='□'},1200)}catch{}}); valueNode.append(button); }
    parent.append(row);
  }
  function detailValue(detail,row,...keys) {
    const fulfillment=detail?.fulfillment||row?.fulfillment||{};
    const aliases={movieName:'movie_name',cinemaName:'cinema_name',startTime:'showtime_start',endTime:'showtime_end',hallName:'hall_name',ticketCode:'ticket_codes',ticketUrl:'ticket_url'};
    for(const key of keys) { if(detail?.[key]!==undefined&&detail?.[key]!==null&&detail?.[key]!=='')return detail[key]; if(row?.[key]!==undefined&&row?.[key]!==null&&row?.[key]!=='')return row[key]; const issued=fulfillment[aliases[key]||key]; if(issued!==undefined&&issued!==null&&issued!=='')return key==='ticketCode'&&Array.isArray(issued)?issued[0]:issued; }
    return '—';
  }
  function renderOrderDetail(title,row,detail) {
    const root=$('orderDetailContent'); root.replaceChildren(); $('orderDetailTitle').textContent='订单详情';
    const movieName=detailValue(detail,row,'movieName','movie_name','movie');
    const cinemaName=detailValue(detail,row,'cinemaName','cinema_name','cinema');
    const showtime=detail.startTime?`${detail.startTime}${detail.endTime?` - ${detail.endTime}`:''}`:detailValue(detail,row,'showtime');
    const seats=detailValue(detail,row,'seats','seat_display','seat_zone_type');
    const poster=safeMediaUrl(detailValue(detail,row,'moviePoster','movie_poster','poster'));
    const movieCard=element('section','order-detail-summary-card');
    const posterBox=element('div','order-detail-poster');
    if(poster) { const image=document.createElement('img'); image.src=poster; image.alt=String(movieName); posterBox.append(image); } else posterBox.append(element('div','order-detail-poster-empty','电影海报'));
    const movieBody=element('div','order-detail-summary-body'); movieBody.append(element('div','order-detail-summary-title',movieName));
    const movieGrid=element('div','order-detail-summary-grid');
    detailPair(movieGrid,'电影名称',movieName,{copy:true}); detailPair(movieGrid,'座位信息',seats,{copy:true});
    detailPair(movieGrid,'开场时间',showtime); detailPair(movieGrid,'原价',centsAmount(detailValue(detail,row,'marketAmount','marketAmountFen','base_total_cents')));
    detailPair(movieGrid,'影院名称',cinemaName,{copy:true}); detailPair(movieGrid,'票数',detailValue(detail,row,'ticketCount','quantity'));
    detailPair(movieGrid,'影院地址',detailValue(detail,row,'cinemaAddress','cinema_address'),{wide:true,copy:true});
    movieBody.append(movieGrid); movieCard.append(posterBox,movieBody); root.append(movieCard);

    const orderCard=element('section','order-detail-summary-card order-card-wide');
    const orderGrid=element('div','order-detail-summary-grid');
    detailPair(orderGrid,'平台订单号',detailValue(detail,row,'orderNo','order_no','providerOrderNo','provider_order_no','orderId'),{copy:true});
    detailPair(orderGrid,'下单时间',formatRecordTime(detailValue(detail,row,'createdAt','createTime')));
    detailPair(orderGrid,'出票价',centsAmount(detailValue(detail,row,'quoteAmount','quote_amount_fen','quoteAmountFen')));
    detailPair(orderGrid,'出票原座位',seats,{copy:true});
    detailPair(orderGrid,'订单状态',detailValue(detail,row,'statusText','status','orderStatusText'));
    detailPair(orderGrid,'成交价',centsAmount(detailValue(detail,row,'settleAmount','estimatedSettleAmount','dealAmountFen','payment')));
    detailPair(orderGrid,'取票链接',detailValue(detail,row,'pickupUrl','pickup_url'),{wide:true,copy:true});
    detailPair(orderGrid,'结算状态',detailValue(detail,row,'settlementStatus','settleStatus','statusText'));
    orderCard.append(orderGrid); root.append(orderCard);

    const ticketTitle=element('div','order-detail-section-title','取票信息'); root.append(ticketTitle);
    const ticket=Array.isArray(detail.tickets)&&detail.tickets.length?detail.tickets[0]:detail;
    const ticketViewer=element('section','order-ticket-viewer');
    const ticketHead=element('div','order-ticket-head'); ticketHead.append(element('h3','',`取票凭证${detail.tickets?.length?`（共 ${detail.tickets.length} 张）`:''}`));
    const tabs=element('div','order-ticket-tabs'); tabs.append(element('button','order-ticket-tab active','原取票码'),element('button','order-ticket-tab','模板取票码')); ticketHead.append(tabs); ticketViewer.append(ticketHead);
    const ticketBody=element('div','order-ticket-body'); const imageWrap=element('div','order-ticket-image-wrap');
    const ticketImage=safeMediaUrl(detailValue(ticket,row,'ticketImage','ticket_image','qrCode','qr_code','ticketLink'));
    if(ticketImage) { const image=document.createElement('img'); image.className='order-ticket-image'; image.src=ticketImage; image.alt='取票凭证'; imageWrap.append(image); } else imageWrap.append(element('div','order-ticket-empty','暂无取票图片'));
    const ticketFacts=element('div','order-ticket-facts');
    detailPair(ticketFacts,'取票码',detailValue(ticket,row,'ticketCode','ticket_code'),{copy:true}); detailPair(ticketFacts,'验证码',detailValue(ticket,row,'ticketPassword','ticket_password'),{copy:true});
    detailPair(ticketFacts,'入场方式',ticket.entryType===1?'扫码入场':ticket.entryType===0?'取票入场':detailValue(ticket,row,'entryType'));
    detailPair(ticketFacts,'版本号',detailValue(ticket,row,'version'));
    ticketBody.append(imageWrap,ticketFacts); ticketViewer.append(ticketBody); root.append(ticketViewer);
  }
  async function openOrderDetail(row){$('orderDetailTitle').textContent='正在读取订单详情…';$('orderDetailContent').replaceChildren(element('div','diagnostic-empty','正在读取权威订单详情…'));$('orderDetailDialog').showModal();try{if(row.source==='wanda'){await fishMoreReady;const response=await fishMoreSdk.authedFetch(`ui/api/orders/${encodeURIComponent(row.orderId)}`);const data=await response.json();if(!response.ok)throw new Error(data?.error||'读取万达订单详情失败');renderOrderDetail(`订单详情 · ${row.orderId}`,row,data.order||row.raw);}else{const response=await v4Fetch(`/api/plugin/liangpiao-orders/${encodeURIComponent(row.providerOrderNo)}`);const data=await response.json();if(!response.ok)throw new Error(data?.detail||'读取良票订单详情失败');renderOrderDetail(`订单详情 · ${row.providerOrderNo}`,row,data.order||{});}}catch(error){$('orderDetailContent').replaceChildren(element('div','diagnostic-empty',error.message));}}

  function reminderStatusLabel(status){return({pending:'待执行',claimed:'执行中',completed:'已完成',missed:'已错过',failed:'失败'})[status]||status||'未知';}
  let reminderLoaded=false;
  async function loadReminderSettings(){
    reminderLoaded=false; $('reminderEnabled').disabled=true;
    try{const response=await v4Fetch('/api/settings/reminders');const data=await response.json();if(!response.ok)throw new Error(data?.detail||'读取提醒设置失败');$('reminderEnabled').checked=Boolean(data.enabled);$('reminderEnabledText').textContent=data.enabled?'已开启':'已关闭';$('preShowMinutes').value=data.pre_show_minutes;$('postShowMinutes').value=data.post_show_minutes;$('preShowTemplate').value=data.pre_show_template||'';$('reminderMeta').textContent=`修订 ${data.revision||0}${data.updated_at?` · ${new Date(data.updated_at).toLocaleString('zh-CN')}`:''}`;reminderLoaded=true; $('reminderEnabled').disabled=false;}catch(error){$('reminderMeta').textContent=error.message;}
  }
  async function saveReminderSettings(){
    const button=$('saveReminders');button.disabled=true;
    try{const payload={enabled:$('reminderEnabled').checked,pre_show_minutes:Number($('preShowMinutes').value),post_show_minutes:Number($('postShowMinutes').value),pre_show_template:$('preShowTemplate').value.trim()};const response=await v4Fetch('/api/settings/reminders',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await response.json();if(!response.ok)throw new Error(data?.detail||'保存提醒设置失败');$('reminderEnabledText').textContent=data.enabled?'已开启':'已关闭';$('reminderMeta').textContent=`已保存 · 修订 ${data.revision} · ${new Date(data.updated_at).toLocaleString('zh-CN')}`;}catch(error){$('reminderMeta').textContent=error.message;}finally{button.disabled=false;}
  }
  async function loadReminderTasks(){
    try{const response=await v4Fetch('/api/plugin/reminders?limit=200');const data=await response.json();if(!response.ok)throw new Error(data?.detail||'读取提醒任务失败');const tasks=data.tasks||[];const counts={};tasks.forEach(task=>{counts[task.status]=(counts[task.status]||0)+1});$('reminderTaskSummary').textContent=tasks.length?`提醒任务 ${tasks.length} 条 · ${Object.entries(counts).map(([status,count])=>`${reminderStatusLabel(status)} ${count}`).join(' · ')}`:'暂无提醒任务；开启后会在订单发货且观影事实完整时自动创建。';}catch(error){$('reminderTaskSummary').textContent=error.message;}
  }

  const knowledgeCategories=['售后人工','下单确认','订单出票','报价规则','截图识别','异常处理','购票流程','常见问题']; let knowledgeEntries=[];
  function knowledgeField(parent,label,value,type='textarea'){const wrap=element('label','',label);const input=document.createElement(type==='input'?'input':'textarea');input.value=value||'';input.maxLength=type==='input'?120:4000;wrap.append(input);parent.append(wrap);return input;}
  function renderKnowledge(){const root=$('knowledgeList');root.replaceChildren();const filter=$('knowledgeCategoryFilter').value;const entries=knowledgeEntries.filter(item=>!filter||item.category===filter);$('knowledgeTotal').textContent=String(knowledgeEntries.length);$('knowledgeEnabled').textContent=String(knowledgeEntries.filter(item=>item.enabled).length);$('knowledgeCategories').textContent=String(new Set(knowledgeEntries.map(item=>item.category)).size);if(!entries.length){root.append(element('div','diagnostic-empty','暂无匹配知识条目。'));return;}entries.forEach(item=>{const detail=element('details','knowledge-entry');const summary=document.createElement('summary');const title=element('span');title.append(element('b','',item.title),element('small','',`${item.category} · ${item.enabled?'启用':'停用'}`));summary.append(title,element('i',`knowledge-badge ${item.enabled?'retained':'blocked'}`,item.enabled?'启用':'停用'));detail.append(summary);const form=element('div','knowledge-edit');const titleInput=knowledgeField(form,'标题',item.title,'input');const categoryLabel=element('label','', '分类');const category=document.createElement('select');knowledgeCategories.forEach(value=>{const option=document.createElement('option');option.value=value;option.textContent=value;option.selected=value===item.category;category.append(option)});categoryLabel.append(category);form.append(categoryLabel);const questions=knowledgeField(form,'买家常见问法',item.common_questions);const guidance=knowledgeField(form,'回复口径',item.reply_guidance);const rules=knowledgeField(form,'处理规则',item.handling_rules);const enabledLabel=element('label');const enabled=document.createElement('input');enabled.type='checkbox';enabled.checked=Boolean(item.enabled);enabledLabel.append(enabled,document.createTextNode(' 启用此知识条目'));form.append(enabledLabel);const actions=element('div','knowledge-actions');const save=element('button','', '保存');const remove=element('button','', '删除');actions.append(save,remove);form.append(actions);detail.append(form);root.append(detail);save.addEventListener('click',async()=>{save.disabled=true;try{const response=await v4Fetch(`/api/settings/knowledge/${encodeURIComponent(item.id)}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:titleInput.value,category:category.value,common_questions:questions.value,reply_guidance:guidance.value,handling_rules:rules.value,enabled:enabled.checked})});const data=await response.json();if(!response.ok)throw new Error(data?.detail||'知识保存失败');knowledgeEntries=knowledgeEntries.map(value=>value.id===item.id?data:value);renderKnowledge()}catch(error){settingsMessage(error.message,true)}finally{save.disabled=false}});remove.addEventListener('click',async()=>{if(!window.confirm(`确定删除“${item.title}”吗？`))return;remove.disabled=true;try{const response=await v4Fetch(`/api/settings/knowledge/${encodeURIComponent(item.id)}`,{method:'DELETE'});if(!response.ok)throw new Error('知识删除失败');knowledgeEntries=knowledgeEntries.filter(value=>value.id!==item.id);renderKnowledge()}catch(error){settingsMessage(error.message,true)}finally{remove.disabled=false}});});}
  async function loadKnowledge(){try{const response=await v4Fetch('/api/settings/knowledge');const data=await response.json();if(!response.ok)throw new Error(data?.detail||'读取知识库失败');knowledgeEntries=Array.isArray(data.entries)?data.entries:[];renderKnowledge()}catch(error){$('knowledgeList').replaceChildren(element('div','diagnostic-empty',error.message));}}
  async function addKnowledge(){const response=await v4Fetch('/api/settings/knowledge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:'新知识条目',category:'常见问题',common_questions:'请填写买家常见问法。',reply_guidance:'请填写统一回复口径。',handling_rules:'请填写处理规则。',enabled:false})});const data=await response.json();if(!response.ok){settingsMessage(data?.detail||'新增知识失败',true);return}knowledgeEntries.push(data);renderKnowledge();document.querySelector('#knowledgeList details:last-child')?.setAttribute('open','');}
  const templateFields=Object.freeze({recognition_failure_other_template:'recognitionFailureOtherTemplate',recognition_template:'recognitionTemplate',cinema_match_failure_template:'cinemaMatchFailureTemplate',unsupported_cinema_template:'unsupportedCinemaTemplate',missing_fields_template:'missingFieldsTemplate',wplus_quote_marker_template:'wplusQuoteMarkerTemplate',wplus_unit_price_reply_template:'wplusUnitPriceReplyTemplate',wplus_marker_confirmation_template:'wplusMarkerConfirmationTemplate',wplus_marker_missing_template:'wplusMarkerMissingTemplate',wplus_marker_confirmed_template:'wplusMarkerConfirmedTemplate',showtime_changed_template:'showtimeChangedTemplate',exact_quote_template:'exactQuoteTemplate',area_quote_template:'areaQuoteTemplate',quote_unavailable_template:'quoteUnavailableTemplate',same_type_unavailable_template:'sameTypeUnavailableTemplate',quote_expired_template:'quoteExpiredTemplate',no_quote_template:'noQuoteTemplate',guidance_template:'guidanceTemplate',payment_success_pending_ticket_template:'paymentSuccessPendingTicketTemplate',order_shipped_template:'orderShippedTemplate',liangpiao_ticketed_template:'liangpiaoTicketedTemplate',liangpiao_failed_template:'liangpiaoFailedTemplate',movie_reminder_template:'movieReminderTemplate',order_pending_without_quote_template:'orderPendingWithoutQuoteTemplate',price_change_confirmation_template:'priceChangeConfirmationTemplate',price_change_failure_template:'priceChangeFailureTemplate',quote_confirmation_clarify_template:'quoteConfirmationClarifyTemplate',quote_ticket_count_request_template:'quoteTicketCountRequestTemplate',quote_quantity_order_guidance_template:'quoteQuantityOrderGuidanceTemplate',quote_quantity_marked_seat_template:'quoteQuantityMarkedSeatTemplate',quote_quantity_flexible_seat_template:'quoteQuantityFlexibleSeatTemplate',quote_quantity_default_seat_template:'quoteQuantityDefaultSeatTemplate',payment_manual_review_template:'paymentManualReviewTemplate',manual_review_template:'manualReviewTemplate',ai_disabled_structured_intake_template:'aiDisabledStructuredIntakeTemplate',order_before_quote_confirmation_template:'orderBeforeQuoteConfirmationTemplate',paid_mismatch_closed_template:'paidMismatchClosedTemplate',paid_mismatch_refund_template:'paidMismatchRefundTemplate'});
  const TEMPLATE_MESSAGE_SEPARATOR='---分隔符---'; let activeTemplateTextarea=null;
  function insertMessageSeparator(){const target=activeTemplateTextarea||document.querySelector('.template-section:not([hidden]) textarea');if(!target){templateMessage('请先打开一条回复话术。',true);return;}target.focus();const start=Number.isInteger(target.selectionStart)?target.selectionStart:target.value.length;const end=Number.isInteger(target.selectionEnd)?target.selectionEnd:start;target.setRangeText(TEMPLATE_MESSAGE_SEPARATOR,start,end,'end');templateMessage('已插入分隔发送符：前后内容会分成两条消息发送。');}
  let keywordRules=[];
  function keywordRuleId(){return`rule-${Date.now().toString(36)}-${Math.random().toString(36).slice(2,8)}`;}
  async function uploadKeywordRuleImage(rule,file,status){
    status.textContent='正在上传图片…';
    const form=new FormData();form.append('image',file);
    try{const response=await v4Fetch('/api/settings/reply-keyword-images',{method:'POST',body:form});const data=await response.json();if(!response.ok)throw new Error(data?.detail||'图片上传失败');rule.image_asset_id=data.asset_id;rule.image_filename=data.filename;status.textContent=`已上传：${data.filename}`;templateMessage('图片已上传，请点击“保存回复话术”使规则生效。');}
    catch(error){status.textContent=error.message;status.classList.add('error-text');}
  }
  function renderKeywordRules(){
    const root=$('keywordRuleList');root.replaceChildren();$('keywordRuleCount').textContent=String(keywordRules.length);
    if(!keywordRules.length){root.append(element('div','diagnostic-empty','暂无关键词规则。可添加“W+怎么买”“营业时间”等确定性回复。'));return;}
    keywordRules.forEach((rule,index)=>{
      const card=element('article','keyword-rule');const top=element('div','keyword-rule-top');const enabled=document.createElement('input');enabled.type='checkbox';enabled.checked=rule.enabled!==false;enabled.setAttribute('aria-label','启用关键词规则');const title=element('strong','',`规则 ${index+1}`);const remove=element('button','keyword-remove','删除');remove.type='button';top.append(enabled,title,remove);
      const grid=element('div','keyword-grid');const keywords=document.createElement('input');keywords.value=(rule.keywords||[]).join('，');keywords.placeholder='关键词，多个用逗号分隔';const mode=document.createElement('select');mode.innerHTML='<option value="contains">包含匹配</option><option value="exact">完全匹配</option>';mode.value=rule.match_mode||'contains';const priority=document.createElement('input');priority.type='number';priority.min='0';priority.max='10000';priority.value=String(rule.priority??100);const reply=document.createElement('textarea');reply.value=rule.reply||'';reply.maxLength=1000;reply.placeholder='命中关键词后发送的固定回复';
      const imageRow=element('div','keyword-image-row');const imageInput=document.createElement('input');imageInput.type='file';imageInput.accept='image/jpeg,image/png,image/webp,image/gif';const imageStatus=element('span','keyword-image-status',rule.image_filename?`已上传：${rule.image_filename}`:'可选：上传命中后发送的图片');const clearImage=element('button','ghost tiny','移除图片');clearImage.type='button';clearImage.hidden=!rule.image_asset_id;imageRow.append(imageInput,imageStatus,clearImage);
      grid.append(keywords,mode,priority,reply,imageRow);card.append(top,grid);root.append(card);
      enabled.addEventListener('change',()=>{rule.enabled=enabled.checked});keywords.addEventListener('input',()=>{rule.keywords=keywords.value.split(/[，,]/u).map(value=>value.trim()).filter(Boolean)});mode.addEventListener('change',()=>{rule.match_mode=mode.value});priority.addEventListener('input',()=>{rule.priority=Math.max(0,Math.min(10000,Number(priority.value)||0))});reply.addEventListener('input',()=>{rule.reply=reply.value});imageInput.addEventListener('change',async()=>{const file=imageInput.files?.[0];if(!file)return;await uploadKeywordRuleImage(rule,file,imageStatus);clearImage.hidden=!rule.image_asset_id;imageInput.value='';});clearImage.addEventListener('click',()=>{rule.image_asset_id=null;rule.image_filename=null;rule.image_tenant_id=null;imageStatus.textContent='图片已移除，保存后生效';clearImage.hidden=true;});remove.addEventListener('click',()=>{keywordRules.splice(index,1);renderKeywordRules()});
    });
  }
  function addKeywordRule(){if(keywordRules.length>=30){templateMessage('关键词规则最多30条。',true);return;}keywordRules.push({id:keywordRuleId(),keywords:[],match_mode:'contains',reply:'',enabled:true,priority:100});renderKeywordRules();}
  function templateMessage(message,isError=false){$('templateSaveMessage').textContent=message;$('templateSaveMessage').classList.toggle('error-text',isError);}
  async function copyTemplateVariable(value){
    if(navigator.clipboard&&window.isSecureContext){try{await navigator.clipboard.writeText(value);return;}catch{/* FishMore iframe may not grant clipboard-write. */}}
    const proxy=document.createElement('textarea');proxy.value=value;proxy.readOnly=true;proxy.className='clipboard-proxy';document.body.append(proxy);proxy.select();proxy.setSelectionRange(0,value.length);const copied=document.execCommand('copy');proxy.remove();if(!copied)throw new Error('copy_not_supported');
  }
  async function loadTemplates() {
    templateMessage('正在读取回复话术…');
    try { const response=await v4Fetch('/api/settings/reply-templates');const data=await response.json();if(!response.ok)throw new Error(data?.detail||'读取话术失败');Object.entries(templateFields).forEach(([key,id])=>{$(id).value=data[key]||''});keywordRules=Array.isArray(data.keyword_replies)?data.keyword_replies.map(rule=>({...rule,keywords:[...(rule.keywords||[])]})):[];renderKeywordRules();$('templateRevision').textContent=`话术修订 ${data.revision||0}${data.updated_at?` · ${new Date(data.updated_at).toLocaleString('zh-CN')}`:''}`;templateMessage(''); }
    catch(error){templateMessage(error.message,true);}
  }
  async function saveTemplates() {
    const button=$('saveTemplates');button.disabled=true;templateMessage('正在校验中文变量并保存…');
    try { const payload=Object.fromEntries(Object.entries(templateFields).map(([key,id])=>[key,$(id).value.trim()]));payload.keyword_replies=keywordRules.map(rule=>({...rule,keywords:(rule.keywords||[]).map(value=>value.trim()).filter(Boolean),reply:String(rule.reply||'').trim()}));const response=await v4Fetch('/api/settings/reply-templates',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await response.json();if(!response.ok)throw new Error(data?.detail||'读取并保存关键词规则失败');keywordRules=data.keyword_replies||[];renderKeywordRules();$('templateRevision').textContent=`话术修订 ${data.revision} · ${new Date(data.updated_at).toLocaleString('zh-CN')}`;templateMessage('回复话术和关键词规则已保存，下一条消息立即使用。'); }
    catch(error){templateMessage(error.message,true);}finally{button.disabled=false;}
  }

  function createLiangpiaoRuleRow({min=0,max=0,markup=0,min_discount_percent,max_discount_percent,markup_percent}={}) {
    const row=element('div','liangpiao-rule-row'); row.dataset.ruleRow='true';
    const lowerValue=min_discount_percent ?? min; const upperValue=max_discount_percent ?? max; const markupValue=markup_percent ?? markup;
    const lower=document.createElement('input'); lower.className='liangpiao-min'; lower.type='number'; lower.min='0'; lower.max='100'; lower.step='0.1'; lower.value=String(lowerValue); lower.setAttribute('aria-label','折扣率下限');
    const upper=document.createElement('input'); upper.className='liangpiao-max'; upper.type='number'; upper.min='0'; upper.max='100'; upper.step='0.1'; upper.value=String(upperValue); upper.setAttribute('aria-label','折扣率上限');
    const adjustment=document.createElement('input'); adjustment.className='liangpiao-markup'; adjustment.type='number'; adjustment.min='-100'; adjustment.max='1000'; adjustment.step='0.1'; adjustment.value=String(markupValue); adjustment.setAttribute('aria-label','调整比例');
    const remove=element('button','liangpiao-remove','删除'); remove.type='button'; remove.addEventListener('click',()=>row.remove());
    row.append(element('span','', '折扣率'),lower,element('span','', '% 至'),upper,element('span','', '%，加价'),adjustment,element('span','', '%'),remove);
    return row;
  }
  function initLiangpiaoRules() {
    for(const [listId,addId] of [['liangpiaoRuleList','addLiangpiaoRule'],['liangpiaoFixedRuleList','addLiangpiaoFixedRule']]){
      const list=$(listId); const add=$(addId); if(!list||!add)continue;
      list.querySelectorAll('.liangpiao-remove').forEach(button=>button.addEventListener('click',()=>button.closest('[data-rule-row]')?.remove()));
      add.addEventListener('click',()=>{
        const previous=list.lastElementChild; const previousMax=Number(previous?.querySelector('.liangpiao-max')?.value);
        const min=Number.isFinite(previousMax)?Math.min(100,previousMax):0; const max=Math.min(100,min+10);
        list.append(createLiangpiaoRuleRow({min,max,markup:0}));
      });
    }
  }

  function createWandaRuleRow({min=0,max=0,adjustment=0,min_discount_percent,max_discount_percent,fixed_adjustment_cents}={}) {
    const row=element('div','wanda-rule-row'); row.dataset.wandaRuleRow='true';
    const lowerValue=min_discount_percent ?? min; const upperValue=max_discount_percent ?? max; const adjustmentValue=fixed_adjustment_cents===undefined?adjustment:Number(fixed_adjustment_cents)/100;
    const lower=document.createElement('input'); lower.className='wanda-min'; lower.type='number'; lower.min='0'; lower.max='100'; lower.step='0.1'; lower.value=String(lowerValue); lower.setAttribute('aria-label','万达折扣率下限');
    const upper=document.createElement('input'); upper.className='wanda-max'; upper.type='number'; upper.min='0'; upper.max='100'; upper.step='0.1'; upper.value=String(upperValue); upper.setAttribute('aria-label','万达折扣率上限');
    const fixed=document.createElement('input'); fixed.className='wanda-adjustment'; fixed.type='number'; fixed.min='-1000'; fixed.max='1000'; fixed.step='0.1'; fixed.value=String(adjustmentValue); fixed.setAttribute('aria-label','固定加价/减价');
    const remove=element('button','wanda-remove','删除'); remove.type='button'; remove.addEventListener('click',()=>row.remove());
    row.append(element('span','', '折扣率'),lower,element('span','', '% 至'),upper,element('span','', '%，固定加价/减价'),fixed,element('span','', '元'),remove);
    return row;
  }
  function initWandaRules() {
    const list=$('wandaRuleList'); const add=$('addWandaRule'); if(!list||!add)return;
    list.querySelectorAll('.wanda-remove').forEach(button=>button.addEventListener('click',()=>button.closest('[data-wanda-rule-row]')?.remove()));
    add.addEventListener('click',()=>{
      const previous=list.lastElementChild; const previousMax=Number(previous?.querySelector('.wanda-max')?.value);
      const min=Number.isFinite(previousMax)?Math.min(100,previousMax):0; const max=Math.min(100,min+10);
      list.append(createWandaRuleRow({min,max,adjustment:0}));
    });
  }

  let preservedRegularAdjustmentCents=null;
  let preservedFridayMemberDayEnabled=true;
  let pricingLoaded=false;
  const yuanToCents=id=>Math.round(Number($(id).value)*100);
  function renderDynamicPricingRules(data={}) {
    const liangpiaoList=$('liangpiaoRuleList'); const liangpiaoFixedList=$('liangpiaoFixedRuleList'); const wandaList=$('wandaRuleList');
    const liangpiaoRules=Array.isArray(data.liangpiao_rules)?data.liangpiao_rules:[];
    const liangpiaoFixedRules=Array.isArray(data.liangpiao_fixed_rules)?data.liangpiao_fixed_rules:liangpiaoRules;
    const wandaRules=Array.isArray(data.wanda_rules)?data.wanda_rules:[];
    liangpiaoList.replaceChildren(...liangpiaoRules.map(rule=>createLiangpiaoRuleRow(rule)));
    liangpiaoFixedList.replaceChildren(...liangpiaoFixedRules.map(rule=>createLiangpiaoRuleRow(rule)));
    wandaList.replaceChildren(...wandaRules.map(rule=>createWandaRuleRow(rule)));
  }
  function collectLiangpiaoRules() {
    return [...document.querySelectorAll('#liangpiaoRuleList [data-rule-row]')].map(row=>({
      min_discount_percent:Number(row.querySelector('.liangpiao-min')?.value),
      max_discount_percent:Number(row.querySelector('.liangpiao-max')?.value),
      markup_percent:Number(row.querySelector('.liangpiao-markup')?.value),
    }));
  }
  function collectLiangpiaoFixedRules() {
    return [...document.querySelectorAll('#liangpiaoFixedRuleList [data-rule-row]')].map(row=>({
      min_discount_percent:Number(row.querySelector('.liangpiao-min')?.value),
      max_discount_percent:Number(row.querySelector('.liangpiao-max')?.value),
      markup_percent:Number(row.querySelector('.liangpiao-markup')?.value),
    }));
  }
  function collectWandaRules() {
    return [...document.querySelectorAll('#wandaRuleList [data-wanda-rule-row]')].map(row=>({
      min_discount_percent:Number(row.querySelector('.wanda-min')?.value),
      max_discount_percent:Number(row.querySelector('.wanda-max')?.value),
      fixed_adjustment_cents:Math.round(Number(row.querySelector('.wanda-adjustment')?.value)*100),
    }));
  }
  async function loadOperations() {
    pricingLoaded=false; $('saveOperations').disabled=true;
    try {
      const response=await v4Fetch('/api/settings/operations'); const data=await response.json(); if(!response.ok)throw new Error(data?.detail||'读取运营设置失败');
      if(!Array.isArray(data.liangpiao_rules)||!Array.isArray(data.liangpiao_fixed_rules)||!Array.isArray(data.wanda_rules))throw new Error('服务端未返回完整报价规则，已禁止保存');
      preservedRegularAdjustmentCents=Number.isInteger(data.regular_adjustment_cents)?data.regular_adjustment_cents:null;
      preservedFridayMemberDayEnabled=data.wplus_friday_member_day_enabled!==false;
      renderDynamicPricingRules(data);
      $('pricingEnabled').checked=data.enabled; $('liangpiaoPriceMode').value=data.liangpiao_price_mode||'FIXED'; $('wplusDiscount').value=(Math.abs(data.wplus_adjustment_cents)/100).toFixed(2); $('wplusThreshold').value=(data.wplus_member_price_threshold_cents/100).toFixed(2); $('roundingIncrement').value=String(data.rounding_increment_cents); $('roundingIncrementLiangpiao').value=String(data.rounding_increment_cents);
      $('pricingRuleMeta').textContent=`版本 ${data.rule_version} · 修订 ${data.revision}${data.updated_at?` · ${new Date(data.updated_at).toLocaleString('zh-CN')}`:''}`;
      pricingLoaded=true; $('saveOperations').disabled=false;
    } catch(error) { settingsMessage(error.message,true); }
  }
  function validatePricingRanges(rules,label) {
    if(!rules.length)return;
    if(rules.some(rule=>![rule.min_discount_percent,rule.max_discount_percent,rule.adjustment].every(Number.isFinite)))throw new Error(`${label}存在空值或无效数字`);
    if(Math.abs(rules[0].min_discount_percent)>1e-9||Math.abs(rules.at(-1).max_discount_percent-100)>1e-9)throw new Error(`${label}必须从0%开始并覆盖到100%`);
    for(let index=0;index<rules.length;index+=1){const rule=rules[index];if(rule.max_discount_percent<=rule.min_discount_percent)throw new Error(`${label}区间上限必须大于下限`);if(index>0&&Math.abs(rule.min_discount_percent-rules[index-1].max_discount_percent)>1e-9)throw new Error(`${label}区间存在断档或重叠`);}
  }
  function validYuan(id,label){const value=Number($(id).value);if(!Number.isFinite(value)||value<0)return null;return Math.round(value*100);}
  async function saveOperations() {
    const button=$('saveOperations'); button.disabled=true; settingsMessage('正在校验并保存运营报价规则…');
    try {
      if(!pricingLoaded||!Number.isInteger(preservedRegularAdjustmentCents))throw new Error('报价规则尚未读取完成，请刷新后再保存');
      const liangpiaoRules=collectLiangpiaoRules().map(rule=>({...rule,adjustment:rule.markup_percent}));
      const liangpiaoFixedRules=collectLiangpiaoFixedRules().map(rule=>({...rule,adjustment:rule.markup_percent}));
      const wandaRules=collectWandaRules().map(rule=>({...rule,adjustment:rule.fixed_adjustment_cents/100}));
      validatePricingRanges(liangpiaoRules,'良票特惠规则'); validatePricingRanges(liangpiaoFixedRules,'良票一口价规则'); validatePricingRanges(wandaRules,'万达规则');
      const discount=validYuan('wplusDiscount','W+调整'); const threshold=validYuan('wplusThreshold','W+阈值');
      if(discount===null||threshold===null||threshold<=0)throw new Error('W+调整和阈值必须填写有效金额');
      const payload={enabled:$('pricingEnabled').checked,liangpiao_price_mode:$('liangpiaoPriceMode').value,wplus_friday_member_day_enabled:preservedFridayMemberDayEnabled,regular_adjustment_cents:preservedRegularAdjustmentCents,wplus_adjustment_cents:-discount,wplus_member_price_threshold_cents:threshold,rounding_increment_cents:Number($('roundingIncrement').value),liangpiao_rules:collectLiangpiaoRules(),liangpiao_fixed_rules:collectLiangpiaoFixedRules(),wanda_rules:collectWandaRules()};
      const response=await v4Fetch('/api/settings/operations',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}); const data=await response.json();
      if(!response.ok)throw new Error(data?.detail?.[0]?.msg||data?.detail||'运营规则保存失败');
      renderDynamicPricingRules(data); $('pricingRuleMeta').textContent=`版本 ${data.rule_version} · 修订 ${data.revision} · ${new Date(data.updated_at).toLocaleString('zh-CN')}`; settingsMessage(`运营报价规则已保存，下一次核价立即使用（${data.rule_version}）。`);
    } catch(error) { settingsMessage(error.message,true); }
    finally { button.disabled=false;refreshDiagnostics(); }
  }

  async function loadConversationPolicy() {
    try {
      const response=await v4Fetch('/api/settings/conversation-policy');const data=await response.json();if(!response.ok)throw new Error(data?.detail||'读取会话策略失败');
      $('aiReplyEnabled').checked=data.ai_reply_enabled!==false;$('memoryHours').value=data.memory_hours;$('memoryDepth').value=data.memory_depth;$('stageGateEnabled').checked=data.stage_gate_enabled;$('interventionStart').value=data.intervention_start;$('interventionEnd').value=data.intervention_end;$('humanTakeoverDelay').value=data.human_takeover_delay_seconds;$('personaBackground').value=data.persona_background||[data.agent_persona,data.business_background].filter(Boolean).join('\n\n');$('customerServiceKnowledge').value=data.customer_service_knowledge||'';$('replyStyle').value=data.reply_style||'';$('humanServiceHours').value=data.human_service_hours||'';
      $('conversationPolicyMeta').textContent=`修订 ${data.revision||0}${data.updated_at?` · ${new Date(data.updated_at).toLocaleString('zh-CN')}`:''}`;
    } catch(error){$('conversationPolicyMeta').textContent=error.message;}
  }
  async function saveConversationPolicy() {
    const button=$('saveConversationPolicy');button.disabled=true;
    try {
      const payload={ai_reply_enabled:$('aiReplyEnabled').checked,memory_hours:Number($('memoryHours').value),memory_depth:Number($('memoryDepth').value),stage_gate_enabled:$('stageGateEnabled').checked,intervention_start:$('interventionStart').value,intervention_end:$('interventionEnd').value,human_takeover_delay_seconds:Number($('humanTakeoverDelay').value),persona_background:$('personaBackground').value.trim(),customer_service_knowledge:$('customerServiceKnowledge').value.trim(),reply_style:$('replyStyle').value.trim(),human_service_hours:$('humanServiceHours').value.trim()};
      const response=await v4Fetch('/api/settings/conversation-policy',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await response.json();if(!response.ok)throw new Error(data?.detail||'保存会话策略失败');$('conversationPolicyMeta').textContent=`已保存 · 修订 ${data.revision} · ${new Date(data.updated_at).toLocaleString('zh-CN')}`;
    } catch(error){$('conversationPolicyMeta').textContent=error.message;}finally{button.disabled=false;}
  }

  function conversationModeIdentity() {
    const shopId=$('conversationModeShopId').value.trim();const buyerId=$('conversationModeBuyerId').value.trim();const chatId=$('conversationModeChatId').value.trim();
    if(!shopId||!buyerId||!chatId)throw new Error('请完整填写店铺、买家和聊天标识');
    return {shopId,buyerId,chatId,path:`/api/plugin/conversations/${encodeURIComponent(buyerId)}/${encodeURIComponent(chatId)}/automation-mode`};
  }
  async function loadConversationMode() {
    try { const identity=conversationModeIdentity();const response=await v4Fetch(`${identity.path}?shop_id=${encodeURIComponent(identity.shopId)}`);const data=await response.json();if(!response.ok)throw new Error(data?.detail||'读取会话模式失败');$('conversationAutomationMode').value=data.mode;$('conversationModeMeta').textContent=data.scope==='conversation'?`当前为会话临时模式：${data.mode}`:`当前继承店铺模式：${data.mode}`; }
    catch(error){$('conversationModeMeta').textContent=error.message;}
  }
  async function saveConversationMode() {
    const button=$('saveConversationMode');button.disabled=true;
    try { const identity=conversationModeIdentity();const response=await v4Fetch(identity.path,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({shop_id:identity.shopId,mode:$('conversationAutomationMode').value})});const data=await response.json();if(!response.ok)throw new Error(data?.detail||'保存会话模式失败');$('conversationModeMeta').textContent=`会话临时模式已保存：${data.mode}，下一条消息生效。`; }
    catch(error){$('conversationModeMeta').textContent=error.message;}finally{button.disabled=false;}
  }
  async function clearConversationMode() {
    const button=$('clearConversationMode');button.disabled=true;
    try { const identity=conversationModeIdentity();const response=await v4Fetch(`${identity.path}?shop_id=${encodeURIComponent(identity.shopId)}`,{method:'DELETE'});const data=await response.json();if(!response.ok)throw new Error(data?.detail||'恢复店铺默认失败');$('conversationAutomationMode').value=data.mode;$('conversationModeMeta').textContent=`已恢复店铺默认模式：${data.mode}，下一条消息生效。`; }
    catch(error){$('conversationModeMeta').textContent=error.message;}finally{button.disabled=false;}
  }

  function showSettings(show) { showWorkspace(show?'model':'chat',true); }
  function updatePromptLength() { $('promptLength').textContent=`${$('visionPrompt').value.length} / 20000`; }
  function settingsMessage(message,isError=false) { $('settingsMessage').textContent=message; $('settingsMessage').classList.toggle('error-text',isError); }
  function catalogMessage(message,isError=false,target='modelFetchMessage') { $(target).textContent=message; $(target).classList.toggle('error-text',isError); }
  function updateThinkingHint() {
    const visionModel=$('modelSelect').value.trim().toLowerCase().split('/').pop(); const chatModel=$('chatModelSelect').value.trim().toLowerCase().split('/').pop(); const enabled=$('thinkingToggle').checked;
    $('qwenThinkingGroup').hidden=!visionModel.startsWith('qwen'); $('gptReasoningGroup').hidden=!chatModel.startsWith('gpt-5');
    $('thinkingHint').textContent=enabled?'已开启模型思考，响应时间可能增加。':'思考模式已关闭，优先降低延迟。';
  }
  async function loadSettings() {
    settingsMessage('正在读取设置…');
    try {
      const response=await v4Fetch('/api/settings/vision'); const data=await response.json(); if(!response.ok) throw new Error(data?.detail||'读取设置失败');
      $('baseUrl').value=data.base_url; $('modelSelect').value=data.model; $('chatBaseUrl').value=data.chat_base_url||data.base_url; $('chatModelSelect').value=data.chat_model||data.model; $('thinkingToggle').checked=data.enable_thinking; $('reasoningEffort').value=data.reasoning_effort||'none'; $('visionPrompt').value=data.vision_prompt; $('chatPrompt').value=data.chat_prompt;
      loadedPrompt=data.vision_prompt; $('currentModel').textContent=`上传千问 · URL良票优先（${data.model} / ${data.chat_model||data.model}）`; $('keyState').textContent=data.has_api_key?`已保存 ${data.masked_api_key}`:'未配置'; $('keyState').style.color=data.has_api_key?'#29945a':'#b16b20';
      $('apiKey').value=''; $('clearApiKey').checked=false; $('chatKeyState').textContent=data.has_chat_api_key?`已保存 ${data.masked_chat_api_key}`:'未配置'; $('chatKeyState').style.color=data.has_chat_api_key?'#29945a':'#b16b20'; $('chatApiKey').value=''; $('clearChatApiKey').checked=false; updatePromptLength(); updateThinkingHint(); settingsMessage('');
    } catch(error) { settingsMessage(error.message,true); }
  }
  async function fetchModels() {
    const button=$('fetchModels'); button.disabled=true; catalogMessage('正在从服务商获取模型…');
    try {
      const payload={base_url:$('baseUrl').value.trim(),api_key:$('apiKey').value.trim()||null};
      const response=await v4Fetch('/api/settings/vision/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}); const data=await response.json();
      if(!response.ok)throw new Error(data?.error?.message||data?.detail?.[0]?.msg||data?.detail||'获取模型失败');
      const options=$('visionModelOptions'); options.replaceChildren(); data.models.forEach(model=>{const option=document.createElement('option');option.value=model;options.append(option)});
      catalogMessage(`已获取 ${data.count} 个模型，请点击模型输入框选择。`); updateThinkingHint(); refreshDiagnostics();
    } catch(error) { catalogMessage(error.message,true); }
    finally { button.disabled=false; }
  }
  async function fetchChatModels() {
    const button=$('fetchChatModels'); button.disabled=true; catalogMessage('正在从 AI 回复服务商获取模型…',false,'chatModelFetchMessage');
    try {
      const payload={provider:'chat',base_url:$('chatBaseUrl').value.trim(),api_key:$('chatApiKey').value.trim()||null};
      const response=await v4Fetch('/api/settings/vision/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}); const data=await response.json();
      if(!response.ok)throw new Error(data?.error?.message||data?.detail?.[0]?.msg||data?.detail||'获取模型失败');
      const options=$('chatModelOptions'); options.replaceChildren(); data.models.forEach(model=>{const option=document.createElement('option');option.value=model;options.append(option)});
      catalogMessage(`已获取 ${data.count} 个 AI 回复模型。`,false,'chatModelFetchMessage'); updateThinkingHint(); refreshDiagnostics();
    } catch(error) { catalogMessage(error.message,true,'chatModelFetchMessage'); }
    finally { button.disabled=false; }
  }
  async function saveSettings() {
    const button=$('saveSettings'); button.disabled=true; settingsMessage('正在加密并保存…');
    try {
      const payload={base_url:$('baseUrl').value.trim(),model:$('modelSelect').value.trim(),chat_base_url:$('chatBaseUrl').value.trim(),chat_model:$('chatModelSelect').value.trim(),enable_thinking:$('thinkingToggle').checked,reasoning_effort:$('reasoningEffort').value,vision_prompt:$('visionPrompt').value.trim(),chat_prompt:$('chatPrompt').value.trim(),api_key:$('apiKey').value.trim()||null,clear_api_key:$('clearApiKey').checked,chat_api_key:$('chatApiKey').value.trim()||null,clear_chat_api_key:$('clearChatApiKey').checked};
      const response=await v4Fetch('/api/settings/vision',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}); const data=await response.json();
      if(!response.ok) throw new Error(data?.detail?.[0]?.msg||data?.detail||'保存失败');
      loadedPrompt=data.vision_prompt; $('currentModel').textContent=`上传千问 · URL良票优先（${data.model} / ${data.chat_model}）`; $('keyState').textContent=data.has_api_key?`已保存 ${data.masked_api_key}`:'未配置'; $('keyState').style.color=data.has_api_key?'#29945a':'#b16b20'; $('chatKeyState').textContent=data.has_chat_api_key?`已保存 ${data.masked_chat_api_key}`:'未配置'; $('chatKeyState').style.color=data.has_chat_api_key?'#29945a':'#b16b20'; $('apiKey').value=''; $('chatApiKey').value=''; $('clearApiKey').checked=false; $('clearChatApiKey').checked=false; updateThinkingHint(); settingsMessage('设置已保存，下一张图片立即使用新配置。');
    } catch(error) { settingsMessage(error.message,true); }
    finally { button.disabled=false;refreshDiagnostics(); }
  }
  const workspaceButtons=[...document.querySelectorAll('[data-workspace]')];
  workspaceButtons.forEach((button,index)=>{
    button.addEventListener('click',()=>showWorkspace(button.dataset.workspace));
    button.addEventListener('keydown',event=>{let next=index;if(event.key==='ArrowRight')next=(index+1)%workspaceButtons.length;else if(event.key==='ArrowLeft')next=(index-1+workspaceButtons.length)%workspaceButtons.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=workspaceButtons.length-1;else return;event.preventDefault();showWorkspace(workspaceButtons[next].dataset.workspace,true)});
  });
  $('openSettings').addEventListener('click',()=>showSettings(true)); $('closeSettings').addEventListener('click',()=>showSettings(false)); $('settingsBackdrop').addEventListener('click',()=>showSettings(false));
  $('modelSettingsTab').addEventListener('click',()=>switchSettingsTab('model')); $('operationsSettingsTab').addEventListener('click',()=>switchSettingsTab('operations'));
  $('useAirelvo').addEventListener('click',()=>{$('baseUrl').value='https://airelvo.cc/v1';catalogMessage('识图接口已切换到 Airelvo。');}); $('fetchModels').addEventListener('click',fetchModels);
  $('useChatAirelvo').addEventListener('click',()=>{$('chatBaseUrl').value='https://airelvo.cc/v1';catalogMessage('AI 回复接口已切换到 Airelvo。',false,'chatModelFetchMessage');}); $('fetchChatModels').addEventListener('click',fetchChatModels);
  $('modelSelect').addEventListener('input',updateThinkingHint); $('chatModelSelect').addEventListener('input',updateThinkingHint); $('thinkingToggle').addEventListener('change',updateThinkingHint); $('reasoningEffort').addEventListener('change',updateThinkingHint);
  $('roundingIncrement').addEventListener('change',()=>{$('roundingIncrementLiangpiao').value=$('roundingIncrement').value}); $('roundingIncrementLiangpiao').addEventListener('change',()=>{$('roundingIncrement').value=$('roundingIncrementLiangpiao').value});
  $('refreshDiagnostics').addEventListener('click',refreshDiagnostics); $('clearDiagnostics').addEventListener('click',clearDiagnostics); $('refreshAgentAudit').addEventListener('click',loadAgentAudit); $('refreshManualTasks').addEventListener('click',loadManualTasks);$('agentAuditSearch').addEventListener('input',()=>renderAgentAudit(loadedAgentToolAudits,loadedEventAudits));$('manualTaskSearch').addEventListener('input',()=>renderManualTasks(loadedManualTasks)); $('refreshShops').addEventListener('click',()=>loadShops(true)); $('refreshQuoteRecords').addEventListener('click',loadQuoteRecords);$('showWandaQuotes').addEventListener('click',()=>showQuoteSource('wanda'));$('showLiangpiaoQuotes').addEventListener('click',()=>showQuoteSource('liangpiao')); $('refreshOrders').addEventListener('click',loadOrders); $('saveReminders').addEventListener('click',saveReminderSettings); $('reminderEnabled').addEventListener('change',()=>{ $('reminderEnabledText').textContent=$('reminderEnabled').checked?'已开启':'已关闭'; if(reminderLoaded)void saveReminderSettings(); }); $('orderSearch').addEventListener('input',()=>{orderPage=1;renderOrders()}); $('closeOrderDetail').addEventListener('click',()=>$('orderDetailDialog').close()); $('orderDetailDialog').addEventListener('click',event=>{if(event.target===$('orderDetailDialog'))$('orderDetailDialog').close()});
  document.querySelectorAll('[data-order-filter]').forEach(button=>button.addEventListener('click',()=>{activeOrderFilter=button.dataset.orderFilter;orderPage=1;document.querySelectorAll('[data-order-filter]').forEach(item=>item.classList.toggle('active',item===button));renderOrders()}));
  document.querySelectorAll('[data-template-filter]').forEach(button=>button.addEventListener('click',()=>{document.querySelectorAll('[data-template-filter]').forEach(item=>item.classList.toggle('active',item===button));document.querySelectorAll('[data-template-section]').forEach(section=>section.hidden=section.dataset.templateSection!==button.dataset.templateFilter)})); $('addKeywordRule').addEventListener('click',addKeywordRule); $('addKnowledge').addEventListener('click',addKnowledge); $('knowledgeCategoryFilter').addEventListener('change',renderKnowledge);
  document.querySelectorAll('[data-template-variable]').forEach(button=>button.addEventListener('click',async()=>{try{await copyTemplateVariable(button.dataset.templateVariable);templateMessage(`已复制 ${button.dataset.templateVariable}`);}catch{templateMessage('复制失败，请选中变量后复制。',true);}}));
  document.querySelectorAll('.template-editor textarea').forEach(textarea=>textarea.addEventListener('focus',()=>{activeTemplateTextarea=textarea;})); $('insertMessageSeparator').addEventListener('click',insertMessageSeparator);
  $('saveSettings').addEventListener('click',saveSettings); $('saveOperations').addEventListener('click',saveOperations); $('saveTemplates').addEventListener('click',saveTemplates); $('saveConversationPolicy').addEventListener('click',saveConversationPolicy); $('loadConversationMode').addEventListener('click',loadConversationMode); $('saveConversationMode').addEventListener('click',saveConversationMode); $('clearConversationMode').addEventListener('click',clearConversationMode); $('visionPrompt').addEventListener('input',updatePromptLength); $('resetPrompt').addEventListener('click',()=>{$('visionPrompt').value=loadedPrompt;updatePromptLength()});
  document.addEventListener('keydown',event=>{if(event.key==='Escape')showSettings(false)});
  showWorkspace(location.hash.replace('#','')||'chat'); updateSend(); initLiangpiaoRules(); initWandaRules(); loadSettings(); loadOperations(); loadConversationPolicy(); loadKnowledge(); refreshDiagnostics();
