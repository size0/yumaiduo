import {createHmac} from 'node:crypto';

export function createBackendRequest({url, secret, timeoutMs=15000, fetchImpl=fetch}) {
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 120000)
    throw new Error('INVALID_BACKEND_TIMEOUT');
  return async function backend(path, method, body) {
    const raw=body===undefined?'':JSON.stringify(body);
    const timestamp=String(Date.now());
    const signature=createHmac('sha256',secret()).update(`${timestamp}.${raw}`).digest('hex');
    const controller=new AbortController();
    let timer;
    // Deadline includes response body consumption. Never retry requests here:
    // claim/authorization may have committed even if their response was lost.
    const deadline=new Promise((_,reject)=>{
      timer=setTimeout(()=>{
        controller.abort();
        reject(Object.assign(new Error('BACKEND_REQUEST_TIMEOUT'),{code:'BACKEND_REQUEST_TIMEOUT'}));
      },timeoutMs);
    });
    try {
      return await Promise.race([deadline,(async()=>{
        const response=await fetchImpl(`${url}${path}`,{method,
          headers:{'content-type':'application/json','x-yumaiduo-timestamp':timestamp,
            'x-yumaiduo-signature':signature},
          body:body===undefined?undefined:raw,signal:controller.signal,redirect:'error'});
        return {status:response.status,data:await response.json().catch(()=>null)};
      })()]);
    } finally { clearTimeout(timer); }
  };
}
