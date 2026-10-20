window.recordAppliedDates = {start:"",end:""};
window.recordOffset = 0;
window.recordParams = function(kind, status) {
  const params = new URLSearchParams({kind, status, offset:String(window.recordOffset)});
  params.set('q',document.getElementById('record-query').value);
  params.set('start',window.recordAppliedDates.start);params.set('end',window.recordAppliedDates.end);
  const shop=document.getElementById('record-shop').value;
  if(shop){const [tenant,id]=JSON.parse(shop);params.set('tenant_id',tenant);params.set('shop_id',id);}
  return params.toString();
};
window.recordPage = function(page) {
  const p=page || {total:0,offset:0,size:50};
  document.getElementById('record-page').textContent=`共 ${p.total} 条 · 第 ${Math.floor(p.offset/p.size)+1} 页`;
  document.getElementById('record-prev').disabled=p.offset===0;
  document.getElementById('record-next').disabled=!p.more;
};
window.bindRecordFilters = function(load, resetStatus) {
  const get=id=>document.getElementById(id);
  const run=async()=>{
    const {start,end}=window.recordAppliedDates;
    if(start && end && start>end){get('record-error').textContent='开始日期不能晚于结束日期';return;}
    get('record-error').textContent='';
    try {await load();} catch(e){get('record-error').textContent='加载失败：'+e.message;}
  };
  const dateLabel=()=>{
    const {start,end}=window.recordAppliedDates;
    get('record-date-label').textContent=start||end?`${start||'不限开始'} 至 ${end||'不限结束'}`:'全部日期';

  };
  const applyDates=()=>{
    if(get('record-start').value && get('record-end').value && get('record-start').value>get('record-end').value){get('record-date-error').textContent='开始日期不能晚于结束日期';return;}
    window.recordAppliedDates={start:get('record-start').value,end:get('record-end').value};
    get('record-date-error').textContent='';dateLabel();get('record-date-picker').open=false;window.recordOffset=0;run();
  };
  get('record-date-picker').ontoggle=()=>{if(get('record-date-picker').open)return;get('record-start').value=window.recordAppliedDates.start;get('record-end').value=window.recordAppliedDates.end;get('record-date-error').textContent='';};
  get('record-date-apply').onclick=applyDates;
  get('record-date-clear').onclick=()=>{get('record-start').value='';get('record-end').value='';applyDates();};
  document.querySelectorAll('[data-days]').forEach(button=>button.onclick=()=>{
    const parts=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());
    const part=k=>parts.find(p=>p.type===k).value;
    const today=`${part('year')}-${part('month')}-${part('day')}`;
    const previous=n=>{const d=new Date(today+'T12:00:00Z');d.setUTCDate(d.getUTCDate()-n);return d.toISOString().slice(0,10);};
    const days=button.dataset.days;
    get('record-start').value=days==='all'?'':days==='month'?today.slice(0,8)+'01':days==='2'?previous(1):previous(Number(days)-1);
    get('record-end').value=days==='all'?'':days==='2'?previous(1):today;
    applyDates();
  });
  document.addEventListener('pointerdown',e=>{if(!get('record-date-picker').contains(e.target))get('record-date-picker').open=false;});
  get('record-date-picker').onkeydown=e=>{if(e.key==='Escape'){get('record-date-picker').open=false;get('record-date-label').focus();}};
  get('record-shop').onchange=()=>{window.recordOffset=0;run();};
  get('record-shop').disabled=true;
  fetch(window.withBase('/api/shops')).then(async r=>{const data=await r.json();if(!r.ok||data.ok===false||data.error_code)throw new Error(data.message||'店铺列表暂不可用');return data;}).then(data=>{
    for(const shop of data.shops||[]){const o=document.createElement('option');o.value=JSON.stringify([String(shop.tenant_id||data.tenant_id||''),String(shop.shop_id)]);o.textContent=shop.display_name||shop.shop_id;get('record-shop').append(o);}
    get('record-shop-status').textContent=(data.shops||[]).length?'':'暂无已授权店铺';
    get('record-shop').disabled=false;
  }).catch(()=>{get('record-shop-status').textContent='店铺列表加载失败，请刷新重试';});
  get('record-search').onclick=()=>{window.recordOffset=0;run();};
  get('record-query').onkeydown=e=>{if(e.key==='Enter'){window.recordOffset=0;run();}};
  get('record-reset').onclick=()=>{for(const id of ['record-query','record-start','record-end','record-shop'])get(id).value='';window.recordAppliedDates={start:'',end:''};get('record-date-picker').open=false;window.recordOffset=0;resetStatus();dateLabel();run();};
  get('record-prev').onclick=()=>{window.recordOffset=Math.max(0,window.recordOffset-50);run();};
  get('record-next').onclick=()=>{window.recordOffset+=50;run();};
};
