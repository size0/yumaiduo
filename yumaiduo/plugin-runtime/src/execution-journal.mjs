// A durable record of local execution attempts and immutable receipts.
// Backend CommandStore remains the only business/execution authorization authority.
import {createHash, randomUUID} from 'node:crypto';
import {mkdirSync, openSync, writeFileSync, fsyncSync, closeSync, renameSync,
  linkSync, unlinkSync, existsSync, readFileSync, readdirSync} from 'node:fs';
import {resolve, join} from 'node:path';

function syncDirectory(path) {
  // Windows does not support opening directories for fsync. Linux production does.
  if (process.platform==='win32') return;
  const fd=openSync(path,'r');
  try { fsyncSync(fd); } finally { closeSync(fd); }
}
function read(path) { return existsSync(path)?JSON.parse(readFileSync(path,'utf8')):null; }
function write(path, name, value, immutable=false) {
  const temp=join(path,`.${name}.${randomUUID()}.tmp`), target=join(path,name);
  const fd=openSync(temp,'wx',0o600);
  try { writeFileSync(fd,JSON.stringify(value)); fsyncSync(fd); } finally { closeSync(fd); }
  try {
    if (immutable) linkSync(temp,target); else renameSync(temp,target);
    syncDirectory(path);
  } finally { if (existsSync(temp)) unlinkSync(temp); }
}
function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value==='object')
    return Object.fromEntries(Object.keys(value).sort().map(k=>[k,canonical(value[k])]));
  return value;
}
function fingerprint(command) {
  const fields=['command_id','type','tenant_id','shop_id','order_id','buyer_id','chat_id',
    'quote_id','target_amount_fen','ticket_version','text','image'];
  return createHash('sha256').update(JSON.stringify(canonical(
    Object.fromEntries(fields.filter(k=>command[k]!==undefined).map(k=>[k,command[k]]))))).digest('hex');
}

export class ExecutionJournal {
  constructor(directory) {
    if (!directory) throw new Error('EXECUTION_JOURNAL_DIRECTORY_REQUIRED');
    this.directory=resolve(directory);
    mkdirSync(this.directory,{recursive:true,mode:0o700});
    this.running=new Set();
    this.reporting=false;
  }
  path(id) {
    if (typeof id!=='string' || !id) throw new Error('COMMAND_ID_REQUIRED');
    return join(this.directory,createHash('sha256').update(id).digest('hex'));
  }
  begin(command) {
    const path=this.path(command.command_id);
    try { mkdirSync(path,{mode:0o700}); }
    catch (error) {
      if (error.code!=='EEXIST') throw error;
      const previous=read(join(path,'started.json'));
      if (previous && previous.fingerprint!==fingerprint(command)) throw new Error('COMMAND_IDENTITY_CHANGED');
      return false;
    }
    syncDirectory(this.directory);
    write(path,'started.json',{command_id:command.command_id,type:command.type,
      fingerprint:fingerprint(command),started_at:new Date().toISOString()},true);
    return true;
  }
  finish(id,result) {
    if (!result || typeof result.status!=='string') throw new Error('EXECUTION_RESULT_REQUIRED');
    write(this.path(id),'result.json',result,true);
  }
  entries() {
    return readdirSync(this.directory,{withFileTypes:true}).filter(x=>x.isDirectory() && /^[a-f0-9]{64}$/.test(x.name))
      .map(x=>{
        const path=join(this.directory,x.name);
        return {path,started:read(join(path,'started.json')),result:read(join(path,'result.json')),
          ack:read(join(path,'ack.json')),retry:read(join(path,'retry.json'))};
      });
  }
  inspect() {
    const entries=this.entries();
    return {receipts_pending:entries.filter(e=>e.result && !e.ack).length,
      recovery_required:entries.filter(e=>!e.started || (!e.result && !this.running.has(e.started.command_id)) || e.retry?.manual)
        .map(e=>({command_id:e.started?.command_id ?? null,type:e.started?.type ?? null,
          reason:e.retry?.reason || 'EXECUTION_RESULT_MISSING',automatic_retry:false})),
      backend_recovery:read(join(this.directory,'backend-recovery.json'))};
  }
}

export async function executeOnce(journal,command,execute) {
  if (!journal.begin(command)) return;
  journal.running.add(command.command_id);
  try {
    const result=await execute(command);
    journal.finish(command.command_id,result);
  } finally { journal.running.delete(command.command_id); }
  // If execution threw or result persistence failed, leave the attempt unresolved.
  // Do not invent failure or re-enter the action after restart.
}

function acknowledged(response,entry) {
  const {data,status}=response, result=entry.result;
  if (status!==200 || data?.command_id!==entry.started.command_id || data.status!==result.status) return false;
  if (result.status==='SENT' && data.sent_message_id!==result.sent_message_id) return false;
  if (result.status==='SUCCEEDED' && data.price_after_fen!==result.price_after_fen) return false;
  return true;
}

export async function flushReceipts(journal,backend,now=Date.now()) {
  if (journal.reporting) return;
  journal.reporting=true;
  try {
    // Bound each poll. Receipts use their original ID and byte-equivalent JSON facts.
    const pending=journal.entries().filter(e=>e.started && e.result && !e.ack && !e.retry?.manual &&
      (e.retry?.next_at ?? 0)<=now).slice(0,10);
    for (const entry of pending) {
      let response;
      try { response=await backend(`/api/v1/commands/${encodeURIComponent(entry.started.command_id)}/result`,'POST',entry.result); }
      catch { response={status:0,data:null}; }
      if (acknowledged(response,entry)) {
        if (!existsSync(join(entry.path,'ack.json'))) write(entry.path,'ack.json',{acknowledged_at:new Date().toISOString()},true);
        continue;
      }
      const attempts=(entry.retry?.attempts ?? 0)+1;
      const permanent=response.status>=400 && response.status<500 && ![408,429].includes(response.status);
      write(entry.path,'retry.json',{attempts,http_status:response.status,
        reason:permanent?'RESULT_RECEIPT_REJECTED':'RESULT_RECEIPT_UNCONFIRMED',
        manual:permanent || attempts>=8,next_at:now+Math.min(60000,1000*2**(attempts-1))});
    }
  } finally { journal.reporting=false; }
}

export async function inspectRecovery(journal,backend) {
  const response=await backend('/api/v1/recovery/inspect','POST',{});
  if (response.status!==200 || !Array.isArray(response.data?.commands)) throw new Error('RECOVERY_INSPECTION_FAILED');
  const project=(items,key)=>(items||[]).map(item=>({[key]:item[key],type:item.type,status:item.status,
    action:item.action,automatic_retry:false}));
  write(journal.directory,'backend-recovery.json',{checked_at:new Date().toISOString(),
    commands:project(response.data.commands,'command_id'),events:project(response.data.events,'event_id'),
    bookings:project(response.data.bookings,'booking_id')});
}
