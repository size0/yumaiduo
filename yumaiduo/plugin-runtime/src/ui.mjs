import {uploadReplyImage} from './reply-image.mjs';

export async function serveUi(req, res, rawBody, backendUrl, client=null, tenant='') {
  const url = new URL(req.url, 'http://plugin.local');
  const path = url.pathname;
  if (path !== '/ui' && !path.startsWith('/ui/')) return false;
  if (path === '/ui') {
    res.writeHead(302, {location: '/ui/'}); res.end(); return true;
  }
  const settings = path === '/ui/api/settings';
  if (path === '/ui/api/settings/reprice-image' && req.method === 'POST') {
    try {
      const image=await uploadReplyImage(JSON.parse(rawBody.toString('utf8')),client,tenant);
      res.writeHead(200,{'content-type':'application/json','cache-control':'no-store'});
      res.end(JSON.stringify(image));
    } catch(error) {
      res.writeHead(400,{'content-type':'application/json'});
      res.end(JSON.stringify({detail:error.message}));
    }
    return true;
  }
  const modelSettings = /^\/ui\/api\/model-connection\/(test|models|save)$/.test(path);
  const preview = ['/ui/api/pricing-preview', '/ui/api/settings/preview'].includes(path);
  const prepare = path === '/ui/api/fulfillment/prepare';
  const review = /^\/ui\/api\/fulfillment\/[a-zA-Z0-9-]+\/(review|delivery-review)$/.test(path);
  const humanAction = /^\/ui\/api\/fulfillment\/[a-zA-Z0-9-]+\/(approve|execute|deliver|approve-and-execute)$/.test(path);
  const aftersalesRead = /^\/ui\/api\/fulfillment\/[a-zA-Z0-9-]+\/aftersales$/.test(path);
  const aftersalesWrite = /^\/ui\/api\/fulfillment\/[a-zA-Z0-9-]+\/aftersales\/(cancel|urge|refund)$/.test(path);
  const api = aftersalesRead || aftersalesWrite || modelSettings || preview || prepare || review || humanAction || ['/ui/api/settings', '/ui/api/shops', '/ui/api/quotes', '/ui/api/orders', '/ui/api/fulfillment/context'].includes(path);
  if (!(req.method === 'GET' || ((modelSettings || preview || prepare || humanAction || aftersalesWrite) && req.method === 'POST') || ((settings || path === '/ui/api/shops') && req.method === 'PUT'))) {
    res.writeHead(405); res.end(); return true;
  }
  const upstream = await fetch(new URL(path + url.search, backendUrl), {
    method: req.method,
    headers: api ? {'content-type': 'application/json', ...(aftersalesWrite ? {'x-v5-aftersales-token':req.headers['x-v5-aftersales-token'] || ''} : {})} : {},
    body: ['PUT','POST'].includes(req.method) ? rawBody : undefined,
    signal: AbortSignal.timeout(modelSettings?30000:10000), redirect: 'manual',
  });
  const headers = {'content-type': upstream.headers.get('content-type') || 'application/octet-stream', 'cache-control': 'no-store', 'x-content-type-options': 'nosniff'};
  res.writeHead(upstream.status, headers);
  res.end(Buffer.from(await upstream.arrayBuffer()));
  return true;
}
