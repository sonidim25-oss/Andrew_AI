import {randomUUID} from 'node:crypto';
import {HOLD_REASONS,safeId,validateSnapshot,validRejection} from './contracts.mjs';
export function transition(previous,event){
 const state=structuredClone(previous);const phase=state.phase;
 const requirePhase=(...phases)=>{if(!phases.includes(phase))throw Error('invalid transition');};
 const admitted=(items,id)=>{const item=items.find(item=>item.effect_id===id);if(!item||item.state!=='admitted')throw Error('invalid transition');return item;};
 switch(event.type){
 case 'provider_admit':{
  requirePhase('input_committed','continuation_ready');if(!safeId(event.effect_id)||state.steps.some(step=>step.effect_id===event.effect_id)||!/^([a-f0-9]{64})$/.test(event.request_sha))throw Error('invalid transition');
  state.steps.push({effect_id:event.effect_id,request_sha:event.request_sha,request_ref:event.request_ref??null,kind:event.kind??'provider',context_ref:event.context_ref??null,state:'admitted',admission_id:event.admission_id??null});if(event.context_ref)state.context_ref=event.context_ref;state.phase='provider_admitted';break;
 }
 case 'provider_confirm':{
  requirePhase('provider_admitted');const item=admitted(state.steps,event.effect_id);if(!event.result_ref)throw Error('invalid transition');
  Object.assign(item,{state:'confirmed',result_ref:event.result_ref,usage:event.usage??null});state.phase='provider_confirmed';break;
 }
 case 'provider_reject':{
  requirePhase('provider_admitted');const item=admitted(state.steps,event.effect_id);
  if(!validRejection(event.rejection,item))throw Error('invalid rejection');
  item.state='rejected';item.rejection=event.rejection;state.phase='provider_backoff';break;
 }
 case 'provider_reconcile':{
  requirePhase('held');const item=admitted(state.steps,event.effect_id);
  if(state.hold_reason!=='provider_uncertain'||state.held_phase!=='provider_admitted'||item!==state.steps.at(-1)
    ||item.admission_id!==event.admission_id||item.request_sha!==event.request_sha)throw Error('invalid transition');
  if(event.rejection){
   if(!validRejection(event.rejection,item))throw Error('invalid rejection');
   item.state='rejected';item.rejection=event.rejection;state.phase='provider_backoff';
  }else{
   if(!event.result_ref||event.usage?.settled!==true)throw Error('invalid transition');
   Object.assign(item,{state:'confirmed',result_ref:event.result_ref,usage:event.usage});state.phase='provider_confirmed';
  }
  delete state.hold_reason;delete state.held_phase;break;
 }
 case 'provider_retry':{
  requirePhase('provider_backoff');const item=state.steps.at(-1);
  if(item.effect_id!==event.effect_id||item.state!=='rejected')throw Error('invalid transition');
  item.state='admitted';state.phase='provider_admitted';break;
 }
 case 'tool_prepare':{
  requirePhase('provider_confirmed');if(!Array.isArray(event.calls)||!event.calls.length)throw Error('invalid transition');
  for(const call of event.calls){if(!safeId(call.effect_id)||!safeId(call.name)||state.tools.some(tool=>tool.effect_id===call.effect_id))throw Error('invalid transition');state.tools.push({...call,state:'prepared',attempt_id:randomUUID()});}
  state.phase='tool_prepared';break;
 }
 case 'tool_admit':{
  requirePhase('tool_prepared','tool_confirmed');const item=state.tools.find(item=>item.effect_id===event.effect_id);
  if(!item||item.state!=='prepared')throw Error('invalid transition');item.state='admitted';item.admission_id=event.admission_id??null;state.phase='tool_admitted';break;
 }
 case 'tool_confirm':{
  requirePhase('tool_admitted');const item=admitted(state.tools,event.effect_id);if(!event.result_ref)throw Error('invalid transition');
  Object.assign(item,{state:'confirmed',result_ref:event.result_ref,...(event.runner_receipt?{runner_receipt:event.runner_receipt}:{}),...(event.terminal_stop?{terminal_stop:event.terminal_stop}:{})});state.phase='tool_confirmed';break;
 }
 case 'runner_tool_confirm':{
  requirePhase('held');const item=admitted(state.tools,event.effect_id);
  if(state.hold_reason!=='tool_uncertain'||state.held_phase!=='tool_admitted'||item.admission_id!==event.admission_id
   ||item.request_sha!==event.request_sha||!event.result_ref||!event.runner_receipt
   ||!['python.run','node.run','shell.run'].includes(item.name)||state.steps.at(-1)?.state!=='confirmed'
   ||item.provider_id!==state.steps.at(-1).effect_id||state.tools.some(tool=>tool!==item&&tool.state==='admitted'))throw Error('invalid transition');
  Object.assign(item,{state:'confirmed',result_ref:event.result_ref,runner_receipt:event.runner_receipt});
  state.phase='tool_confirmed';delete state.hold_reason;delete state.held_phase;break;
 }
 case 'local_diary_reconcile':{
  requirePhase('held');const tool=state.tools[0];
  if(state.hold_reason!=='tool_uncertain'||state.held_phase!=='tool_admitted'||state.tools.length!==1||state.steps.length!==1
    ||state.steps[0].state!=='confirmed'||tool.name!=='save_diary_entry'||tool.state!=='admitted'
    ||tool.effect_id!==event.effect_id||tool.admission_id!==event.admission_id||tool.request_sha!==event.request_sha||!event.result_ref
    ||event.terminal?.stop_reason!=='error'||event.terminal.error_category!=='diary_not_committed'
    ||event.terminal.result_admission_id!==tool.admission_id||event.terminal.consumer_id!==state.binding.consumer_id
    ||!safeId(event.terminal.id)||!Array.isArray(event.terminal.assistant_ids)||event.terminal.assistant_ids.length
    ||!/^[a-f0-9]{64}$/.test(event.terminal.maintenance_receipt_sha??''))throw Error('invalid transition');
  Object.assign(tool,{state:'confirmed',result_ref:event.result_ref});
  state.terminal={...event.terminal,acknowledged:false};state.phase='terminal_committed';delete state.hold_reason;delete state.held_phase;break;
 }
 case 'continuation':requirePhase('tool_confirmed');if(state.tools.some(item=>item.state!=='confirmed')||!event.context_ref)throw Error('invalid transition');state.context_ref=event.context_ref;state.phase='continuation_ready';break;
 case 'compaction':requirePhase('provider_confirmed');if(state.steps.at(-1)?.kind!=='compaction'||!event.context_ref)throw Error('invalid transition');state.context_ref=event.context_ref;state.phase='continuation_ready';break;
 case 'terminal':requirePhase('provider_confirmed');if(!event.terminal||!safeId(event.terminal.id)||event.terminal.consumer_id!==state.binding.consumer_id||state.tools.some(item=>item.state!=='confirmed'))throw Error('invalid transition');state.terminal={...event.terminal,acknowledged:false};state.phase='terminal_committed';break;
 case 'owner_yield':{
  const safePhases=['input_committed','provider_admitted','tool_admitted','provider_confirmed','provider_backoff','tool_prepared','tool_confirmed','continuation_ready'];
  if(!(safePhases.includes(phase)||(phase==='held'&&['owner_blocked','provider_uncertain','tool_uncertain'].includes(state.hold_reason)&&safePhases.includes(state.held_phase)))
    ||state.steps.some(item=>!['admitted','confirmed','rejected'].includes(item.state))||state.tools.some(item=>!['admitted','prepared','confirmed'].includes(item.state))
    ||!Array.isArray(event.terminal?.cancelled_admission_ids)
    ||[...state.steps,...state.tools].some(item=>item.state==='admitted'&&!event.terminal.cancelled_admission_ids.includes(item.admission_id))
    ||!safeId(event.terminal?.id)||!safeId(event.terminal?.yield_receipt_id)||event.terminal?.stop_reason!=='owner_input_pending'
    ||event.terminal.consumer_id!==state.binding.consumer_id)throw Error('invalid transition');
  for(const item of [...state.steps,...state.tools])if(item.state==='prepared'||(item.state==='admitted'&&event.terminal.cancelled_admission_ids.includes(item.admission_id)))item.state='cancelled';
  state.terminal={...event.terminal,acknowledged:false};state.phase='terminal_committed';delete state.hold_reason;delete state.held_phase;break;
 }
 case 'lifecycle_terminal':{
  requirePhase('tool_confirmed');
  const tool=state.tools.find(item=>item.admission_id===event.terminal?.result_admission_id&&item.state==='confirmed'&&item.terminal_stop===event.terminal?.lifecycle_stop);
  if(!tool||!['sleep','wake_at'].includes(tool.terminal_stop)||state.tools.some(item=>item.state==='admitted')||event.terminal.consumer_id!==state.binding.consumer_id)throw Error('invalid transition');
  for(const pending of state.tools)if(pending.state==='prepared')pending.state='cancelled';
  state.terminal={...event.terminal,acknowledged:false};state.phase='terminal_committed';break;
 }
 case 'ack':requirePhase('terminal_committed');if(event.consumer_id!==state.binding.consumer_id||event.terminal_id!==state.terminal.id)throw Error('invalid transition');state.terminal.acknowledged=true;state.terminal.settled=event.settled===true;break;
 case 'hold':if(!HOLD_REASONS.includes(event.reason))throw Error('invalid transition');state.held_phase=phase;state.hold_reason=event.reason;state.phase='held';break;
 case 'claim':if(!event.claim||event.claim.fence!==state.fence+1)throw Error('invalid transition');state.fence=event.claim.fence;state.claim=event.claim;break;
 case 'renew':if(state.claim?.claim_id!==event.claim?.claim_id||state.fence!==event.claim?.fence)throw Error('invalid transition');state.claim=event.claim;break;
 case 'release':if(state.claim?.claim_id!==event.claim_id||state.fence!==event.fence)throw Error('invalid transition');state.claim=null;break;
 case 'resume':if(!safeId(event.intent?.attempt_id)||state.attempts?.[event.intent.attempt_id])throw Error('invalid transition');state.attempts??={};state.attempts[event.intent.attempt_id]={intent:event.intent,state:'admitted'};break;
 default:throw Error('invalid transition');
 }
 state.revision++;return validateSnapshot(state);
}
