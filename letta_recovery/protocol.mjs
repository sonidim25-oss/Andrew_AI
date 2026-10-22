import {randomUUID} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import {CONTRACT,canonical,digest,safeId,sameBinding,validateBinding} from './contracts.mjs';
import {readJSON} from './payload-store.mjs';
import {CheckpointStore} from './checkpoint-store.mjs';
import {OwnershipManager} from './ownership.mjs';
import {RecoveryExecution} from './execution.mjs';
import {validateCommand} from './wire.mjs';
import {adopt,assertNotRestored} from './adoption.mjs';

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
  return {...this.store.inspect(binding),run_id:snapshot.run_id,attempt_id:attemptId??null,state:attempt?(snapshot.phase==='terminal_committed'?'complete':snapshot.phase==='held'?'held':this.jobs.has(attemptId)?'running':'interrupted'):'missing',runner_receipt_admissions:snapshot.tools.filter(item=>item.state==='confirmed'&&item.runner_receipt).map(item=>item.admission_id),effect_admission_ids:[...snapshot.steps,...snapshot.tools].filter(item=>item.admission_id).map(item=>item.admission_id),terminal:snapshot.terminal??null};
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
  this._assertOpen();validateCommand(command);
  if(command.type==='recovery_capabilities')return {contract:CONTRACT,version:1,store_id:this.storeId,owner_handoff:1,late_provider_receipt:1,local_diary_reconciliation:1,runner_result_recovery_v1:1,held_tool_result_v1:1,runner_tool_evidence_v1:1};
  const binding=validateBinding(command.binding);if(binding.store_id!==this.storeId)throw Error('binding_mismatch');
  if(['recovery_inspect','recovery_status','recovery_subscribe'].includes(command.type))return this._status(binding,command.attempt_id);
  assertNotRestored(this.root);
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
   let existing;try{existing=this.store.load(binding);}catch(error){if(error.message!=='binding mismatch'||!command.previous_binding)throw error;}
   if(existing){if(digest(command.input)!==binding.input_sha)throw Error('binding_mismatch');return this._status(binding);}
   await this.sdk.assertAdoptable(binding);this._assertOpen();adopt(this.root,binding);
   const snapshot=command.previous_binding?this.store.admitNext(binding,command.input,command.previous_binding,command.expected_revision):await this.execution.admitInput(binding,command.input);return this._status(snapshot.binding);
  }
  if(['recovery_claim','recovery_yield','recovery_reconcile_provider','recovery_reconcile_local_diary','recovery_import_runner_tool'].includes(command.type)){
   this._scope(command,null);
   const claim=await this.ownership.claim(binding,command.expected_revision,command.admission_id);
   try{
    this._assertOpen();this.claims.set(claim.claim_id,{claim,binding});this._scope(command,claim);
    if(command.type==='recovery_reconcile_provider'){
     await this.sdk.checkEvidence(binding,claim);this._assertOpen();
     await this.execution.reconcileProvider(binding,claim,command.effect_admission_id);
    }else if(command.type==='recovery_import_runner_tool'){
     // Historical evidence only: records a saved runner result, grants no continuation.
     await this.sdk.checkEvidence(binding,claim);this._assertOpen();
     const repaired=await this.execution.reconcileTool(binding,claim,command.effect_admission_id);
     await this.execution.projectTranscript(repaired,claim);
    }else if(command.type==='recovery_reconcile_local_diary'){
     await this.sdk.checkEvidence(binding,claim);this._assertOpen();
     const repaired=await this.execution.reconcileLocalDiary(binding,claim,command.effect_admission_id);
     await this.execution.projectTranscript(repaired,claim);
    }else if(command.type==='recovery_yield'){
     await this.sdk.checkEvidence(binding,claim);
     const yielded=await this.execution.yieldOwner(binding,claim);await this.execution.projectTranscript(yielded,claim);
    }else if(this.store.load(binding).phase==='terminal_committed')await this.sdk.checkEvidence(binding,claim);else await this.sdk.checkScope(binding,claim);
    this._assertOpen();this.ownership.assertOwned(claim);
   }catch(error){this.claims.delete(claim.claim_id);await this.ownership.release(claim);throw error;}
   return {...claim,revision:this.store.load(binding).revision};
  }
  if(command.type==='recovery_reconcile_tool'){
   const claim=this._claim(command);this._scope(command,claim);
   if(this.store.load(binding).revision!==command.expected_revision)throw Error('stale revision');
   await this.sdk.checkEvidence(binding,this.ownership.currentClaim(claim));this._claim(command);
   if(this.store.load(binding).revision!==command.expected_revision)throw Error('stale revision');
   const repaired=await this.execution.reconcileTool(binding,claim,command.effect_admission_id);
   this._claim(command);await this.execution.projectTranscript(repaired,claim);
   return this._status(binding,command.attempt_id);
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
   const claim=this._claim(command),snapshot=this.store.load(binding);this._scope(command,claim);
   if(snapshot.terminal?.id!==command.terminal_id||binding.consumer_id!==command.consumer_id)throw Error('invalid transition');
   const evidence=await this.sdk.settlement(binding,this.ownership.currentClaim(claim),command.terminal_id);this._claim(command);
   const admissions=[...new Set([...snapshot.steps,...snapshot.tools].filter(item=>item.admission_id).map(item=>item.admission_id).concat(snapshot.terminal?.cancelled_admission_ids??[]))].sort();
   if(evidence?.settled!==true||!sameBinding(evidence.binding,binding)||evidence.terminal_id!==command.terminal_id||canonical(evidence.admission_ids)!==canonical(admissions))throw Error('provider_uncertain');
   this.store.commit(binding,command.expected_revision,{type:'ack',terminal_id:command.terminal_id,consumer_id:command.consumer_id,settled:true,fence:claim.fence,claim_id:claim.claim_id});return this._status(binding,command.attempt_id);
  }
  throw Error('unsupported_contract');
 }
 async _run(binding,claim,attemptId){
  try{
   for(let count=0;count<1024;){
    this.ownership.assertOwned(claim);const snapshot=this.store.load(binding);
    await this.execution.projectTranscript(snapshot,claim);
    if(['held','terminal_committed'].includes(snapshot.phase))return;
    const advanced=await this.execution.advance(binding,claim);
    // Waiting for Retry-After does not consume the bounded work-step budget.
    if(!(snapshot.phase==='provider_backoff'&&advanced.phase==='provider_backoff'
         &&snapshot.steps.at(-1).rejection.attempt===advanced.steps.at(-1).rejection.attempt))count++;
   }
   throw Error('storage_limit');
  }catch(error){
   if(error.message==='authority_lost')return; // Lost evidence transport never invents an unknown result.
   if(error.message==='owner_blocked'){
    try{const yielded=await this.execution.yieldOwner(binding,claim);await this.execution.projectTranscript(yielded,claim);return;}catch{/* Owner changes and unknown effects remain fenced. */}
   }
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
