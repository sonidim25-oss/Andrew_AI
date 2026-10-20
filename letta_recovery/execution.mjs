import {randomUUID} from 'node:crypto';
import {unlinkSync,readdirSync,lstatSync} from 'node:fs';
import {join} from 'node:path';
import {canonical,digest,sameBinding,safeId,validRejection,validateRunnerReceipt} from './contracts.mjs';
import {syncDirectory} from './payload-store.mjs';
import {projectTranscript} from './transcript-projection.mjs';

export class RecoveryExecution{
 constructor({store,ownership,gate,sdk}){this.store=store;this.ownership=ownership;this.gate=gate;this.sdk=sdk;}
 async admitInput(binding,input){if(digest(input)!==binding.input_sha)throw Error('binding mismatch');return this.store.commit(binding,null,{type:'input',input});}
 _read(binding,ref){return JSON.parse(this.store.readPayload(binding,ref).toString('utf8'));}
 _commit(binding,claim,event){this.ownership.assertOwned(claim);const current=this.store.load(binding);return this.store.commit(binding,current.revision,{...event,fence:claim.fence,claim_id:claim.claim_id});}
 _hold(binding,claim,reason){return this._commit(binding,claim,{type:'hold',reason});}
 async yieldOwner(binding,claim){
  const state=this.store.load(binding);
  if(state.phase==='terminal_committed'){
   if(state.terminal?.stop_reason!=='owner_input_pending')throw Error('owner_blocked');return state;
  }
  const phases=['input_committed','provider_admitted','tool_admitted','provider_confirmed','provider_backoff','tool_prepared','tool_confirmed','continuation_ready'];
  if(!(phases.includes(state.phase)||(state.phase==='held'&&['owner_blocked','provider_uncertain','tool_uncertain'].includes(state.hold_reason)&&phases.includes(state.held_phase)))
    ||state.steps.some(item=>!['admitted','confirmed','rejected'].includes(item.state))||state.tools.some(item=>!['admitted','prepared','confirmed'].includes(item.state)))throw Error('owner_blocked');
  const proof=await this.gate.ownerYield(binding,this.ownership.currentClaim(claim));this.ownership.assertOwned(claim);
  const ids=[...state.steps,...state.tools].filter(item=>item.admission_id).map(item=>item.admission_id).sort();
  const cancelled=proof?.cancelled_admission_ids;
  if(proof?.state!=='owner_yield'||!safeId(proof.receipt_id)||!sameBinding(proof.binding,binding)
    ||!Array.isArray(cancelled)||!cancelled.every(safeId)||new Set(cancelled).size!==cancelled.length
    ||[...state.steps,...state.tools].some(item=>item.state==='admitted'&&!cancelled.includes(item.admission_id))
    ||cancelled.some(id=>[...state.steps,...state.tools].some(item=>item.admission_id===id&&item.state!=='admitted'))
    ||canonical(proof.admission_ids)!==canonical([...new Set([...ids,...cancelled])].sort()))throw Error('owner_blocked');
  return this._commit(binding,claim,{type:'owner_yield',terminal:{id:randomUUID(),run_id:state.run_id,consumer_id:binding.consumer_id,
   stop_reason:'owner_input_pending',assistant_ids:[],yield_receipt_id:proof.receipt_id,cancelled_admission_ids:cancelled}});
 }
 async reconcileProvider(binding,claim,admissionId){
  this.ownership.assertOwned(claim);const state=this.store.load(binding),item=state.steps.at(-1);
  if(!item||item.admission_id!==admissionId||!['provider','compaction'].includes(item.kind??'provider'))throw Error('provider_uncertain');
  const held=state.phase==='held'&&state.hold_reason==='provider_uncertain'&&state.held_phase==='provider_admitted'&&item.state==='admitted';
  const repeated=(state.phase==='provider_confirmed'&&item.state==='confirmed')||(state.phase==='provider_backoff'&&item.state==='rejected');
  if(!held&&!repeated)throw Error('provider_uncertain');
  const proof=await this.gate.result({admission_id:admissionId,binding,effect_id:item.effect_id,kind:item.kind??'provider'});
  this.ownership.assertOwned(claim);
  const rejected=validRejection(proof,item);
  const confirmed=proof?.state==='confirmed'&&proof.admission_id===item.admission_id&&proof.kind===(item.kind??'provider')
   &&proof.request_sha===item.request_sha&&proof.result_sha===digest(proof.result)&&proof.usage?.settled===true;
  if(!rejected&&!confirmed)throw Error('provider_uncertain');
  if(repeated){
   if((confirmed&&item.result_ref===proof.result_sha)||(rejected&&canonical(item.rejection)===canonical(proof)))return state;
   throw Error('provider_uncertain');
  }
  return this._commit(binding,claim,{type:'provider_reconcile',effect_id:item.effect_id,admission_id:admissionId,
   request_sha:item.request_sha,...(rejected?{rejection:proof}:{result:proof.result,usage:proof.usage})});
 }
 async reconcileLocalDiary(binding,claim,admissionId){
  this.ownership.assertOwned(claim);
  const state=this.store.load(binding),tool=state.tools[0];
  if(state.tools.length!==1||state.steps.length!==1||state.steps[0].state!=='confirmed'
    ||tool.name!=='save_diary_entry'||tool.admission_id!==admissionId)throw Error('tool_uncertain');
  const proof=await this.gate.result({binding,admission_id:admissionId,effect_id:tool.effect_id,kind:'tool'});
  this.ownership.assertOwned(claim);
  const receipt=proof?.usage?.local_diary_reconciliation;
  const result=JSON.stringify({status:'error',error:'diary_not_committed',committed:false,evidence:'quiescent_local_diary_ledger'});
  if(proof?.state!=='confirmed'||proof.kind!=='tool'||proof.admission_id!==admissionId||proof.request_sha!==tool.request_sha
    ||proof.result!==result||proof.result_sha!==digest(result)||receipt?.contract!=='local_diary_destination_reconciliation_v1'
    ||receipt.committed!==false||typeof receipt.evidence_sha!=='string'||!/^[a-f0-9]{64}$/.test(receipt.evidence_sha))throw Error('tool_uncertain');
  if(state.phase==='terminal_committed'){
   if(tool.state!=='confirmed'||tool.result_ref!==proof.result_sha||state.terminal?.error_category!=='diary_not_committed'
     ||state.terminal.maintenance_receipt_sha!==receipt.evidence_sha)throw Error('tool_uncertain');
   return state;
  }
  return this._commit(binding,claim,{type:'local_diary_reconcile',effect_id:tool.effect_id,admission_id:admissionId,
   request_sha:tool.request_sha,result:proof.result,terminal:{id:randomUUID(),run_id:state.run_id,consumer_id:binding.consumer_id,
    stop_reason:'error',assistant_ids:[],result_admission_id:admissionId,error_category:'diary_not_committed',maintenance_receipt_sha:receipt.evidence_sha}});
 }
 async reconcileTool(binding,claim,admissionId){
  this.ownership.assertOwned(claim);const state=this.store.load(binding),tool=state.tools.find(item=>item.admission_id===admissionId);
  const held=state.phase==='held'&&state.hold_reason==='tool_uncertain'&&state.held_phase==='tool_admitted'&&tool?.state==='admitted';
  const repeated=state.phase==='tool_confirmed'&&tool?.state==='confirmed'&&tool.runner_receipt;
  if(!tool||(!held&&!repeated)||state.steps.at(-1)?.state!=='confirmed'||tool.provider_id!==state.steps.at(-1).effect_id
   ||state.steps.some(item=>!['confirmed','rejected'].includes(item.state))||state.tools.some(item=>item!==tool&&item.state==='admitted'))throw Error('tool_uncertain');
  const proof=await this.gate.result({binding,admission_id:admissionId,effect_id:tool.effect_id,kind:'tool'});
  this.ownership.assertOwned(claim);
  if(this.store.load(binding).revision!==state.revision)throw Error('stale revision');
  const receipt=validateRunnerReceipt(proof,binding,tool,this._read(binding,tool.arguments_ref));
  if(repeated){if(tool.result_ref!==proof.result_sha||canonical(tool.runner_receipt)!==canonical(receipt))throw Error('tool_uncertain');return state;}
  return this._commit(binding,claim,{type:'runner_tool_confirm',effect_id:tool.effect_id,admission_id:admissionId,
   request_sha:tool.request_sha,result:proof.result,runner_receipt:receipt});
 }
 async _confirm(binding,claim,kind,item){
  let proof;try{proof=await this.gate.result({admission_id:item.admission_id,binding,effect_id:item.effect_id,kind});}catch{throw Error('authority_lost');}
  if(kind!=='tool'&&validRejection(proof,item))
   return this._commit(binding,claim,{type:'provider_reject',effect_id:item.effect_id,rejection:proof});
  if(proof?.state!=='confirmed'||proof.admission_id!==item.admission_id||proof.result_sha!==digest(proof.result)
    ||(kind!=='tool'&&proof.usage?.settled!==true)){
   try{return await this.yieldOwner(binding,claim);}catch{/* Only durable controller cancellation can prove no dispatch. */}
   return this._hold(binding,claim,kind==='tool'?'tool_uncertain':'provider_uncertain');
  }
  let runnerReceipt;
  if(kind==='tool'&&(proof.runner_receipt!==undefined||proof.usage?.runner_receipt!==undefined)){
   try{runnerReceipt=validateRunnerReceipt(proof,binding,item,this._read(binding,item.arguments_ref));}
   catch{return this._hold(binding,claim,'tool_uncertain');}
  }
  return this._commit(binding,claim,{type:kind==='tool'?'tool_confirm':'provider_confirm',effect_id:item.effect_id,result:proof.result,usage:proof.usage,
   ...(runnerReceipt?{runner_receipt:runnerReceipt}:{}),...(kind==='tool'&&['sleep','wake_at'].includes(proof.terminal_stop)?{terminal_stop:proof.terminal_stop}:{})});
 }
 async advance(binding,claim){
  this.ownership.assertOwned(claim);const state=this.store.load(binding);if(!state)throw Error('missing checkpoint');
  if(state.phase==='held'||state.phase==='terminal_committed')return state;
  if(['input_committed','continuation_ready'].includes(state.phase)){
   const context=this._read(binding,state.context_ref??state.input_ref);
   const planned=this.sdk.prepareProviderStep?await this.sdk.prepareProviderStep(binding,context):{kind:'provider',request:this.sdk.prepareProviderRequest?await this.sdk.prepareProviderRequest(binding,context):context};
   if(!['provider','compaction'].includes(planned.kind))throw Error('invalid provider step');
   const request=planned.request;
   const effect={kind:planned.kind,effect_id:'p-'+digest({run_id:state.run_id,index:state.steps.length}),request_sha:digest(request)};
   const admission=await this.gate.authorize(binding,this.ownership.currentClaim(claim),effect);this.ownership.assertOwned(claim);
   const admitted=this._commit(binding,claim,{type:'provider_admit',...effect,admission_id:admission.admission_id,request,context:planned.context??context});
   try{await this.sdk.executeProviderStep(request,admission);}catch{/* Only the gate's exact settled evidence can close this gap. */}
   return this._confirm(binding,claim,planned.kind,admitted.steps.at(-1));
  }
  if(state.phase==='provider_admitted')return this._confirm(binding,claim,state.steps.at(-1).kind??'provider',state.steps.at(-1));
  if(state.phase==='provider_backoff'){
   if(this.sdk.checkScope)await this.sdk.checkScope(binding,this.ownership.currentClaim(claim));
   const item=state.steps.at(-1),kind=item.kind??'provider';
   let proof;try{proof=await this.gate.result({admission_id:item.admission_id,binding,effect_id:item.effect_id,kind});}catch{throw Error('authority_lost');}
   // Only CURRENT controller evidence authorizes a retry. An old checkpoint's
   // rejection cannot authorize a retry whose newer response was lost.
   if(!validRejection(proof,item)){
    this._commit(binding,claim,{type:'provider_retry',effect_id:item.effect_id});
    return this._confirm(binding,claim,kind,item);
   }
   if(proof.retry_at_ms>Date.now()){
    await new Promise(resolve=>setTimeout(resolve,Math.min(1000,proof.retry_at_ms-Date.now())));
    this.ownership.assertOwned(claim);return this.store.load(binding);
   }
   const request=this._read(binding,item.request_ref);
   if(digest(request)!==item.request_sha)throw Error('state_corrupt');
   const effect={kind,effect_id:item.effect_id,request_sha:item.request_sha};
   const admission=await this.gate.authorize(binding,this.ownership.currentClaim(claim),effect);this.ownership.assertOwned(claim);
   this._commit(binding,claim,{type:'provider_retry',effect_id:item.effect_id});
   try{await this.sdk.executeProviderStep(request,admission);}catch{/* The receipt decides, never the transport error. */}
   return this._confirm(binding,claim,kind,item);
  }
  if(state.phase==='tool_admitted')return this._confirm(binding,claim,'tool',state.tools.find(item=>item.state==='admitted'));
  if(state.phase==='provider_confirmed'){
   const step=state.steps.at(-1),response=this._read(binding,step.result_ref);
   const choice=response?.choices?.length===1?response.choices[0]:null;
   if(!choice||choice.message?.role!=='assistant')return this._hold(binding,claim,'state_corrupt');
   if(step.kind==='compaction'){
    if(choice.finish_reason!=='stop'||typeof choice.message.content!=='string'||!choice.message.content.trim()||choice.message.tool_calls?.length||typeof this.sdk.completeCompaction!=='function')return this._hold(binding,claim,'state_corrupt');
    const context=await this.sdk.completeCompaction(this._read(binding,step.context_ref),response);
    return this._commit(binding,claim,{type:'compaction',context});
   }
   const calls=choice.message.tool_calls??[];
   if(calls.length){
    const input=this._read(binding,state.input_ref),allowed=new Set((input.tools??[]).map(tool=>tool.function?.name));
    try{
     if(!Array.isArray(calls)||calls.length>256)throw Error();
     const prepared=calls.map(call=>{
      if(!safeId(call.id)||!allowed.has(call.function?.name)||call.type!=='function')throw Error();
      const args=JSON.parse(call.function.arguments);if(!args||typeof args!=='object'||Array.isArray(args))throw Error();
      return {effect_id:'t-'+digest({run_id:state.run_id,provider_id:step.effect_id,call_id:call.id}),provider_id:step.effect_id,call_id:call.id,name:call.function.name,arguments:args,request_sha:digest({name:call.function.name,arguments:args})};
     });
     return this._commit(binding,claim,{type:'tool_prepare',calls:prepared});
    }catch{return this._hold(binding,claim,'tool_uncertain');}
   }
   if(choice.finish_reason!=='stop'||typeof choice.message.content!=='string'||!choice.message.content.trim())return this._hold(binding,claim,'state_corrupt');
   const terminal={id:randomUUID(),run_id:state.run_id,consumer_id:binding.consumer_id,stop_reason:'end_turn',assistant_ids:['provider:'+step.effect_id],result_admission_id:step.admission_id};
   return this._commit(binding,claim,{type:'terminal',terminal});
  }
  if(['tool_prepared','tool_confirmed'].includes(state.phase)){
   const lifecycle=state.tools.find(item=>item.state==='confirmed'&&['sleep','wake_at'].includes(item.terminal_stop));
   if(lifecycle){
    if(state.tools.some(item=>item.state==='admitted'))return this._hold(binding,claim,'tool_uncertain');
    return this._commit(binding,claim,{type:'lifecycle_terminal',terminal:{id:randomUUID(),run_id:state.run_id,consumer_id:binding.consumer_id,
     stop_reason:'end_turn',assistant_ids:['tool:'+lifecycle.effect_id],result_admission_id:lifecycle.admission_id,lifecycle_stop:lifecycle.terminal_stop}});
   }
   const tool=state.tools.find(item=>item.state==='prepared');
   if(tool){
    const args=this._read(binding,tool.arguments_ref),effect={kind:'tool',effect_id:tool.effect_id,attempt_id:tool.attempt_id,request_sha:tool.request_sha,name:tool.name};
    const admission=await this.gate.authorize(binding,this.ownership.currentClaim(claim),effect);this.ownership.assertOwned(claim);
    this._commit(binding,claim,{type:'tool_admit',effect_id:tool.effect_id,admission_id:admission.admission_id});
    try{await this.sdk.executeTool({...tool,arguments:args},admission);}catch{/* Unknown effects stay held. */}
    return this._confirm(binding,claim,'tool',{...tool,admission_id:admission.admission_id});
   }
   const previous=this._read(binding,state.context_ref??state.input_ref),step=state.steps.at(-1),response=this._read(binding,step.result_ref);
   const messages=[...previous.messages,response.choices[0].message];
   for(const result of state.tools.filter(item=>item.provider_id===step.effect_id))
    messages.push({role:'tool',tool_call_id:result.call_id,content:canonical(this._read(binding,result.result_ref))});
   return this._commit(binding,claim,{type:'continuation',context:{...previous,messages}});
  }
  throw Error('unsupported recovery phase');
 }
 async projectTranscript(snapshot,claim=snapshot.claim){
  const assertOwned=()=>{this.ownership.assertOwned(claim);const current=this.store.load(snapshot.binding,{currentOnly:true});if(current.run_id!==snapshot.run_id)throw Error('authority_lost');};
  assertOwned();return projectTranscript(snapshot,{sdk:this.sdk,assertOwned,assertBudget:bytes=>this.store.assertProjectionBudget(snapshot.binding,bytes),readPayload:ref=>this.store.readPayload(snapshot.binding,ref)});
 }
 collectSettledPayloads(binding,evidence){
  const state=this.store.load(binding);
  if(state?.phase!=='terminal_committed'||state.terminal?.acknowledged!==true||evidence?.settled!==true||!evidence.binding||!sameBinding(evidence.binding,binding))return 0;
  const required=[...new Set([...state.steps,...state.tools].filter(item=>item.admission_id).map(item=>item.admission_id).concat(state.terminal?.cancelled_admission_ids??[]))].sort();
  if(canonical(required)!==canonical([...(evidence.admission_ids??[])].sort()))return 0;
  const payloads=this.store.payloadStore(binding),retained=this.store.retainedPayloadRefs(binding);let removed=0;
  for(const name of readdirSync(payloads.root)){
   const path=join(payloads.root,name);if(!lstatSync(path).isFile())throw Error('unsafe payload');
   if(/^[a-f0-9]{64}$/.test(name)&&!retained.has(name)){unlinkSync(path);removed++;}
  }
  if(removed)syncDirectory(payloads.root);return removed;
 }
}
