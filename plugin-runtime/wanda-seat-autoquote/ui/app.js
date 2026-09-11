import { createPluginSdk } from './sdk.js';

const fishMoreSdk=createPluginSdk();
const fishMoreReady=fishMoreSdk.ready();
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
let modelConfigScope='global';
let modelConfigShopId='';
async function v4Fetch(path,options={}){await fishMoreReady;const normalized=String(path).replace(/^\/+/, '');const request={...options};request.headers={...(request.headers||{}),'x-wanda-model-scope':modelConfigScope,...(modelConfigShopId?{'x-wanda-shop-id':modelConfigShopId}:{})};if(request.body instanceof FormData){request.headers['content-type']='application/json';request.body=JSON.stringify(await serializeFormData(request.body));}return fishMoreSdk.authedFetch(`ui/v4/${normalized}`,request);}
async function v4ImageFetch(form){
  await fishMoreReady;
  const started=await fishMoreSdk.authedFetch('ui/v4/jobs/image-message',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(await serializeFormData(form))});
  if(started.status!==202)return started;
  const job=await started.json();if(!job.job_id)throw new Error('图片识别任务创建失败');
  for(let attempt=0;attempt<100;attempt+=1){
    await new Promise(resolve=>setTimeout(resolve,900));
    const result=await fishMoreSdk.authedFetch(`ui/v4/jobs/${encodeURIComponent(job.job_id)}`);
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

  function now() { return new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date()); }
  function money(value) { return Number.isFinite(value) ? `¥${value.toFixed(2).replace(/\.00$/,'')}` : '—'; }
  function centsAmount(value) { if(value===null||value===undefined||value==='')return '—';const amount=Number(value);return Number.isSafeInteger(amount)?`¥${(amount/100).toFixed(2)}`:'—'; }
  function displayValue(value) { return value===null||value===undefined||value===''?'—':String(value); }
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
    stack.append(bubble,element('div','message-time',now())); row.append(stack); chat.append(row); scrollBottom(); return row;
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
        const form=new FormData(); form.append('conversation_id',state.conversationId); form.append('message_text',text); form.append('image',file);
        response=await v4ImageFetch(form);
      } else {
        response=await v4Fetch('/api/chat/text-messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({conversation_id:state.conversationId,text})});
      }
      const raw=await response.text();let body={};try{body=raw?JSON.parse(raw):{};}catch{body={};}
      if(!response.ok){const detail=typeof body.detail==='string'?body.detail:body?.detail?.[0]?.msg;throw new Error(body?.error?.message||detail||`回复生成失败（HTTP ${response.status}），请稍后重试。`);}
      typing.remove(); appendMessage('assistant',body.message.text,{recognition:body.message.recognition,quote:body.message.quote});
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

  let loadedAgentRuns=[];let loadedManualTasks=[];
  const auditStatusLabel=value=>({running:'运行中',ready:'已生成回复',reply_ready:'已生成回复',failed:'失败',pending:'待处理',claimed:'处理中',completed:'已完成'}[String(value||'')]||String(value||'未知状态'));
  const auditReasonLabel=value=>({
    agent_model_failed:'agent_model_failed',agent_response_missing:'agent_response_missing',conversation_snapshot_unavailable:'会话历史不可用',
    external_write_fuse_open:'系统写入保护已开启',fulfillment_required:'等待人工出票或发货',multiple_pending_orders:'发现多个待处理订单',
    order_unverified:'订单归属尚未核验',send_message_failed:'消息发送失败',paid_amount_unverified:'付款金额尚未核验',
    paid_amount_mismatch:'付款金额与报价不一致',order_already_paid:'订单已付款，禁止自动改价',platform_price_change_rejected:'平台拒绝自动改价',
    MANUAL_HOLD:'MANUAL_HOLD',WAITING_WPLUS_MARK:'WAITING_WPLUS_MARK',READY_FOR_MANUAL_TICKETING:'READY_FOR_MANUAL_TICKETING',
    MANUAL_QUOTE:'MANUAL_QUOTE',REFUND_REQUIRED:'REFUND_REQUIRED',REFUND_PENDING:'REFUND_PENDING',PROBE_REQUIRED:'PROBE_REQUIRED',
  }[String(value||'')]||String(value||'未分类'));
  function auditValue(value){if(value===null||value===undefined||value==='')return'暂无';if(Array.isArray(value))return value.length?value.map(auditValue).join('、'):'暂无';if(typeof value==='object')return Object.entries(value).map(([key,item])=>`${key}: ${auditValue(item)}`).join(' · ')||'暂无';return String(value);}
  function auditMatches(record,query){if(!query)return true;return[record.buyer_id,record.chat_id,record.event_id,record.shop_id,record.status,record.failure?.reason,...(record.tool_calls||[]).flatMap(call=>[call.tool_name,call.status,call.error_reason])].some(value=>String(value||'').toLowerCase().includes(query));}
  function appendAuditContext(parent,label,value){const card=element('div','audit-context-card');card.append(element('strong','',label),element('small','',auditValue(value)));parent.append(card);}
  function renderAgentAudit(runs=loadedAgentRuns){
    const root=$('agentAuditEntries');root.replaceChildren();const query=String($('agentAuditSearch')?.value||'').trim().toLowerCase();const visible=runs.filter(run=>auditMatches(run,query));
    if(!visible.length){root.append(element('div','diagnostic-empty',query?'没有找到匹配的 Agent 运行。':'暂无 Canonical Agent 运行记录。'));return;}
    visible.forEach(run=>{
      const detail=element('details','diagnostic-entry');const summary=element('summary');
      summary.append(element('span',`diagnostic-event${run.failure?.reason?' diagnostic-error':''}`,run.failure?.reason||auditStatusLabel(run.status)));
      summary.append(element('span','diagnostic-meta',[run.flow,run.buyer_id,run.chat_id,run.message_time].filter(Boolean).join(' · ')));detail.append(summary);
      const facts=element('div','audit-facts');
      [['买家',run.buyer_id],['店铺',run.shop_id],['聊天ID',run.chat_id],['事件ID',run.event_id],['消息时间',run.message_time],['流程',run.flow||'CANONICAL_CONVERSATION_AGENT'],['运行状态',auditStatusLabel(run.status)],['Agent模型',`${run.agent_model?.provider||'OpenAI-compatible'} · ${run.agent_model?.model||$('currentModel')?.textContent||'当前配置'}`],['模型配置',run.agent_model?.config_id],['配置修订',run.agent_model?.config_revision],['配置主机',run.agent_model?.base_url_host]].forEach(([label,value])=>facts.append(recordFact(label,value)));
      detail.append(facts);
      const context=element('div','audit-context');appendAuditContext(context,'IM history',run.context?.im_history);appendAuditContext(context,'Recognition',run.context?.recognition);appendAuditContext(context,'Purchase Context',run.context?.purchase_context);appendAuditContext(context,'Quote / Same-Type Reference',{quote:run.context?.quote,same_type_reference:run.context?.same_type_reference});appendAuditContext(context,'Transaction State',run.context?.transaction_state);appendAuditContext(context,'Human / Manual Context',run.context?.human_manual_context);detail.append(context);
      const tools=run.tool_calls||[];const toolTitle=element('h4','audit-section-title',`Tool calls（${tools.length}）`);detail.append(toolTitle);
      if(!tools.length)detail.append(element('div','diagnostic-empty','暂无工具调用记录。'));else{const toolFacts=element('div','audit-facts');tools.forEach(call=>toolFacts.append(recordFact(`第${Number(call.sequence??0)+1}轮 · ${call.tool_name||'未知工具'}`,`${auditStatusLabel(call.status)}${call.error_reason?` · ${auditReasonLabel(call.error_reason)}`:''}${call.result_status?` · result ${call.result_status}`:''}`)));detail.append(toolFacts);}
      const resultFacts=element('div','audit-facts');resultFacts.append(recordFact('Reply',`${run.reply?.status||'unavailable'}${run.reply?.origin?` · ${run.reply.origin}`:''}`),recordFact('Command',run.command?.status||'not_created'),recordFact('Command ID',run.command?.command_id),recordFact('Sent message ID',run.command?.sent_message_id),recordFact('Failure stage',run.failure?.stage),recordFact('Failure reason',run.failure?.reason?auditReasonLabel(run.failure.reason):'无'));detail.append(resultFacts);root.append(detail);
    });
  }
  async function loadAgentAudit(){const root=$('agentAuditEntries');root.replaceChildren(element('div','diagnostic-empty','正在读取 Canonical Agent 审计…'));try{const response=await v4Fetch('/api/rules-first/agent-runs?limit=200');const data=await response.json();if(!response.ok)throw new Error(data?.detail||'读取 Agent 审计失败');loadedAgentRuns=Array.isArray(data.runs)?data.runs:[];renderAgentAudit();}catch(error){root.replaceChildren(element('div','diagnostic-empty',error.message));}}
  async function claimManualTask(task,button){const operator=window.prompt('请输入操作人标识（仅用于领取租约）','plugin-ui');if(!operator?.trim())return;button.disabled=true;try{const response=await v4Fetch(`/api/rules-first/manual-tasks/${encodeURIComponent(task.task_id)}/claim`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:task.transaction_revision,operator_id:operator.trim()})});const data=await response.json();if(!response.ok)throw new Error(data?.detail||'领取任务失败');await loadManualTasks();}catch(error){button.textContent=error.message;button.disabled=false;}}
  async function completeManualTask(task,resolution,button){if(!window.confirm(resolution==='resume'?'确认恢复自动化？系统仍会重新执行全部安全门禁。':'确认仅关闭人工任务？该操作不会改订单、履约或交易状态。'))return;button.disabled=true;try{const response=await v4Fetch(`/api/rules-first/manual-tasks/${encodeURIComponent(task.task_id)}/complete`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:task.transaction_revision,lease_token:task.lease_token,resolution})});const data=await response.json();if(!response.ok)throw new Error(data?.detail||'处理任务失败');await loadManualTasks();}catch(error){button.textContent=error.message;button.disabled=false;}}
  function renderManualTasks(tasks=loadedManualTasks){const root=$('manualTaskEntries');root.replaceChildren();const query=String($('manualTaskSearch')?.value||'').trim().toLowerCase();const visible=tasks.filter(task=>auditMatches({...task,failure:{reason:task.reason}},query));if(!visible.length){root.append(element('div','diagnostic-empty',query?'没有找到匹配的人工任务。':'暂无人工任务；当前状态没有可展示的人工接管任务。'));return;}visible.forEach(task=>{const detail=element('details','diagnostic-entry');const summary=element('summary');summary.append(element('span','diagnostic-event',auditReasonLabel(task.reason)),element('span','diagnostic-meta',[auditStatusLabel(task.status),task.buyer_id,task.chat_id].filter(Boolean).join(' · ')));const facts=element('div','audit-facts');[['店铺ID',task.shop_id],['买家ID',task.buyer_id],['聊天ID',task.chat_id],['任务编号',task.task_id],['交易状态',task.details?.flow_state||task.details?.transaction_state||'Unavailable'],['人工原因',auditReasonLabel(task.reason)],['任务状态',auditStatusLabel(task.status)],['创建时间',task.created_at||'暂无'],['修订号',task.transaction_revision]].forEach(([label,value])=>facts.append(recordFact(label,value)));detail.append(facts);const actions=element('div','manual-task-actions');if(task.status==='pending'){const claim=element('button','diagnostics-button','领取任务');claim.type='button';claim.disabled=task.reason==='fulfillment_required';claim.title=claim.disabled?'该任务等待官方履约事件，不允许人工领取':'';claim.addEventListener('click',()=>claimManualTask(task,claim));actions.append(claim);}else if(task.status==='claimed'&&task.lease_token){const resolved=element('button','diagnostics-button','状态已更新，关闭任务');resolved.type='button';resolved.addEventListener('click',()=>completeManualTask(task,'resolved',resolved));const resume=element('button','diagnostics-button','恢复自动化');resume.type='button';resume.addEventListener('click',()=>completeManualTask(task,'resume',resume));actions.append(resolved,resume);}if(actions.childElementCount)detail.append(actions);root.append(detail);});}
  async function loadManualTasks(){const root=$('manualTaskEntries');root.replaceChildren(element('div','diagnostic-empty','正在读取人工任务…'));try{const response=await v4Fetch('/api/rules-first/manual-tasks');const data=await response.json();if(!response.ok)throw new Error(data?.detail||'读取人工任务失败');loadedManualTasks=Array.isArray(data.tasks)?data.tasks:[];renderManualTasks();}catch(error){root.replaceChildren(element('div','diagnostic-empty',error.message));}}

  async function overviewRequest(path) {
    const response=await v4Fetch(path);const data=await response.json();
    if(!response.ok)throw new Error(data?.detail||`读取失败（HTTP ${response.status}）`);
    return data;
  }
  function overviewValue(label,value) { const row=element('div','config-value');row.append(element('span','',label),element('b','',displayValue(value)));return row; }
  function isToday(value) { const date=new Date(value);const now=new Date();return Number.isFinite(date.getTime())&&date.getFullYear()===now.getFullYear()&&date.getMonth()===now.getMonth()&&date.getDate()===now.getDate(); }
  async function loadOverview() {
    const results=await Promise.allSettled([
      overviewRequest('/api/plugin/shops?include_canonical=true'),overviewRequest('/api/plugin/quote-records?limit=50'),
      overviewRequest('/api/rules-first/agent-runs?limit=8'),overviewRequest('/api/rules-first/manual-tasks'),
      overviewRequest('/api/settings/vision'),fishMoreSdk.authedFetch('ui/api/overview').then(async response=>({ok:response.ok,data:await response.json()})),
    ]);
    const value=index=>results[index].status==='fulfilled'?results[index].value:null;
    const shops=value(0)?.shops||[];const records=value(1)?.records||[];const runs=value(2)?.runs||[];const tasks=value(3)?.tasks||[];const model=value(4);const overview=value(5);
    const todayRecords=records.filter(record=>isToday(record.created_at));const failedRecords=records.filter(record=>record.status==='failed');const failedRuns=runs.filter(run=>run.failure?.reason);
    $('overviewQuoteCount').textContent=String(todayRecords.length);$('overviewQuoteMeta').textContent=`共 ${records.length} 条真实核价记录`;
    const latestRun=runs[0];$('overviewAgentStatus').textContent=latestRun?auditStatusLabel(latestRun.status):'暂无运行';$('overviewAgentMeta').textContent=latestRun?.agent_model?.model||'等待下一次运行';
    const enabledShops=shops.filter(shop=>shop.enabled).length;$('overviewShopCount').textContent=`${enabledShops} / ${shops.length}`;$('overviewShopMeta').textContent=shops.length?'自动化已开启店铺':'暂无同步店铺';
    const exceptions=failedRecords.length+failedRuns.length+tasks.filter(task=>task.status==='pending').length;$('overviewExceptionCount').textContent=String(exceptions);$('overviewExceptionMeta').textContent=exceptions?'需要查看详情':'当前没有待处理异常';
    const configRoot=$('overviewConfigList');configRoot.replaceChildren();
    if(model){configRoot.append(overviewValue('作用域',model.scope||'当前租户'),overviewValue('Provider',model.provider||'OpenAI-compatible'),overviewValue('Model',model.chat_model||model.model),overviewValue('配置修订',model.config_revision!==undefined?`${model.config_id||'—'} · r${model.config_revision}`:'—'));}
    else configRoot.append(element('div','empty-state','模型配置暂不可读取。'));
    const health=overview?.ok&&overview.data?.plugin?.status==='healthy';$('headerHealth').textContent=health?'系统运行正常':'服务需要检查';$('overviewConfigStatus').textContent=health?'已连接':'待检查';$('overviewConfigStatus').className=`status-badge ${health?'success':'warning'}`;
    const runRoot=$('overviewRuns');runRoot.replaceChildren();const feed=[...runs.map(run=>({kind:'Agent',title:run.failure?.reason||auditStatusLabel(run.status),meta:[run.buyer_id,run.message_time].filter(Boolean).join(' · '),status:run.failure?.reason?'danger':'success'})),...tasks.slice(0,4).map(task=>({kind:'人工任务',title:auditReasonLabel(task.reason),meta:[auditStatusLabel(task.status),task.buyer_id].filter(Boolean).join(' · '),status:task.status==='completed'?'success':'warning'}))].slice(0,6);
    if(!feed.length)runRoot.append(element('div','empty-state','暂无最近运行记录。'));
    feed.forEach(item=>{const row=element('div','overview-run');const copy=element('div');copy.append(element('span','run-kind',item.kind),element('strong','',item.title),element('small','',item.meta||'—'));row.append(copy,element('span',`status-badge ${item.status}`,item.status==='danger'?'异常':item.status==='warning'?'待处理':'完成'));runRoot.append(row);});
  }

  const workspaceMeta=Object.freeze({
    overview:['概览','查看插件运行、报价服务与最近处理记录。'],
    chat:['识别报价','保留既有图片识别与报价入口。'],
    shops:['店铺开关','按当前鱼麦多租户控制各店铺的自动回复和自动改价。'],
    pricing:['运营报价','配置确定性整数分报价规则，保存后下一次核价立即使用。'],
    'quote-records':['报价记录','查看当前租户通过官方实时核价取得的历史报价。'],
    orders:['订单管理','通过鱼麦多权威订单接口查看插件已观察订单的最新状态。'],
    templates:['回复话术','编辑识图、报价、引导和改价成功通知；变量名称全部使用中文。'],
    model:['模型接口','管理当前真实 scoped model API 配置。'],
    knowledge:['知识库','审核生产知识与人工会话候选，禁止未经审批直接进入运行时。'],
    'agent-audit':['Agent审计','查看 CanonicalConversationAgent、AgentContextBuilder 与 RulesFirst 审计投影。'],
    'manual-tasks':['人工任务','查看当前 RulesFirst 被安全门禁转入人工处理的真实任务。'],
    settings:['设置','保留会话策略及通用运行配置，不改变后端配置语义。']
  });
  let activeWorkspace='overview';
  function showWorkspace(name,focus=false) {
    if(name==='conversation')name='settings';
    if(!workspaceMeta[name])name='overview'; activeWorkspace=name;
    const settingsWorkspace=['shops','pricing','quote-records','orders','templates','model','knowledge','settings'].includes(name);
    $('mainPage').hidden=settingsWorkspace;
    $('overviewPage').hidden=name!=='overview'; $('workspaceChat').hidden=name!=='chat'; $('diagnosticsPanel').hidden=name!=='logs'; $('agentAuditPanel').hidden=name!=='agent-audit'; $('manualTasksPanel').hidden=name!=='manual-tasks';
    $('settingsDrawer').classList.toggle('show',settingsWorkspace); $('settingsDrawer').setAttribute('aria-hidden',String(!settingsWorkspace));
    document.querySelectorAll('[data-workspace]').forEach(button=>{const selected=button.dataset.workspace===name;button.classList.toggle('active',selected);button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1});
    if(settingsWorkspace){
      const model=name==='model'; $('modelSettingsPanel').hidden=!model; $('operationsSettingsPanel').hidden=model;
      document.querySelectorAll('[data-operation-view]').forEach(section=>section.hidden=model||section.dataset.operationView!==name);
      $('saveSettings').hidden=!model; $('settingsFooter').hidden=!model;
      $('workspaceSettingsTitle').textContent=workspaceMeta[name][0]; $('workspaceSettingsDescription').textContent=workspaceMeta[name][1]; settingsMessage('');
    }
    if(name==='overview')loadOverview();
    if(name==='shops')loadShops();
    if(name==='pricing')loadOperations();
    if(name==='quote-records')loadQuoteRecords();
    if(name==='orders')loadOrders();
    if(name==='templates'){loadTemplates();loadReminderSettings();loadReminderTasks();}
    if(name==='settings')loadConversationPolicy();
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
      const response=await v4Fetch(`/api/plugin/shops/${encodeURIComponent(shop.shop_id)}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:input.checked,canonical_quote_enabled:input.checked,canonical_conversation_enabled:input.checked})});
      const data=await response.json(); if(!response.ok)throw new Error(data?.detail||'店铺开关保存失败');
      shop.enabled=data.shop.enabled; state.textContent=data.shop.enabled?'自动回复与改价已开启':'自动化已关闭'; state.classList.toggle('disabled',!data.shop.enabled);
      const verify=await v4Fetch('/api/plugin/shops?include_canonical=true'); const listed=await verify.json();
      const saved=(listed.shops||[]).find(item=>String(item.shop_id)===String(shop.shop_id));
      if(!verify.ok||!saved||saved.enabled!==data.shop.enabled||saved.canonical_quote_enabled!==data.shop.enabled)throw new Error('保存后回读状态不一致，请检查服务存储权限');
    } catch(error) { input.checked=!input.checked; state.textContent=error.message; state.classList.add('disabled'); }
    finally { input.disabled=false; }
  }
  async function setCanonicalShopEnabled(shop,input,state) {
    input.disabled=true;
    try {
      const response=await v4Fetch(`/api/plugin/shops/${encodeURIComponent(shop.shop_id)}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:Boolean(shop.enabled),canonical_quote_enabled:input.checked})});
      const data=await response.json(); if(!response.ok)throw new Error(data?.detail||'Canonical 报价开关保存失败');
      shop.canonical_quote_enabled=data.shop.canonical_quote_enabled; state.textContent=data.shop.canonical_quote_enabled?'Canonical 报价已开启':'Canonical 报价已关闭'; state.classList.toggle('disabled',!data.shop.canonical_quote_enabled);
      const verify=await v4Fetch('/api/plugin/shops?include_canonical=true'); const listed=await verify.json();
      const saved=(listed.shops||[]).find(item=>String(item.shop_id)===String(shop.shop_id));
      if(!verify.ok||!saved||saved.canonical_quote_enabled!==data.shop.canonical_quote_enabled)throw new Error('保存后回读状态不一致，请检查服务存储权限');
    } catch(error) { input.checked=!input.checked; state.textContent=error.message; state.classList.add('disabled'); }
    finally { input.disabled=false; }
  }
  let loadedShops=[];
  function renderShops(shops) {
    loadedShops=shops;const query=String($('shopSearch')?.value||'').trim().toLowerCase();const status=$('shopStatusFilter')?.value||'';const visible=shops.filter(shop=>(!query||[shop.shop_name,shop.shop_id].some(value=>String(value||'').toLowerCase().includes(query)))&&(!status||(status==='enabled'?shop.enabled:!shop.enabled)));const root=$('shopList');root.replaceChildren();$('shopCount').textContent=`共 ${visible.length} 家店铺`;$('shopFooterCount').textContent=`共 ${visible.length} 条`;
    if(!visible.length){root.append(element('div','diagnostic-empty',shops.length?'没有符合当前筛选条件的店铺。':'当前租户暂未同步到店铺。收到下一条平台事件后会自动同步，也可稍后刷新。'));return;}
    visible.forEach(shop=>{
      const row=element('div','shop-row');const copy=element('div','shop-copy');copy.append(element('strong','',shop.shop_name||shop.shop_id),element('small','',`店铺标识 ${shop.shop_id}`));
      const controls=element('div','shop-controls'); const state=element('span',`shop-state${shop.enabled&&shop.canonical_quote_enabled?'':' disabled'}`,shop.enabled&&shop.canonical_quote_enabled?'Canonical 自动报价已开启':'Canonical 自动报价已关闭'); const label=element('label','switch'); const input=document.createElement('input'); input.type='checkbox'; input.checked=Boolean(shop.enabled&&shop.canonical_quote_enabled); const slider=element('span','slider'); label.append(input,slider); controls.append(element('small','shop-toggle-name','Canonical 自动报价'),state,label); row.append(copy,controls); root.append(row); input.addEventListener('change',()=>setShopEnabled(shop,input,state));
    });
  }
  async function loadShops(sync=false) {
    const root=$('shopList');root.replaceChildren(element('div','diagnostic-empty',sync?'正在从鱼麦多同步店铺…':'正在读取店铺…'));
    try {
      if(sync){
        await fishMoreReady;
        const synced=await fishMoreSdk.authedFetch('ui/api/shops/sync',{method:'POST'});
        const syncData=await synced.json();
        if(!synced.ok)throw new Error(syncData?.error||'鱼麦多店铺同步失败');
      }
      const response=await v4Fetch('/api/plugin/shops?include_canonical=true');const data=await response.json();if(!response.ok)throw new Error(data?.detail||'读取店铺失败');renderShops(data.shops||[]);
    }
    catch(error){root.replaceChildren(element('div','diagnostic-empty',error.message));}
  }

  function recordFact(label,value,className='') { const node=element('div',`record-fact ${className}`.trim());node.append(element('span','',label),element('b','',displayValue(value)));return node; }
  function formatRecordDate(value) { const parsed=Date.parse(String(value||''));if(!Number.isFinite(parsed))return'';const date=new Date(parsed);return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`; }
  function formatRecordTime(value) { const parsed=Date.parse(String(value||''));return Number.isFinite(parsed)?new Date(parsed).toLocaleString('zh-CN',{hour12:false}):'—'; }
  function recordValue(record,...keys) { for(const key of keys){const value=record?.[key];if(value!==null&&value!==undefined&&value!=='')return value;}return null; }
  function recordAmount(record,centsKey,fenKey) { return centsAmount(recordValue(record,centsKey,fenKey)); }
  function quoteRecordStatus(record) {
    const status=String(record?.status||'').toLowerCase();
    const state=String(record?.quote_state||'').toUpperCase();
    if(status==='failed'||status==='rejected'||state==='FAILED')return{key:'failed',label:'失败',className:'danger'};
    if(status==='superseded'||state==='SUPERSEDED')return{key:'superseded',label:'已替代',className:'warning'};
    if(state==='PREVIEW'||record?.transaction_authorized===false||recordValue(record,'total_quote_cents','total_quote_fen')===null)return{key:'pending',label:'待完成',className:'warning'};
    return{key:'success',label:'成功',className:'success'};
  }
  function renderQuoteRecordDetail(record) {
    const root=$('quoteRecordDetail');root.replaceChildren();const heading=element('div','detail-panel-head');
    heading.append(element('span','eyebrow','QUOTE DETAIL'),element('h3','',recordValue(record,'cinema')||'报价详情'),element('small','',`${recordValue(record,'movie')||'影片待确认'} · ${formatRecordTime(recordValue(record,'created_at','createdAt'))}`));
    const info=quoteRecordStatus(record);const badge=element('span',`status-badge ${info.className}`,info.label);heading.append(badge);root.append(heading);
    const facts=element('div','detail-fact-list');[['核价状态',info.label],['买家',recordValue(record,'buyer_id','buyerId')],['聊天会话',recordValue(record,'chat_id','chatId')],['城市',recordValue(record,'city')],['影院',recordValue(record,'cinema')],['场次',`${recordValue(record,'quote_date','date_text','quoteDate')||'—'} ${recordValue(record,'showtime_start','showtimeStart')||''}`.trim()],['座位',recordValue(record,'seat_display','seat_zone_type','seatDisplay','seatZoneType')],['原价',recordAmount(record,'original_unit_price_cents','original_unit_price_fen')],['会员价',recordAmount(record,'member_unit_price_cents','member_unit_price_fen')],['报价单价',recordAmount(record,'unit_quote_cents','unit_quote_fen','unit_sell_price_fen')],['报价合计',recordAmount(record,'total_quote_cents','total_quote_fen','total_sell_price_fen')],['规则版本',recordValue(record,'pricing_rule_version','pricing_rule_revision')],['失败原因',info.key==='failed'?recordValue(record,'failure_reason','failureReason'):null]].forEach(([label,value])=>facts.append(overviewValue(label,value)));root.append(facts);
  }
  function renderQuoteRecords(records) {
    records=Array.isArray(records)?records.filter(record=>record&&typeof record==='object'):[];window.__quoteRecords=records;
    const root=$('quoteRecordList');root.replaceChildren();
    const classified=records.map(record=>({record,info:quoteRecordStatus(record)}));
    const failed=classified.filter(item=>item.info.key==='failed').length;
    const succeeded=classified.filter(item=>item.info.key==='success').length;
    const query=String($('quoteRecordSearch')?.value||'').trim().toLowerCase();
    const status=$('quoteRecordStatus')?.value||'';
    const date=$('quoteRecordDate')?.value||'';
    const visible=records.filter(record=>{
      const haystack=['quote_id','record_id','order_id','buyer_id','chat_id','movie','cinema','city'].flatMap(key=>[record[key],record[key.replace(/_([a-z])/g,(_,letter)=>letter.toUpperCase())]]).map(value=>String(value||'').toLowerCase()).join(' ');
       const day=formatRecordDate(record.created_at||record.createdAt);
       const info=quoteRecordStatus(record);
       return(!query||haystack.includes(query))&&(!status||info.key===status)&&(!date||day===date);
    });
    $('quoteRecordTotal').textContent=String(records.length);
    $('quoteRecordSuccess').textContent=String(succeeded);
    $('quoteRecordFailed').textContent=String(failed);
    $('quoteRecordSummary').textContent=`共 ${records.length} 条报价记录 · 当前显示 ${visible.length} 条`;
    if(!visible.length){root.append(element('div','diagnostic-empty',records.length?'没有符合筛选条件的报价记录。':'暂无核价记录。新产生的官方报价及失败原因会自动显示在这里。'));$('quoteRecordDetail').replaceChildren(element('div','detail-panel-empty','暂无可展示报价详情'));return;}
    visible.forEach((record,index)=>{
       const info=quoteRecordStatus(record);const card=element('article',`record-card${index===0?' selected':''}`);card.tabIndex=0;
      const head=element('div','record-card-head');
      const identity=element('div','record-card-identity');
      identity.append(element('strong','',`${recordValue(record,'city')?`${recordValue(record,'city')} · `:''}${recordValue(record,'cinema')||'影院待确认'}`),element('small','',`${recordValue(record,'movie')||'影片待确认'} · ${recordValue(record,'quote_date','date_text')||'日期待确认'} ${recordValue(record,'showtime_start','showtimeStart')||''}
${recordValue(record,'seat_display','seat_zone_type','seatDisplay','seatZoneType')||'座位待确认'}`));
       const statusBadge=element('span',`status-badge record-card-status ${info.className}`,info.label);head.append(identity,statusBadge);card.append(head);
       if(info.key==='failed'){
        card.append(element('div','record-card-failure',recordValue(record,'failure_reason','failureReason')||'未取得权威报价'));
      } else {
        const metrics=element('div','record-card-metrics');
        metrics.append(recordFact('原价',recordAmount(record,'original_unit_price_cents','original_unit_price_fen')),recordFact('会员价',recordAmount(record,'member_unit_price_cents','member_unit_price_fen')),recordFact('报价单价',recordAmount(record,'unit_quote_cents','unit_quote_fen','unit_sell_price_fen')),recordFact('报价合计',recordAmount(record,'total_quote_cents','total_quote_fen','total_sell_price_fen')),recordFact('买家 / 会话',`${recordValue(record,'buyer_id','buyerId')||'—'} · ${recordValue(record,'chat_id','chatId')||'会话待同步'}
${formatRecordTime(recordValue(record,'created_at','createdAt'))}`));
        card.append(metrics);
      }
      const select=()=>{root.querySelectorAll('.record-card').forEach(node=>node.classList.toggle('selected',node===card));renderQuoteRecordDetail(record);};
      card.addEventListener('click',select);card.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();select();}});root.append(card);
    });
    renderQuoteRecordDetail(visible[0]);
  }
  async function loadQuoteRecords() {
    $('quoteRecordList').replaceChildren(element('div','diagnostic-empty','正在读取报价记录…'));
    try{const response=await v4Fetch('/api/plugin/quote-records?limit=500');const data=await response.json();if(!response.ok)throw new Error(data?.detail||'读取报价记录失败');renderQuoteRecords(data.records||[]);}catch(error){$('quoteRecordSummary').textContent='读取失败';$('quoteRecordList').replaceChildren(element('div','diagnostic-empty',error.message));}
  }
  let loadedOrderRows=[];let linkedOrderFacts=new Map();let observedOrderCount=0;let activeOrderFilter='all';let orderSourceWarning='';
  function orderCategory(order){const status=String((order.status||order.orderStatus)??'').toLowerCase();const label=String(order.statusText||order.orderStatusText||'').toLowerCase();if(status==='1'||/待付款|等待付款|pending|wait_pay|created/.test(label)||status==='created')return'unpaid';if(status==='2'||/已付款|支付成功|paid/.test(label)||status==='paid')return'paid';if(status==='3'||/已发货|待收货|shipped|ticket_sent/.test(label)||['shipped','ticket_sent'].includes(status))return'shipped';if(['4','5','6','60','99','completed','cancelled','closed'].includes(status)||/完成|关闭|取消|退款|completed|closed|cancel|refund/.test(label))return'closed';return'other';}
  function linkedFactMap(records){const map=new Map();records.forEach(record=>{const orderId=String(record.order_id||'').trim();if(!orderId||record.status==='failed'||map.has(orderId))return;map.set(orderId,record)});return map;}
  function orderShowtime(row){return row?.showtime||'—';}
  function seatText(value){if(Array.isArray(value))return value.map(item=>item?.seatNo||item?.seat_no||`${item?.rowNo||item?.row_no||''}排${item?.colNo||item?.col_no||''}座`).filter(Boolean).join('、')||'—';return String(value||'—');}
  function wandaOrderRow(order,fact){return{source:'wanda',detailId:String(order.orderId||''),orderId:String(order.orderId||''),movie:(order.fulfillment?.movie_name||fact?.movie||order.productTitle||order.sku||'—'),city:(order.fulfillment?.city||fact?.city||'—'),cinema:(order.fulfillment?.cinema_name||fact?.cinema||'—'),showtime:orderShowtime({showtime:order.fulfillment?.showtime_start?`${order.fulfillment.show_date?`${order.fulfillment.show_date} `:''}${order.fulfillment.showtime_start}${order.fulfillment.showtime_end?` - ${order.fulfillment.showtime_end}`:''}`:`${fact?.quote_date||fact?.date_text||''}${fact?.showtime_start?` ${fact.showtime_start}`:''}`.trim()}),seats:Array.isArray(order.fulfillment?.seats)&&order.fulfillment.seats.length?order.fulfillment.seats.join('、'):fact?.seat_display||fact?.seat_zone_type||'—',ticketMode:'手动出票',marketAmountFen:fact?.base_total_cents,quoteAmountFen:fact?.total_quote_cents,dealAmountFen:order.payment,status:order.orderStatusText||String(order.orderStatus??'未知'),statusText:order.orderStatusText||String(order.orderStatus??'未知'),createdAt:order.createTime||order.observedAt,buyerNick:order.buyerNick||'—',accountUnb:order.accountUnb,raw:order,fact,fulfillment:order.fulfillment||null};}
  function liangpiaoOrderRow(order){return{source:'liangpiao',detailId:String(order.orderNo||order.order_no||order.provider_order_no||''),orderId:String(order.orderNo||order.order_no||order.provider_order_no||''),movie:order.movieName||'—',city:order.cityName||'—',cinema:order.cinemaName||'—',showtime:`${order.startTime||''}${order.endTime?` - ${order.endTime}`:''}`.trim()||'—',seats:seatText(order.seats),ticketMode:order.ticketMode||order.priceMode||'—',marketAmountFen:order.marketAmount,quoteAmountFen:order.quoteAmount,dealAmountFen:order.settleAmount??order.estimatedSettleAmount,status:order.status||'未知',statusText:order.status||'未知',createdAt:order.createdAt,buyerNick:order.buyer_nick||order.buyer_id||'—',outOrderNo:order.out_order_no,providerOrderNo:order.provider_order_no||order.orderNo,raw:order};}
  function orderMatches(row,query){if(!query)return true;return[row.orderId,row.outOrderNo,row.providerOrderNo,row.buyerNick,row.movie,row.city,row.cinema,row.showtime,row.seats,row.ticketMode,row.statusText].some(value=>String(value||'').toLowerCase().includes(query));}
  function renderOrderPreview(row) { const root=$('orderRecordDetail');if(!root||!row)return;root.replaceChildren();const heading=element('div','detail-panel-head');heading.append(element('span','eyebrow','ORDER DETAIL'),element('h3','',row.movie||'订单详情'),element('small','',row.orderId||'—'));root.append(heading);const facts=element('div','detail-fact-list');[['状态',row.statusText],['买家',row.buyerNick],['城市',row.city],['影院',row.cinema],['场次',row.showtime],['座位',row.seats],['出票方式',row.ticketMode],['票面价',centsAmount(row.marketAmountFen)],['报价',centsAmount(row.quoteAmountFen)],['成交价',centsAmount(row.dealAmountFen)],['下单时间',formatRecordTime(row.createdAt)]].forEach(([label,value])=>facts.append(overviewValue(label,value)));root.append(facts); }
  function renderOrders(){
    const query=String($('orderSearch').value||'').trim().toLowerCase();const visible=loadedOrderRows.filter(row=>(activeOrderFilter==='all'||orderCategory(row)===activeOrderFilter)&&orderMatches(row,query));const root=$('orderList');root.replaceChildren();$('orderSummary').textContent=`共 ${loadedOrderRows.length} 笔订单 · 万达手动出票 ${loadedOrderRows.filter(row=>row.source==='wanda').length} 笔 · 良票 ${loadedOrderRows.filter(row=>row.source==='liangpiao').length} 笔 · 当前显示 ${visible.length} 笔${orderSourceWarning?` · ${orderSourceWarning}`:''}`;
    if(!visible.length){root.append(element('div','diagnostic-empty',loadedOrderRows.length?'没有符合当前筛选条件的订单。':'暂无可读取订单。'));$('orderRecordDetail').replaceChildren(element('div','detail-panel-empty','暂无可展示订单详情'));return;}
    const table=document.createElement('table');table.className='order-table';const headers=['影片','城市','影院','场次','座位','出票方式','票面价','报价','成交价','状态','下单时间','咸鱼买家','来源','操作'];const thead=document.createElement('thead');const headRow=document.createElement('tr');headers.forEach(label=>headRow.append(element('th','',label)));thead.append(headRow);const tbody=document.createElement('tbody');visible.forEach((row,rowIndex)=>{const tr=document.createElement('tr');tr.addEventListener('click',()=>renderOrderPreview(row));const values=[row.movie,row.city,row.cinema,row.showtime,row.seats,row.ticketMode,centsAmount(row.marketAmountFen),centsAmount(row.quoteAmountFen),centsAmount(row.dealAmountFen),row.statusText,formatRecordTime(row.createdAt),row.buyerNick,row.source==='liangpiao'?'良票':'万达'];values.forEach((value,index)=>{const cell=element('td',index===9?'order-table-status':'',value);if(index===9)cell.classList.add(`order-status-${orderCategory(row)}`);tr.append(cell);});const actionCell=document.createElement('td');const detail=element('button','order-detail-button','详情');detail.type='button';detail.addEventListener('click',event=>{event.stopPropagation();renderOrderPreview(row);openOrderDetail(row)});actionCell.append(detail);tr.append(actionCell);tbody.append(tr);});table.append(thead,tbody);root.append(table);renderOrderPreview(visible[0]);
  }
  let orderRefreshInFlight=false;
  async function loadOrders() {
    if(orderRefreshInFlight)return; orderRefreshInFlight=true;
    $('orderList').replaceChildren(element('div','diagnostic-empty','正在回读订单、报价和订单来源…'));orderSourceWarning='';
    try{await fishMoreReady;const[orderResponse,factResponse,liangpiaoResponse]=await Promise.all([fishMoreSdk.authedFetch('ui/api/orders?limit=100'),v4Fetch('/api/plugin/quote-records?limit=500'),v4Fetch('/api/plugin/liangpiao-orders?page=1&page_size=100')]);const data=await orderResponse.json();const facts=await factResponse.json();const liangpiaoData=await liangpiaoResponse.json();if(!orderResponse.ok)throw new Error(data?.error||'读取万达订单失败');if(!factResponse.ok)throw new Error(facts?.detail||'读取订单观影信息失败');if(!liangpiaoResponse.ok)orderSourceWarning='良票订单暂不可用';const factsByOrder=linkedFactMap(facts.records||[]);loadedOrderRows=(data.orders||[]).map(order=>wandaOrderRow(order,factsByOrder.get(String(order.orderId)))).concat(liangpiaoResponse.ok?(liangpiaoData.orders||[]).map(liangpiaoOrderRow):[]);loadedOrderRows.sort((a,b)=>String(b.createdAt||'').localeCompare(String(a.createdAt||'')));observedOrderCount=Number(data.observedCount)||0;renderOrders();}catch(error){$('orderSummary').textContent='读取失败';$('orderList').replaceChildren(element('div','diagnostic-empty',error.message));}finally{orderRefreshInFlight=false;}
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
  async function loadReminderSettings(){
    try{const response=await v4Fetch('/api/settings/reminders');const data=await response.json();if(!response.ok)throw new Error(data?.detail||'读取提醒设置失败');$('reminderEnabled').checked=Boolean(data.enabled);$('reminderEnabledText').textContent=data.enabled?'已开启':'已关闭';$('preShowMinutes').value=data.pre_show_minutes;$('postShowMinutes').value=data.post_show_minutes;$('preShowTemplate').value=data.pre_show_template||'';$('reminderMeta').textContent=`修订 ${data.revision||0}${data.updated_at?` · ${new Date(data.updated_at).toLocaleString('zh-CN')}`:''}`;}catch(error){$('reminderMeta').textContent=error.message;}
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
  function renderKnowledge(){const root=$('knowledgeList');root.replaceChildren();const filter=$('knowledgeCategoryFilter').value;const query=String($('knowledgeSearch')?.value||'').trim().toLowerCase();const entries=knowledgeEntries.filter(item=>(!filter||item.category===filter)&&(!query||[item.title,item.category,item.common_questions,item.reply_guidance,item.handling_rules].some(value=>String(value||'').toLowerCase().includes(query))));$('knowledgeTotal').textContent=String(knowledgeEntries.length);$('knowledgeEnabled').textContent=String(knowledgeEntries.filter(item=>item.enabled).length);$('knowledgeCategories').textContent=String(new Set(knowledgeEntries.map(item=>item.category)).size);if(!entries.length){root.append(element('div','diagnostic-empty','暂无匹配知识条目。'));return;}entries.forEach(item=>{const detail=element('details','knowledge-entry');const summary=document.createElement('summary');const title=element('span');title.append(element('b','',item.title),element('small','',`${item.category} · ${item.enabled?'启用':'停用'}`));summary.append(title,element('i',`knowledge-badge ${item.enabled?'retained':'blocked'}`,item.enabled?'启用':'停用'));detail.append(summary);const form=element('div','knowledge-edit');const titleInput=knowledgeField(form,'标题',item.title,'input');const categoryLabel=element('label','', '分类');const category=document.createElement('select');knowledgeCategories.forEach(value=>{const option=document.createElement('option');option.value=value;option.textContent=value;option.selected=value===item.category;category.append(option)});categoryLabel.append(category);form.append(categoryLabel);const questions=knowledgeField(form,'买家常见问法',item.common_questions);const guidance=knowledgeField(form,'回复口径',item.reply_guidance);const rules=knowledgeField(form,'处理规则',item.handling_rules);const enabledLabel=element('label');const enabled=document.createElement('input');enabled.type='checkbox';enabled.checked=Boolean(item.enabled);enabledLabel.append(enabled,document.createTextNode(' 启用此知识条目'));form.append(enabledLabel);const actions=element('div','knowledge-actions');const save=element('button','', '保存');const remove=element('button','', '删除');actions.append(save,remove);form.append(actions);detail.append(form);root.append(detail);save.addEventListener('click',async()=>{save.disabled=true;try{const response=await v4Fetch(`/api/settings/knowledge/${encodeURIComponent(item.id)}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:titleInput.value,category:category.value,common_questions:questions.value,reply_guidance:guidance.value,handling_rules:rules.value,enabled:enabled.checked})});const data=await response.json();if(!response.ok)throw new Error(data?.detail||'知识保存失败');knowledgeEntries=knowledgeEntries.map(value=>value.id===item.id?data:value);renderKnowledge()}catch(error){settingsMessage(error.message,true)}finally{save.disabled=false}});remove.addEventListener('click',async()=>{if(!window.confirm(`确定删除“${item.title}”吗？`))return;remove.disabled=true;try{const response=await v4Fetch(`/api/settings/knowledge/${encodeURIComponent(item.id)}`,{method:'DELETE'});if(!response.ok)throw new Error('知识删除失败');knowledgeEntries=knowledgeEntries.filter(value=>value.id!==item.id);renderKnowledge()}catch(error){settingsMessage(error.message,true)}finally{remove.disabled=false}});});}
  async function loadKnowledge(){try{const response=await v4Fetch('/api/settings/knowledge');const data=await response.json();if(!response.ok)throw new Error(data?.detail||'读取知识库失败');knowledgeEntries=Array.isArray(data.entries)?data.entries:[];renderKnowledge()}catch(error){$('knowledgeList').replaceChildren(element('div','diagnostic-empty',error.message));}}
  async function addKnowledge(){const response=await v4Fetch('/api/settings/knowledge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:'新知识条目',category:'常见问题',common_questions:'请填写买家常见问法。',reply_guidance:'请填写统一回复口径。',handling_rules:'请填写处理规则。',enabled:false})});const data=await response.json();if(!response.ok){settingsMessage(data?.detail||'新增知识失败',true);return}knowledgeEntries.push(data);renderKnowledge();document.querySelector('#knowledgeList details:last-child')?.setAttribute('open','');}
  const templateFields=Object.freeze({recognition_failure_other_template:'recognitionFailureOtherTemplate',recognition_template:'recognitionTemplate',cinema_match_failure_template:'cinemaMatchFailureTemplate',unsupported_cinema_template:'unsupportedCinemaTemplate',missing_fields_template:'missingFieldsTemplate',showtime_changed_template:'showtimeChangedTemplate',exact_quote_template:'exactQuoteTemplate',area_quote_template:'areaQuoteTemplate',quote_unavailable_template:'quoteUnavailableTemplate',quote_expired_template:'quoteExpiredTemplate',no_quote_template:'noQuoteTemplate',guidance_template:'guidanceTemplate',payment_success_pending_ticket_template:'paymentSuccessPendingTicketTemplate',order_shipped_template:'orderShippedTemplate',movie_reminder_template:'movieReminderTemplate',order_pending_without_quote_template:'orderPendingWithoutQuoteTemplate',price_change_confirmation_template:'priceChangeConfirmationTemplate',price_change_failure_template:'priceChangeFailureTemplate',quote_confirmation_clarify_template:'quoteConfirmationClarifyTemplate',quote_ticket_count_request_template:'quoteTicketCountRequestTemplate',quote_quantity_order_guidance_template:'quoteQuantityOrderGuidanceTemplate',quote_quantity_marked_seat_template:'quoteQuantityMarkedSeatTemplate',quote_quantity_flexible_seat_template:'quoteQuantityFlexibleSeatTemplate',quote_quantity_default_seat_template:'quoteQuantityDefaultSeatTemplate',payment_manual_review_template:'paymentManualReviewTemplate',manual_review_template:'manualReviewTemplate',ai_disabled_structured_intake_template:'aiDisabledStructuredIntakeTemplate',order_before_quote_confirmation_template:'orderBeforeQuoteConfirmationTemplate',paid_mismatch_closed_template:'paidMismatchClosedTemplate',paid_mismatch_refund_template:'paidMismatchRefundTemplate',recognition_waiting_template:'recognitionWaitingTemplate',wplus_quote_marker_template:'wplusQuoteMarkerTemplate',wplus_unit_price_reply_template:'wplusUnitPriceReplyTemplate',wplus_marker_confirmation_template:'wplusMarkerConfirmationTemplate',wplus_marker_missing_template:'wplusMarkerMissingTemplate',wplus_mark_required_template:'wplusMarkRequiredTemplate',wplus_marker_confirmed_template:'wplusMarkerConfirmedTemplate',same_type_unavailable_template:'sameTypeUnavailableTemplate',quote_above_fan_price_template:'quoteAboveFanPriceTemplate',liangpiao_ticketed_template:'liangpiaoTicketedTemplate',liangpiao_failed_template:'liangpiaoFailedTemplate',liangpiao_fixed_quote_template:'liangpiaoFixedQuoteTemplate',liangpiao_fixed_failed_template:'liangpiaoFixedFailedTemplate',order_pending_with_quote_template:'orderPendingWithQuoteTemplate',post_order_recognition_reprice_template:'postOrderRecognitionRepriceTemplate',order_submit_unpaid_template:'orderSubmitUnpaidTemplate',order_detected_hold_payment_template:'orderDetectedHoldPaymentTemplate',pending_order_image_quote_unavailable_template:'pendingOrderImageQuoteUnavailableTemplate'});
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
  const extraTemplateLabels={recognition_waiting_template:'识图处理中',wplus_quote_marker_template:'W+标记报价',wplus_unit_price_reply_template:'W+单价回复',wplus_marker_confirmation_template:'W+标记确认',wplus_marker_missing_template:'W+标记缺失',wplus_mark_required_template:'需要标记座位',wplus_marker_confirmed_template:'标记座位已确认',same_type_unavailable_template:'同类场次不可用',quote_above_fan_price_template:'报价高于粉丝价',liangpiao_ticketed_template:'良票已出票',liangpiao_failed_template:'良票失败',liangpiao_fixed_quote_template:'良票固定报价',liangpiao_fixed_failed_template:'良票固定报价失败',order_pending_with_quote_template:'拍下后有有效报价',post_order_recognition_reprice_template:'下单后识图改价',order_submit_unpaid_template:'提交订单未付款',order_detected_hold_payment_template:'检测到待付款订单',pending_order_image_quote_unavailable_template:'待付款截图无法报价'};
  function ensureExtraTemplateFields(){
    const containers=['recognition-quote-flow','order-payment-flow','fulfillment-flow'].map(name=>document.querySelector(`[data-template-section="${name}"]`)).filter(Boolean);
    Object.entries(extraTemplateLabels).forEach(([key,label],index)=>{const id=templateFields[key];if(document.getElementById(id))return;const article=document.createElement('article');article.className='template-card';article.innerHTML=`<div><b>${label}</b><small>此流程节点的后台可配置回复话术。</small></div><label><span>无变量</span><textarea id="${id}"></textarea></label>`;containers[index<9?0:index<13?2:1].appendChild(article);});
  }
  async function loadTemplates() {
    ensureExtraTemplateFields();
    templateMessage('正在读取回复话术…');
    try { const response=await v4Fetch('/api/settings/reply-templates');const data=await response.json();if(!response.ok)throw new Error(data?.detail||'读取话术失败');Object.entries(templateFields).forEach(([key,id])=>{$(id).value=data[key]||''});keywordRules=Array.isArray(data.keyword_replies)?data.keyword_replies.map(rule=>({...rule,keywords:[...(rule.keywords||[])]})):[];renderKeywordRules();$('templateRevision').textContent=`话术修订 ${data.revision||0}${data.updated_at?` · ${new Date(data.updated_at).toLocaleString('zh-CN')}`:''}`;templateMessage(''); }
    catch(error){templateMessage(error.message,true);}
  }
  async function saveTemplates() {
    const button=$('saveTemplates');button.disabled=true;templateMessage('正在校验中文变量并保存…');
    try { const payload=Object.fromEntries(Object.entries(templateFields).map(([key,id])=>[key,$(id).value.trim()]));payload.keyword_replies=keywordRules.map(rule=>({...rule,keywords:(rule.keywords||[]).map(value=>value.trim()).filter(Boolean),reply:String(rule.reply||'').trim()}));const response=await v4Fetch('/api/settings/reply-templates',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await response.json();if(!response.ok)throw new Error(data?.detail||'读取并保存关键词规则失败');keywordRules=data.keyword_replies||[];renderKeywordRules();$('templateRevision').textContent=`话术修订 ${data.revision} · ${new Date(data.updated_at).toLocaleString('zh-CN')}`;templateMessage('回复话术和关键词规则已保存，下一条消息立即使用。'); }
    catch(error){templateMessage(error.message,true);}finally{button.disabled=false;}
  }

  function createLiangpiaoRuleRow({min=0,max=0,markup=0}={}) {
    const row=element('div','liangpiao-rule-row'); row.dataset.ruleRow='true';
    const lower=document.createElement('input'); lower.className='liangpiao-min'; lower.type='number'; lower.min='0'; lower.max='100'; lower.step='0.1'; lower.value=String(min); lower.setAttribute('aria-label','折扣率下限');
    const upper=document.createElement('input'); upper.className='liangpiao-max'; upper.type='number'; upper.min='0'; upper.max='100'; upper.step='0.1'; upper.value=String(max); upper.setAttribute('aria-label','折扣率上限');
    const adjustment=document.createElement('input'); adjustment.className='liangpiao-markup'; adjustment.type='number'; adjustment.min='-100'; adjustment.max='1000'; adjustment.step='0.1'; adjustment.value=String(markup); adjustment.setAttribute('aria-label','调整比例');
    const remove=element('button','liangpiao-remove','删除'); remove.type='button'; remove.addEventListener('click',()=>row.remove());
    row.append(lower,element('span','', '% 至'),upper,element('span','', '%，加价'),adjustment,element('span','', '%'),remove);
    return row;
  }
  function initLiangpiaoRules() {
    const list=$('liangpiaoRuleList'); const add=$('addLiangpiaoRule'); if(!list||!add)return;
    list.querySelectorAll('.liangpiao-remove').forEach(button=>button.addEventListener('click',()=>button.closest('[data-rule-row]')?.remove()));
    add.addEventListener('click',()=>{
      const previous=list.lastElementChild; const previousMax=Number(previous?.querySelector('.liangpiao-max')?.value);
      const min=Number.isFinite(previousMax)?Math.min(100,previousMax):0; const max=Math.min(100,min+10);
      list.append(createLiangpiaoRuleRow({min,max,markup:0}));
    });
  }

  function createWandaRuleRow({min=0,max=0,adjustment=0,legacyFixed=false}={}) {
    const row=element('div','wanda-rule-row'); row.dataset.wandaRuleRow='true'; row.dataset.pricingMode=legacyFixed?'fixed':'percent';
    const lower=document.createElement('input'); lower.className='wanda-min'; lower.type='number'; lower.min='0'; lower.max='100'; lower.step='0.1'; lower.value=String(min); lower.setAttribute('aria-label','万达折扣率下限');
    const upper=document.createElement('input'); upper.className='wanda-max'; upper.type='number'; upper.min='0'; upper.max='100'; upper.step='0.1'; upper.value=String(max); upper.setAttribute('aria-label','万达折扣率上限');
    const fixed=document.createElement('input'); fixed.className='wanda-adjustment'; fixed.type='number'; fixed.min='-1000'; fixed.max='1000'; fixed.step='0.01'; fixed.value=String(adjustment); fixed.setAttribute('aria-label',legacyFixed?'历史固定调整（元）':'加价比例（%）');
    const remove=element('button','wanda-remove','删除'); remove.type='button'; remove.addEventListener('click',()=>row.remove());
    row.append(element('span','', '折扣率'),lower,element('span','', '% 至'),upper,element('span','', legacyFixed?'%，历史固定调整':'%，加价比例'),fixed,element('span','', legacyFixed?'元':'%'),remove);
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

  let preservedRegularAdjustmentCents=100;let preservedOperations={};
  const yuanToCents=id=>Math.round(Number($(id).value)*100);
  function hydratePricingRows(listId,createRow,rows){const list=$(listId);if(!list||!Array.isArray(rows))return;list.replaceChildren();rows.forEach(row=>list.append(createRow(row)));}
  function pricingRows(listId,selectors,mapper){const list=$(listId);if(!list)return[];return [...list.querySelectorAll('[data-rule-row],[data-wanda-rule-row]')].map(row=>{const inputs=[selectors.min,selectors.max,selectors.value].map(selector=>row.querySelector(selector));if(inputs.some(input=>!input||input.value.trim()===''||!input.reportValidity()))throw new Error('请完整填写所有报价区间。');const values=inputs.map(input=>Number(input.value));if(values.some(value=>!Number.isFinite(value)))throw new Error('报价区间必须是有效数字。');return mapper({min:values[0],max:values[1],value:values[2],mode:row.dataset.pricingMode});});}
  function hydrateOperationView(data){
    preservedRegularAdjustmentCents=Number.isInteger(data.regular_adjustment_cents)?data.regular_adjustment_cents:100;
    preservedOperations={wplus_friday_member_day_enabled:data.wplus_friday_member_day_enabled,vip_fixed_cost_cents:data.vip_fixed_cost_cents,vip_discount_threshold_cents:data.vip_discount_threshold_cents,vip_high_price_discount_percent:data.vip_high_price_discount_percent,vip_low_price_discount_cents:data.vip_low_price_discount_cents,liangpiao_price_mode:data.liangpiao_price_mode,liangpiao_fixed_rules:Array.isArray(data.liangpiao_fixed_rules)?data.liangpiao_fixed_rules:[]};
    $('pricingEnabled').checked=data.enabled; $('wplusDiscount').value=(Number(data.wplus_adjustment_cents)/100).toFixed(2); $('wplusThreshold').value=(data.wplus_member_price_threshold_cents/100).toFixed(2); $('roundingIncrement').value=String(data.rounding_increment_cents);
    hydratePricingRows('wandaRuleList',createWandaRuleRow,(data.wanda_rules||[]).map(row=>({min:row.min_discount_percent,max:row.max_discount_percent,adjustment:row.markup_percent??Number(row.fixed_adjustment_cents)/100,legacyFixed:row.markup_percent==null})));
    hydratePricingRows('liangpiaoRuleList',createLiangpiaoRuleRow,(data.liangpiao_rules||[]).map(row=>({min:row.min_discount_percent,max:row.max_discount_percent,markup:row.markup_percent})));
    $('pricingRuleMeta').textContent=`版本 ${data.rule_version} · 修订 ${data.revision}${data.updated_at?` · ${new Date(data.updated_at).toLocaleString('zh-CN')}`:''}`;$('pricingHistoryVersion').textContent=`${data.rule_version||'当前版本'} · r${data.revision??0}`;$('pricingHistoryMeta').textContent=data.updated_at?new Date(data.updated_at).toLocaleString('zh-CN'):'当前生效规则';
  }
  let operationsLoaded=false;
  async function loadOperations() {
    operationsLoaded=false;const saveButton=$('saveOperations');if(saveButton)saveButton.disabled=true;
    try {
      const response=await v4Fetch('/api/settings/operations'); const data=await response.json(); if(!response.ok)throw new Error(data?.detail||'读取运营设置失败');
      hydrateOperationView(data);
      operationsLoaded=true;if(saveButton)saveButton.disabled=false;
    } catch(error) { settingsMessage(error.message,true); }
  }
  async function saveOperations() {
    const button=$('saveOperations'); if(!operationsLoaded){settingsMessage('运营规则尚未成功读取，暂不能保存。',true);return;} button.disabled=true; settingsMessage('正在校验并保存运营报价规则…');
    try {
      const wandaRules=pricingRows('wandaRuleList',{min:'.wanda-min',max:'.wanda-max',value:'.wanda-adjustment'},row=>({min_discount_percent:row.min,max_discount_percent:row.max,...(row.mode==='fixed'?{fixed_adjustment_cents:Math.round(row.value*100)}:{markup_percent:row.value})}));
      const liangpiaoRules=pricingRows('liangpiaoRuleList',{min:'.liangpiao-min',max:'.liangpiao-max',value:'.liangpiao-markup'},row=>({min_discount_percent:row.min,max_discount_percent:row.max,markup_percent:row.value}));
      const payload={...preservedOperations,enabled:$('pricingEnabled').checked,regular_adjustment_cents:preservedRegularAdjustmentCents,wplus_adjustment_cents:yuanToCents('wplusDiscount'),wplus_member_price_threshold_cents:yuanToCents('wplusThreshold'),rounding_increment_cents:Number($('roundingIncrement').value),wanda_rules:wandaRules,liangpiao_rules:liangpiaoRules};
      const response=await v4Fetch('/api/settings/operations',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}); const data=await response.json();
      if(!response.ok)throw new Error(data?.detail?.[0]?.msg||data?.detail||'运营规则保存失败');
      hydrateOperationView(data); operationsLoaded=true; settingsMessage(`运营报价规则已保存，下一次核价立即使用（${data.rule_version}）。`);
    } catch(error) { settingsMessage(error.message,true); }
    finally { button.disabled=!operationsLoaded;refreshDiagnostics(); }
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

  function showSettings(show) { showWorkspace(show?'model':'overview',true); }
  function updatePromptLength() { $('promptLength').textContent=`${$('visionPrompt').value.length} / 20000`; }
  function settingsMessage(message,isError=false) { $('settingsMessage').textContent=message; $('settingsMessage').classList.toggle('error-text',isError); }
  function catalogMessage(message,isError=false,target='modelFetchMessage') { $(target).textContent=message; $(target).classList.toggle('error-text',isError); }
  function updateThinkingHint() {
    const visionModel=$('modelSelect').value.trim().toLowerCase().split('/').pop(); const chatModel=$('chatModelSelect').value.trim().toLowerCase().split('/').pop(); const enabled=$('thinkingToggle').checked;
    $('qwenThinkingGroup').hidden=!visionModel.startsWith('qwen'); $('gptReasoningGroup').hidden=!chatModel.startsWith('gpt-5');
    $('thinkingHint').textContent=enabled?'已开启模型思考，响应时间可能增加。':'思考模式已关闭，优先降低延迟。';
  }
  async function loadModelConfigScopes() {
    const select=$('modelConfigScope');
    try {
      const response=await v4Fetch('/api/plugin/shops'); const data=await response.json();
      if(!response.ok)throw new Error(data?.detail||'读取店铺失败');
      [...(data.shops||[])].forEach(shop=>{const option=document.createElement('option');option.value=`shop:${shop.shop_id}`;option.textContent=`店铺：${shop.shop_name||shop.shop_id}（${shop.shop_id}）`;select.append(option);});
    } catch(error) { catalogMessage(error.message,true); }
  }
  async function loadSettings() {
    settingsMessage('正在读取设置…');
    try {
      const selectedScope=$('modelConfigScope').value;
      modelConfigScope=selectedScope.startsWith('shop:')?'shop':selectedScope;
      modelConfigShopId=selectedScope.startsWith('shop:')?selectedScope.slice(5):'';
      const response=await v4Fetch('/api/settings/vision'); const data=await response.json(); if(!response.ok) throw new Error(data?.detail||'读取设置失败');
      $('baseUrl').value=data.base_url; $('modelSelect').value=data.model; $('chatBaseUrl').value=data.chat_base_url||data.base_url; $('chatModelSelect').value=data.chat_model||data.model; $('thinkingToggle').checked=data.enable_thinking; $('reasoningEffort').value=data.reasoning_effort||'none'; $('visionPrompt').value=data.vision_prompt; $('chatPrompt').value=data.chat_prompt;
      $('modelProviderValue').textContent=data.provider||'OpenAI-compatible';$('modelBaseUrlValue').textContent=data.chat_base_url||data.base_url||'—';$('modelNameValue').textContent=data.chat_model||data.model||'—';$('modelKeyValue').textContent=data.has_chat_api_key?data.masked_chat_api_key:(data.has_api_key?data.masked_api_key:'未配置');$('modelConfigIdValue').textContent=data.config_id||'—';$('modelRevisionValue').textContent=data.config_revision===undefined?'—':`r${data.config_revision}`;$('modelConnectionStatus').textContent=(data.has_chat_api_key||data.has_api_key)?'已配置':'未配置';$('modelConnectionStatus').className=`status-badge ${(data.has_chat_api_key||data.has_api_key)?'success':'warning'}`;
      loadedPrompt=data.vision_prompt; $('currentModel').textContent=`${data.model} → ${data.chat_model||data.model}`; $('keyState').textContent=data.has_api_key?`已保存 ${data.masked_api_key}`:'未配置'; $('keyState').style.color=data.has_api_key?'#29945a':'#b16b20';
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
      loadedPrompt=data.vision_prompt; $('currentModel').textContent=`${data.model} → ${data.chat_model}`; $('keyState').textContent=data.has_api_key?`已保存 ${data.masked_api_key}`:'未配置'; $('keyState').style.color=data.has_api_key?'#29945a':'#b16b20'; $('chatKeyState').textContent=data.has_chat_api_key?`已保存 ${data.masked_chat_api_key}`:'未配置'; $('chatKeyState').style.color=data.has_chat_api_key?'#29945a':'#b16b20'; $('apiKey').value=''; $('chatApiKey').value=''; $('clearApiKey').checked=false; $('clearChatApiKey').checked=false; updateThinkingHint(); settingsMessage('设置已保存，下一张图片立即使用新配置。');
    } catch(error) { settingsMessage(error.message,true); }
    finally { button.disabled=false;refreshDiagnostics(); }
  }
  const workspaceButtons=[...document.querySelectorAll('[data-workspace]')];
  workspaceButtons.forEach((button,index)=>{
    button.addEventListener('click',()=>showWorkspace(button.dataset.workspace));
    button.addEventListener('keydown',event=>{let next=index;if(event.key==='ArrowRight')next=(index+1)%workspaceButtons.length;else if(event.key==='ArrowLeft')next=(index-1+workspaceButtons.length)%workspaceButtons.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=workspaceButtons.length-1;else return;event.preventDefault();showWorkspace(workspaceButtons[next].dataset.workspace,true)});
  });
  $('openSettings').addEventListener('click',()=>showSettings(true)); $('closeSettings').addEventListener('click',()=>showSettings(false)); $('settingsBackdrop').addEventListener('click',()=>showSettings(false)); $('refreshOverview').addEventListener('click',loadOverview); document.querySelectorAll('[data-quick-workspace]').forEach(button=>button.addEventListener('click',()=>showWorkspace(button.dataset.quickWorkspace,true)));
  $('modelConfigScope').addEventListener('change',()=>{const selected=$('modelConfigScope').value;modelConfigScope=selected.startsWith('shop:')?'shop':selected;modelConfigShopId=selected.startsWith('shop:')?selected.slice(5):'';loadSettings();});
  $('modelSettingsTab').addEventListener('click',()=>switchSettingsTab('model')); $('operationsSettingsTab').addEventListener('click',()=>switchSettingsTab('operations'));
  $('useAirelvo').addEventListener('click',()=>{$('baseUrl').value='https://airelvo.cc/v1';catalogMessage('识图接口已切换到 Airelvo。');}); $('fetchModels').addEventListener('click',fetchModels);
  $('useChatAirelvo').addEventListener('click',()=>{$('chatBaseUrl').value='https://airelvo.cc/v1';catalogMessage('AI 回复接口已切换到 Airelvo。',false,'chatModelFetchMessage');}); $('fetchChatModels').addEventListener('click',fetchChatModels);
  $('modelSelect').addEventListener('input',updateThinkingHint); $('chatModelSelect').addEventListener('input',updateThinkingHint); $('thinkingToggle').addEventListener('change',updateThinkingHint); $('reasoningEffort').addEventListener('change',updateThinkingHint);
  $('refreshDiagnostics').addEventListener('click',refreshDiagnostics); $('clearDiagnostics').addEventListener('click',clearDiagnostics); $('refreshAgentAudit').addEventListener('click',loadAgentAudit); $('refreshManualTasks').addEventListener('click',loadManualTasks); $('agentAuditSearch').addEventListener('input',()=>renderAgentAudit()); $('manualTaskSearch').addEventListener('input',()=>renderManualTasks()); $('refreshShops').addEventListener('click',()=>loadShops(true)); $('shopSearch').addEventListener('input',()=>renderShops(loadedShops)); $('shopStatusFilter').addEventListener('change',()=>renderShops(loadedShops)); $('resetShopFilters').addEventListener('click',()=>{$('shopSearch').value='';$('shopStatusFilter').value='';renderShops(loadedShops);}); $('refreshQuoteRecords').addEventListener('click',loadQuoteRecords); ['quoteRecordSearch','quoteRecordStatus','quoteRecordDate'].forEach(id=>$(id)?.addEventListener('input',()=>renderQuoteRecords(window.__quoteRecords||[]))); $('resetQuoteFilters').addEventListener('click',()=>{$('quoteRecordSearch').value='';$('quoteRecordStatus').value='';$('quoteRecordDate').value='';renderQuoteRecords(window.__quoteRecords||[]);}); $('refreshOrders').addEventListener('click',loadOrders); $('saveReminders').addEventListener('click',saveReminderSettings); $('reminderEnabled').addEventListener('change',()=>{$('reminderEnabledText').textContent=$('reminderEnabled').checked?'已开启':'已关闭'}); $('orderSearch').addEventListener('input',renderOrders); $('closeOrderDetail').addEventListener('click',()=>$('orderDetailDialog').close()); $('orderDetailDialog').addEventListener('click',event=>{if(event.target===$('orderDetailDialog'))$('orderDetailDialog').close()});
  document.querySelectorAll('[data-order-filter]').forEach(button=>button.addEventListener('click',()=>{activeOrderFilter=button.dataset.orderFilter;document.querySelectorAll('[data-order-filter]').forEach(item=>item.classList.toggle('active',item===button));renderOrders()}));
  document.querySelectorAll('[data-template-filter]').forEach(button=>button.addEventListener('click',()=>{document.querySelectorAll('[data-template-filter]').forEach(item=>item.classList.toggle('active',item===button));document.querySelectorAll('[data-template-section]').forEach(section=>section.hidden=section.dataset.templateSection!==button.dataset.templateFilter)})); $('addKeywordRule').addEventListener('click',addKeywordRule); $('addKnowledge').addEventListener('click',addKnowledge); $('knowledgeCategoryFilter').addEventListener('change',renderKnowledge);
  document.querySelectorAll('[data-template-variable]').forEach(button=>button.addEventListener('click',async()=>{try{await copyTemplateVariable(button.dataset.templateVariable);templateMessage(`已复制 ${button.dataset.templateVariable}`);}catch{templateMessage('复制失败，请选中变量后复制。',true);}}));
  $('saveSettings').addEventListener('click',saveSettings); $('saveOperations').addEventListener('click',saveOperations); $('saveTemplates').addEventListener('click',saveTemplates); $('saveConversationPolicy').addEventListener('click',saveConversationPolicy); $('visionPrompt').addEventListener('input',updatePromptLength); $('knowledgeSearch').addEventListener('input',renderKnowledge); $('resetPrompt').addEventListener('click',()=>{$('visionPrompt').value=loadedPrompt;updatePromptLength()});
  document.addEventListener('keydown',event=>{if(event.key==='Escape')showSettings(false)});
  showWorkspace(location.hash.replace('#','')||'overview'); updateSend(); initLiangpiaoRules(); initWandaRules(); loadModelConfigScopes().finally(loadSettings); loadOperations(); loadConversationPolicy(); loadKnowledge(); refreshDiagnostics(); setInterval(()=>{if(!document.hidden&&activeWorkspace==='overview')loadOverview();},30000);
