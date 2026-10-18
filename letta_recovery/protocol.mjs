import {randomUUID} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import {CONTRACT,canonical,digest,safeId,sameBinding,validateBinding} from './contracts.mjs';
import {readJSON} from './payload-store.mjs';
import {CheckpointStore} from './checkpoint-store.mjs';
import {OwnershipManager} from './ownership.mjs';
import {RecoveryExecution} from './execution.mjs';

export class RecoveryProtocol{
 constructor({root,sdk,connectionId,acquireLock,storeId}){
  this.root=root;this.sdk=sdk;this.store=new CheckpointStore({root,acquireLock});this.ownership=new OwnershipManager({store:this.store,connectionId});
  this.execution=new RecoveryExecution({store:this.store,ownership:this.ownership,sdk,gate:sdk.gate()});
  this.jobs=new Map();this.claims=new Map();this.closed=false;this.pending=Promise.resolve();
  try{this.storeId=readJSON(join(root,'identity.json')).store_id;}catch(error){if(error.code!=='ENOENT')throw error;this.storeId=storeId??randomUUID();}
 }
 _status(binding,attemptId){
  const snapshot=this.store.load(binding);if(!snapshot)return {status:'missing',hold_reason:'legacy_unproven'};
  const attempt=attemptId?snapshot.attempts?.[attemptId]:null;
  return {...this.store.inspect(binding),run_id:snapshot.run_id,attempt_id:attemptId??null,state:attempt?(snapshot.phase==='terminal_committed'?'complete':snapshot.phase==='held'?'held':this.jobs.has(attemptId)?'running':'interrupted'):'missing',terminal:snapshot.terminal??null};
 }
 _assertOpen(){if(this.closed)throw Error('authority_lost');}
 _claim(command){
  this._assertOpen();const entry=this.claims.get(command.claim_id),claim=entry?.claim;
  if(!claim||claim.fence!==command.fence||claim.admission_id!==command.admission_id||!sameBinding(entry.binding,command.binding))throw Error('authority_lost');
  this.ownership.assertOwned(claim);return claim;
 }
 _scope(command,claim){
  if(!safeId(command.admission_id)||typeof command.scope_token!=='string'||command.scope_token.length!==64)throw Error('authority_lost');
  if(claim&&claim.admission_id!==command.admission_id)throw Error('authority_lost');
  this.sdk.setScope(command.binding,{admission_id:command.admission_id,scope_token:command.scope_token,claim:()=>this.ownership.currentClaim(claim)});
 }
 handleRecoveryCommand(command){
  // Serialize connection-local scope changes, including validation across awaits.
  const pending=this.pending.then(()=>this._handleRecoveryCommand(command));
  this.pending=pending.catch(()=>{});return pending;
 }
 async _handleRecoveryCommand(command){
  this._assertOpen();
  if(command.type==='recovery_capabilities')return {contract:CONTRACT,version:1,store_id:this.storeId};
  const binding=validateBinding(command.binding);if(binding.store_id!==this.storeId)throw Error('binding_mismatch');
  if(['recovery_inspect','recovery_status','recovery_subscribe'].includes(command.type))return this._status(binding,command.attempt_id);
  if(command.type==='recovery_result_read'){
   const claim=this._claim(command);this._scope(command,claim);
   await this.sdk.checkScope(binding,this.ownership.currentClaim(claim));this._claim(command);
   const snapshot=this.store.load(binding);if(!snapshot)throw Error('legacy_unproven');
   const item=[...snapshot.steps,...snapshot.tools].find(item=>item.admission_id===command.effect_admission_id);
   if(!item?.result_ref)throw Error('provider_uncertain');return {admission_id:item.admission_id,result:JSON.parse(this.store.readPayload(binding,item.result_ref)),result_sha:item.result_ref};
  }
  if(command.type==='recovery_admit'){
   this._scope(command,null);
   await this.sdk.checkScope(binding,{admission_id:command.admission_id,fence:1,boot_id:readFileSync('/proc/sys/kernel/random/boot_id','utf8').trim(),expires_monotonic_ms:Number(process.hrtime.bigint()/1000000n)+15000});
   this._assertOpen();this.store.initialize(this.storeId);
   const existing=this.store.load(binding);
   if(existing){if(digest(command.input)!==binding.input_sha)throw Error('binding_mismatch');return this._status(binding);}
   const snapshot=await this.execution.admitInput(binding,command.input);return this._status(snapshot.binding);
  }
  if(command.type==='recovery_claim'){
   this._scope(command,null);
   const claim=await this.ownership.claim(binding,command.expected_revision,command.admission_id);
   try{
    this._assertOpen();this.claims.set(claim.claim_id,{claim,binding});this._scope(command,claim);
    await this.sdk.checkScope(binding,claim);this._assertOpen();this.ownership.assertOwned(claim);
   }catch(error){this.claims.delete(claim.claim_id);await this.ownership.release(claim);throw error;}
   return {...claim,revision:this.store.load(binding).revision};
  }
  if(command.type==='recovery_resume'){
   if(!safeId(command.attempt_id))throw Error('binding_mismatch');
   const intent={attempt_id:command.attempt_id,binding,expected_revision:command.expected_revision,claim_id:command.claim_id,fence:command.fence,admission_id:command.admission_id};
   const claim=this._claim(command);
   const snapshot=this.store.load(binding),existing=snapshot?.attempts?.[command.attempt_id];
   if(existing){if(canonical(existing.intent)!==canonical(intent))throw Error('binding_mismatch');return this._status(binding,command.attempt_id);}
   this._scope(command,claim);await this.sdk.checkScope(binding,this.ownership.currentClaim(claim));this._claim(command);
   if(snapshot?.phase==='held')throw Error(snapshot.hold_reason);
   this.store.commit(binding,command.expected_revision,{type:'resume',intent,claim_id:claim.claim_id,fence:claim.fence});
   // The durable acknowledgement leaves before the detached worker can dispatch.
   const timer=setTimeout(()=>this._run(binding,claim,command.attempt_id),10);timer.unref();this.jobs.set(command.attempt_id,timer);
   return this._status(binding,command.attempt_id);
  }
  if(command.type==='recovery_renew')return this.ownership.renew(this._claim(command));
  if(['recovery_revoke','recovery_release'].includes(command.type)){
   const claim=this._claim(command);await this.ownership.revoke(claim);this.claims.delete(claim.claim_id);return this._status(binding,command.attempt_id);
  }
  if(command.type==='recovery_ack'){
   const claim=this._claim(command);this.store.commit(binding,command.expected_revision,{type:'ack',terminal_id:command.terminal_id,consumer_id:command.consumer_id,fence:claim.fence,claim_id:claim.claim_id});return this._status(binding,command.attempt_id);
  }
  throw Error('unsupported_contract');
 }
 async _run(binding,claim,attemptId){
  try{
   for(let count=0;count<1024;count++){
    this.ownership.assertOwned(claim);const snapshot=this.store.load(binding);
    await this.execution.projectTranscript(snapshot);
    if(['held','terminal_committed'].includes(snapshot.phase))return;
    await this.execution.advance(binding,claim);
   }
   throw Error('storage_limit');
  }catch(error){
   try{this.ownership.assertOwned(claim);const snapshot=this.store.load(binding);
    const reason=['projection_conflict','storage_limit','owner_blocked','configuration_changed','unsupported_contract'].includes(error.message)?error.message:'state_corrupt';
    if(!['held','terminal_committed'].includes(snapshot.phase))this.store.commit(binding,snapshot.revision,{type:'hold',reason,fence:claim.fence,claim_id:claim.claim_id});
   }catch{/* A lost owner cannot write a hold over its successor. */}
  }finally{this.jobs.delete(attemptId);}
 }
 async close(){
  this.closed=true;
  for(const timer of this.jobs.values())clearTimeout(timer);
  this.jobs.clear();
  for(const {claim} of this.claims.values())await this.ownership.revoke(claim);
  this.claims.clear();
 }
}
