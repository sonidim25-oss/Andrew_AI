import {randomUUID} from 'node:crypto';
import {HOLD_REASONS,safeId,validateSnapshot} from './contracts.mjs';
export function transition(previous,event){
 const state=structuredClone(previous);const phase=state.phase;
 const requirePhase=(...phases)=>{if(!phases.includes(phase))throw Error('invalid transition');};
 const admitted=(items,id)=>{const item=items.find(item=>item.effect_id===id);if(!item||item.state!=='admitted')throw Error('invalid transition');return item;};
 switch(event.type){
 case 'provider_admit':{
  requirePhase('input_committed','continuation_ready');if(!safeId(event.effect_id)||state.steps.some(step=>step.effect_id===event.effect_id)||!/^([a-f0-9]{64})$/.test(event.request_sha))throw Error('invalid transition');
  state.steps.push({effect_id:event.effect_id,request_sha:event.request_sha,request_ref:event.request_ref??null,state:'admitted',admission_id:event.admission_id??null});state.phase='provider_admitted';break;
 }
 case 'provider_confirm':{
  requirePhase('provider_admitted');const item=admitted(state.steps,event.effect_id);if(!event.result_ref)throw Error('invalid transition');
  Object.assign(item,{state:'confirmed',result_ref:event.result_ref,usage:event.usage??null});state.phase='provider_confirmed';break;
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
  Object.assign(item,{state:'confirmed',result_ref:event.result_ref});state.phase='tool_confirmed';break;
 }
 case 'continuation':requirePhase('tool_confirmed');if(state.tools.some(item=>item.state!=='confirmed')||!event.context_ref)throw Error('invalid transition');state.context_ref=event.context_ref;state.phase='continuation_ready';break;
 case 'terminal':requirePhase('provider_confirmed');if(!event.terminal||!safeId(event.terminal.id)||event.terminal.consumer_id!==state.binding.consumer_id||state.tools.some(item=>item.state!=='confirmed'))throw Error('invalid transition');state.terminal={...event.terminal,acknowledged:false};state.phase='terminal_committed';break;
 case 'ack':requirePhase('terminal_committed');if(event.consumer_id!==state.binding.consumer_id||event.terminal_id!==state.terminal.id)throw Error('invalid transition');state.terminal.acknowledged=true;break;
 case 'hold':if(!HOLD_REASONS.includes(event.reason))throw Error('invalid transition');state.held_phase=phase;state.hold_reason=event.reason;state.phase='held';break;
 case 'claim':if(!event.claim||event.claim.fence!==state.fence+1)throw Error('invalid transition');state.fence=event.claim.fence;state.claim=event.claim;break;
 case 'renew':if(state.claim?.claim_id!==event.claim?.claim_id||state.fence!==event.claim?.fence)throw Error('invalid transition');state.claim=event.claim;break;
 case 'release':if(state.claim?.claim_id!==event.claim_id||state.fence!==event.fence)throw Error('invalid transition');state.claim=null;break;
 case 'resume':if(!safeId(event.intent?.attempt_id)||state.attempts?.[event.intent.attempt_id])throw Error('invalid transition');state.attempts??={};state.attempts[event.intent.attempt_id]={intent:event.intent,state:'admitted'};break;
 default:throw Error('invalid transition');
 }
 state.revision++;return validateSnapshot(state);
}
