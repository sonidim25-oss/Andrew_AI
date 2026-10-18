import {spawn} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {randomUUID} from 'node:crypto';
import {once} from 'node:events';
import {safeId,sameBinding} from './contracts.mjs';

const LEASE_MS=15000,RENEW_MS=5000;
const monotonic=()=>Number(process.hrtime.bigint()/1000000n);
async function executionLock(helper,path){
 const child=spawn('/usr/bin/python3',[helper,'--path',path],{stdio:['pipe','pipe','ignore']});
 try{
  await new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{child.kill();reject(Error('authority unavailable'));},5000);
   child.once('error',()=>{clearTimeout(timer);reject(Error('authority unavailable'));});
   child.once('exit',()=>{clearTimeout(timer);reject(Error('authority owned'));});
   child.stdout.once('data',data=>{clearTimeout(timer);let message;try{message=JSON.parse(data.toString());}catch{reject(Error('authority unavailable'));return;}
    if(message.state==='owned')resolve();else reject(Error(message.error==='owned'?'authority owned':'authority unavailable'));});
  });
  return child;
 }catch(error){child.stdin.end();child.kill();throw error;}
}
async function closeLock(child){
 if(child.exitCode!==null||child.signalCode!==null)return;
 const done=once(child,'exit').catch(()=>{});child.stdin.end('release\n');
 const timer=setTimeout(()=>child.kill('SIGKILL'),1000);timer.unref();
 await done;clearTimeout(timer);
}
export class OwnershipManager{
 constructor({store,lockHelper=fileURLToPath(new URL('./lock-helper.py',import.meta.url)),clock=monotonic,bootId=()=>readFileSync('/proc/sys/kernel/random/boot_id','utf8').trim(),connectionId=randomUUID()}){
  this.store=store;this.lockHelper=lockHelper;this.clock=clock;this.bootId=typeof bootId==='function'?bootId:()=>bootId;
  this.ownerId=randomUUID();this.connectionId=connectionId;this.owned=new Map();
 }
 async claim(binding,expectedRevision,admissionId){
  if(!safeId(admissionId))throw Error('invalid authority admission');
  const snapshot=this.store.load(binding);if(!snapshot||snapshot.revision!==expectedRevision)throw Error('stale revision');
  const child=await executionLock(this.lockHelper,this.store.checkpointPath(binding)+'.execution');
  try{
   const now=this.clock(),boot=this.bootId();if(!Number.isFinite(now)||!safeId(boot))throw Error('authority unavailable');
   const claim={claim_id:randomUUID(),admission_id:admissionId,owner_id:this.ownerId,connection_id:this.connectionId,boot_id:boot,
    fence:snapshot.fence+1,revision:expectedRevision+1,expires_monotonic_ms:now+LEASE_MS};
   this.store.commit(binding,expectedRevision,{type:'claim',claim},[]);
   const entry={claim,binding,child,lastNow:now,lost:false,timer:null};
   child.once('exit',()=>{entry.lost=true;clearInterval(entry.timer);});
   child.stdin.on('error',()=>{entry.lost=true;clearInterval(entry.timer);});
   entry.timer=setInterval(()=>{this.renew(claim).catch(()=>{entry.lost=true;clearInterval(entry.timer);child.kill();});},RENEW_MS);entry.timer.unref();
   this.owned.set(claim.claim_id,entry);return {...claim};
  }catch(error){await closeLock(child);throw error;}
 }
 assertOwned(claim){
  const entry=this.owned.get(claim?.claim_id);if(!entry||entry.lost||entry.child.exitCode!==null||entry.child.signalCode!==null)throw Error('authority lost');
  const now=this.clock();if(!Number.isFinite(now)||now<entry.lastNow||now>=entry.claim.expires_monotonic_ms||this.bootId()!==entry.claim.boot_id||claim.fence!==entry.claim.fence||claim.owner_id!==this.ownerId){entry.lost=true;throw Error('authority lost');}
  entry.lastNow=now;const current=this.store.load(entry.binding);
  if(!current||current.fence!==claim.fence||current.claim?.claim_id!==claim.claim_id||current.claim.owner_id!==this.ownerId||!sameBinding(current.binding,entry.binding)){entry.lost=true;throw Error('authority lost');}
 }
 async renew(claim){
  this.assertOwned(claim);const entry=this.owned.get(claim.claim_id),current=this.store.load(entry.binding);
  const renewed={...entry.claim,revision:current.revision+1,expires_monotonic_ms:this.clock()+LEASE_MS};
  this.store.commit(entry.binding,current.revision,{type:'renew',claim:renewed},[]);entry.claim=renewed;return {...renewed};
 }
 async release(claim){
  const entry=this.owned.get(claim?.claim_id);if(!entry)return;
  entry.lost=true;clearInterval(entry.timer);
  try{
   const current=this.store.load(entry.binding);
   if(current?.claim?.claim_id===claim.claim_id&&current.fence===claim.fence)
    this.store.commit(entry.binding,current.revision,{type:'release',claim_id:claim.claim_id,fence:claim.fence},[]);
  }finally{this.owned.delete(claim.claim_id);await closeLock(entry.child);}
 }
 async revoke(claim){await this.release(claim);}
}
