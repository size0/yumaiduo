(() => {
  const params = new URLSearchParams(location.search);
  const tenant = params.get('tenant_id') || '', shop = params.get('shop_id') || '';
  const base = window.__DSH_BASE__ || '/';
  const nativeFetch = window.fetch.bind(window);
  let busy = false;
  const ready = new Set();
  window.shopPageReady = kind => ready.has(kind);
  window.markShopReady = kind => {
    ready.add(kind);
    document.querySelectorAll('[data-shop-push="'+kind+'"] button,[data-shop-push="'+kind+'"] select').forEach(e=>e.disabled=false);
    document.querySelectorAll('.shop-source').forEach(e=>e.disabled=false);
  };
  window.fetch = (input, opts) => {
    if (typeof input === 'string' || input instanceof URL) {
      const u = new URL(input, location.href);
      if (u.origin === location.origin && /\/api\/(assistant|settings|templates|config-push|model-test|preview\/[^/]+)$/.test(u.pathname)) {
        if (shop && tenant) { u.searchParams.set('tenant_id', tenant); u.searchParams.set('shop_id', shop); }
        input = u.href;
      }
    }
    return nativeFetch(input, opts);
  };
  function activeKind() { return document.querySelector('#panel-replies.active') ? 'templates' : document.querySelector('#cfg-body') ? 'features' : 'assistant'; }
  function positionBars() {
    const main = document.querySelector('.app-main');
    if (main) document.documentElement.style.setProperty('--action-left', Math.max(12,main.getBoundingClientRect().left+24)+'px');
  }
  window.addEventListener('resize',positionBars);positionBars();
  const unusedEscape = s => String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  document.querySelectorAll('[data-shop-push]').forEach(bar => {
    bar.classList.add('floating-actions');
    const kind=bar.dataset.shopPush;
    const tools=document.createElement('div');tools.className='shop-push-tools';
    tools.innerHTML='<span class="shop-push-caption">复制到其他店铺，覆盖该页配置</span><select aria-label="推送目标店铺"><option value="">选择目标店铺</option></select><button type="button" class="btn primary">保存并推送</button><span class="push-feedback" role="status"></span>';
    bar.appendChild(tools);
    bar.querySelectorAll('button,select').forEach(e=>e.disabled=true);
    tools.querySelector('button').onclick=async()=>{
      const select=tools.querySelector('select'),status=tools.querySelector('[role=status]');
      if (busy) return;
      if (!ready.has(kind)) { status.textContent='页面尚未加载完成，请刷新后重试';return; }
      if (!select.value) { status.textContent='请先选择目标店铺';return; }
      const target=JSON.parse(select.value);busy=true;
      document.querySelectorAll('.shop-source,.shop-push-tools button,.shop-push-tools select').forEach(e=>e.disabled=true);
      status.textContent='保存并推送中…';
      try {
        if (!await window.saveShopPage(kind)) throw new Error('当前修改尚未保存，请先处理保存提示');
        const res=await fetch(base+'api/config-push',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind,tenant_id:target[0],shop_id:target[1]})});
        const data=await res.json();if(!res.ok||!data.ok)throw new Error(data.error||'推送失败');
        status.textContent='已推送到 '+select.selectedOptions[0].textContent+'，立即生效';
      } catch(e) { status.textContent='未完成：'+e.message; }
      finally {busy=false;document.querySelectorAll('.shop-source,.shop-push-tools button,.shop-push-tools select').forEach(e=>e.disabled=false);}
    };
  });
  const headers=document.querySelectorAll('#panel-assistant .page-header,#panel-replies .page-header,body[data-config-page] .page-header');
  headers.forEach(header=>{
    const label=document.createElement('label');label.className='shop-source-label';label.innerHTML='当前配置 <select class="shop-source" aria-label="当前配置店铺"><option value="">公共默认配置</option></select>';
    header.appendChild(label);
    label.querySelector('select').disabled=true;
    label.querySelector('select').onchange=async e=>{
      if (busy) return;
      const selected=e.target.value;
      if (!ready.has(activeKind())) {e.target.value=shop?JSON.stringify([tenant,shop]):'';return;}
      if (!await window.saveShopPage(activeKind())) {e.target.value=shop?JSON.stringify([tenant,shop]):'';return;}
      const u=new URL(location.href);u.searchParams.delete('tenant_id');u.searchParams.delete('shop_id');
      if(selected){const v=JSON.parse(selected);u.searchParams.set('tenant_id',v[0]);u.searchParams.set('shop_id',v[1]);}
      location.assign(u.href);
    };
  });
  nativeFetch(base+'api/shops').then(async r=>{if(!r.ok)throw new Error('店铺列表读取失败');return r.json();}).then(data=>{
    const shops=data.shops||[];
    for(const select of document.querySelectorAll('.shop-source,.shop-push-tools select')){
      for(const row of shops){if(!row.tenant_id||!row.shop_id)continue;if(!select.classList.contains('shop-source')&&String(row.shop_id)===shop&&String(row.tenant_id)===tenant)continue;
        const op=document.createElement('option');op.value=JSON.stringify([String(row.tenant_id),String(row.shop_id)]);op.textContent=row.display_name||row.shop_name||row.shop_id;select.appendChild(op);}
      if(select.classList.contains('shop-source'))select.value=shop?JSON.stringify([tenant,shop]):'';
    }
    document.querySelectorAll('[data-shop-push] .push-tip').forEach(e=>e.textContent=shop?'保存仅对当前店铺生效；推送会覆盖目标店铺的本页配置。':'当前为公共默认配置；未设置独立配置的店铺沿用它。');
  }).catch(e=>document.querySelectorAll('.push-feedback').forEach(x=>x.textContent=e.message));
})();
