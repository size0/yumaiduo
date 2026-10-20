import test from 'node:test';
import assert from 'node:assert/strict';
import {createHmac} from 'node:crypto';
import {serveUi} from '../src/ui.mjs';

const gateway = {secret:'unit-test-secret', pluginId:'wanda-seat-autoquote'};
const tenant = '107';
function request(url, method='GET', raw=Buffer.alloc(0)) {
  const timestamp = String(Date.now());
  return {url, method, headers:{
    'x-yumaiduo-plugin-id':gateway.pluginId,
    'x-yumaiduo-tenant-id':tenant,
    'x-yumaiduo-timestamp':timestamp,
    'x-yumaiduo-signature':createHmac('sha256',gateway.secret)
      .update(`${timestamp}.${raw}`).digest('hex'),
  }};
}
function response() {
  return {status:null, body:null, writeHead(status) {this.status=status;},
    end(body) {this.body=body;}};
}

test('unsigned UI requests cannot reach the backend', async t => {
  const fetch = t.mock.method(globalThis,'fetch',async()=>{throw new Error('must not proxy');});
  const req = request('/ui/api/templates');
  delete req.headers['x-yumaiduo-signature'];
  const res = response();
  await serveUi(req,res,Buffer.alloc(0),'http://backend',null,tenant,gateway);
  assert.equal(res.status,401);
  assert.equal(fetch.mock.callCount(),0);
});

test('signed preview preserves the selected channel', async t => {
  t.mock.method(globalThis,'fetch',async url => {
    assert.equal(url.pathname,'/ui/api/preview/QUOTE_CARD');
    assert.equal(url.search,'?channel=wanda');
    return new Response('{"parts":["wanda"]}',{headers:{'content-type':'application/json'}});
  });
  const res = response();
  await serveUi(request('/ui/api/preview/QUOTE_CARD?channel=wanda'),res,
    Buffer.alloc(0),'http://backend',null,tenant,gateway);
  assert.equal(res.status,200);
});

test('signed template save forwards the exact body', async t => {
  const raw=Buffer.from('{"key":"QUOTE_CARD","channel":"wanda","text":"edited"}');
  t.mock.method(globalThis,'fetch',async (_url, options) => {
    assert.equal(options.method,'PUT');
    assert.deepEqual(options.body,raw);
    assert.equal(options.headers['content-type'],'application/json');
    return new Response('{"ok":true}');
  });
  const res=response();
  await serveUi(request('/ui/api/templates','PUT',raw),res,raw,
    'http://backend',null,tenant,gateway);
  assert.equal(res.status,200);
});

test('wrong tenant or modified body is rejected before forwarding', async t => {
  const fetch=t.mock.method(globalThis,'fetch',async()=>{throw new Error('must not proxy');});
  const req=request('/ui/api/templates','PUT',Buffer.from('{}'));
  const res=response();
  await serveUi(req,res,Buffer.from('{"changed":true}'),'http://backend',null,tenant,gateway);
  assert.equal(res.status,401);
  req.headers['x-yumaiduo-tenant-id']='other';
  await serveUi(req,response(),Buffer.from('{}'),'http://backend',null,tenant,gateway);
  assert.equal(fetch.mock.callCount(),0);
});
