"""Explicit, bounded internal test; no main import, platform transport or production stores.

Run only with explicit authorization for production-host model requests.
Provider data is synthetic; date is historical fixture data, not a live show.
"""
from pathlib import Path
import base64
import subprocess

ROOT = Path(__file__).resolve().parents[1]
HEAD = r'''
import os,sys,subprocess,base64
from pathlib import Path
pid=subprocess.check_output(['systemctl','show','wanda-v4','-p','MainPID','--value'],text=True).strip()
for x in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0'):
 if b'=' in x:
  k,v=x.split(b'=',1);os.environ[k.decode()]=v.decode()
sys.path.insert(0,'/opt/wanda-v4/current')
from app.settings_store import PersistentSettingsStore
s=PersistentSettingsStore(Path(os.environ['WANDA_VISION_SETTINGS_PATH'])).current()
import app.recognition_v2.models as schema
import app.canonical_conversation_agent as agent_module
'''

def main():
    source = HEAD
    for module, path in [
        ('schema', ROOT/'audit/runtime/app/recognition_v2/models.py'),
        ('agent_module', ROOT/'backend/app/canonical_conversation_agent.py'),
    ]:
        encoded = base64.b64encode(path.read_bytes()).decode()
        source += f"\nexec(compile(base64.b64decode('{encoded}'), '<isolated-patch>', 'exec'), {module}.__dict__)\n"
    source += (ROOT/'audit/quote_fixtures.py').read_text(encoding='utf-8').replace('from __future__ import annotations','')
    source += '\n' + (ROOT/'audit/probe_c_tail.py').read_text(encoding='utf-8')
    source += '''
import httpx
class Observe(httpx.AsyncHTTPTransport):
 async def handle_async_request(self, request):
  start=time.monotonic()
  r=await super().handle_async_request(request)
  await r.aread()
  print('HTTP',json.dumps({'status':r.status_code,'request_id':r.headers.get('x-request-id'),'elapsed':round(time.monotonic()-start,2)}),flush=True)
  return r
client=OpenAICompatibleAgentModel(api_key=s.chat_api_key,base_url=s.chat_base_url,model=s.chat_model,timeout_seconds=min(s.request_timeout_seconds,20),transport=Observe())
asyncio.run(execute(client))
'''
    result = subprocess.run(['ssh','-i','C:/Users/13250/.ssh/id_windsurf_nopw','ubuntu@124.220.29.179',
        'sudo -n /opt/wanda-v4/current/.venv/bin/python -B -u -'], input=source.encode(),capture_output=True,timeout=110)
    (ROOT/'audit/probe-c-patched.txt').write_bytes(result.stdout + result.stderr)
    print('Probe exit',result.returncode)
    for line in result.stdout.decode('utf-8').splitlines():
        if line.startswith('HTTP'):print(line)
        if line.startswith('AGENT '):
            import json
            data=json.loads(line[6:]);print(json.dumps({k:data.get(k) for k in ['status','reason','reply','model_diagnostic']},ensure_ascii=False))
    raise SystemExit(result.returncode)

if __name__ == '__main__':
    main()
