import manifest from '../yumaiduo.plugin.json' with { type: 'json' };

export async function registerPlugin({ coreUrl, baseUrl, developerToken, fetchImpl=fetch }) {
  if (!coreUrl || !baseUrl || !developerToken) throw new Error('PLUGIN_REGISTRATION_CONFIG_MISSING');
  const response=await fetchImpl(`${coreUrl.replace(/\/$/,'')}/api/v1/plugin/runtime/register`,{method:'POST',headers:{'content-type':'application/json','x-plugin-developer-token':developerToken},body:JSON.stringify({manifest,baseUrl})});
  const payload=await response.json().catch(()=>null);
  if(!response.ok) throw new Error(payload?.message||`PLUGIN_REGISTRATION_FAILED_${response.status}`);
  return payload?.data||payload;
}
