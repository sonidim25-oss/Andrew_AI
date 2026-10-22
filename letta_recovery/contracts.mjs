import {createHash} from 'node:crypto';
export const CONTRACT='local_durable_recovery_v1';
export const LIMITS=Object.freeze({metadata:1048576,payload:16777216,conversation:268435456});
export const PHASES=Object.freeze(['input_committed','provider_admitted','provider_backoff','provider_confirmed','tool_prepared','tool_admitted','tool_confirmed','continuation_ready','terminal_committed','held']);
export const HOLD_REASONS=Object.freeze(['legacy_unproven','binding_mismatch','unsupported_contract','provider_uncertain','tool_uncertain','authority_lost','state_corrupt','storage_limit','owner_blocked','configuration_changed','restore_required','projection_conflict']);
export const BINDING_FIELDS=Object.freeze(['store_id','agent_id','conversation_id','session_id','cycle_id','client_message_id','consumer_id','input_sha','owner_revision','config_sha']);
export const safeId=value=>typeof value==='string'&&/^[A-Za-z0-9_-][A-Za-z0-9._:-]{0,255}$/.test(value)&&!value.includes('..');
export const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
export const digest=value=>sha(Buffer.from(canonical(value)));
export function canonical(value){
 if(value===null||typeof value==='string'||typeof value==='boolean')return JSON.stringify(value);
 if(typeof value==='number'&&Number.isFinite(value))return JSON.stringify(value);
 if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
 if(value&&Object.getPrototypeOf(value)===Object.prototype)
  return '{'+Object.keys(value).sort().map(key=>JSON.stringify(key)+':'+canonical(value[key])).join(',')+'}';
 throw Error('invalid structured value');
}
export function validateBinding(value){
 if(!value||typeof value!=='object'||Array.isArray(value)||Object.keys(value).length!==BINDING_FIELDS.length)
  throw Error('invalid binding');
 for(const key of BINDING_FIELDS){
  if(key.endsWith('_sha')){if(typeof value[key]!=='string'||!/^([a-f0-9]{64})$/.test(value[key]))throw Error('invalid binding');}
  else if(!safeId(value[key]))throw Error('invalid binding');
 }
 return value;
}
export function validateSnapshot(value){
 try{
  if(!value||value.version!==1||!Number.isSafeInteger(value.revision)||value.revision<0||!Number.isSafeInteger(value.fence)||value.fence<0||!PHASES.includes(value.phase)||!safeId(value.run_id)||!Array.isArray(value.steps)||!Array.isArray(value.tools)||!(value.terminal===null||typeof value.terminal==='object'))throw Error();
  validateBinding(value.binding);
  const validHash=hash=>typeof hash==='string'&&/^[a-f0-9]{64}$/.test(hash);
  const items=[...value.steps,...value.tools];
  if(value.steps.length>256||value.tools.length>256)throw Error();
  for(const item of items){
   if(!item||!safeId(item.effect_id)||!['prepared','admitted','rejected','confirmed','cancelled'].includes(item.state))throw Error();
   if(item.state==='confirmed'&&!validHash(item.result_ref))throw Error();
   if(item.state==='rejected'&&(!value.steps.includes(item)||!validRejection(item.rejection,item)))throw Error();
  }
  if(new Set(value.steps.map(item=>item.effect_id)).size!==value.steps.length||new Set(value.tools.map(item=>item.effect_id)).size!==value.tools.length)throw Error();
  if(value.phase==='provider_confirmed'&&value.steps.at(-1)?.state!=='confirmed')throw Error();
  if(value.phase==='provider_admitted'&&value.steps.at(-1)?.state!=='admitted')throw Error();
  if(value.phase==='provider_backoff'&&value.steps.at(-1)?.state!=='rejected')throw Error();
  if(value.phase==='terminal_committed'){
   if(!value.terminal||!safeId(value.terminal.id)||value.terminal.consumer_id!==value.binding.consumer_id)throw Error();
   if(value.terminal.stop_reason==='owner_input_pending'){
    if(!safeId(value.terminal.yield_receipt_id)||!Array.isArray(value.terminal.cancelled_admission_ids)||!value.terminal.cancelled_admission_ids.every(safeId)||value.steps.some(item=>!['confirmed','rejected','cancelled'].includes(item.state))||value.tools.some(item=>!['confirmed','cancelled'].includes(item.state)))throw Error();
   }else if(value.steps.at(-1)?.state!=='confirmed')throw Error();
  }
  if(value.payload_refs!==undefined){
   if(!Array.isArray(value.payload_refs)||!value.payload_refs.every(validHash)||!validHash(value.input_ref)||!value.payload_refs.includes(value.input_ref))throw Error();
   for(const item of items)for(const key of ['result_ref','request_ref','arguments_ref'])if(item[key]&&!value.payload_refs.includes(item[key]))throw Error();
  }
  canonical(value);return value;
 }catch{throw Error('invalid snapshot');}
}
export const sameBinding=(left,right)=>canonical(validateBinding(left))===canonical(validateBinding(right));

// An explicit HTTP 429, or a transport failure the controller proved happened
// before any request byte left. Receipts without a reason are HTTP 429 only.
export function validRejection(proof,item){
 const typed=proof?.reason===undefined?proof?.http_status===429:proof?.reason==='provider_not_reached'&&proof.http_status===null;
 return proof?.state==='rejected'&&typed&&proof.admission_id===item.admission_id&&proof.request_sha===item.request_sha
  &&Number.isSafeInteger(proof.attempt)&&proof.attempt>0&&Number.isSafeInteger(proof.rejected_at_ms)&&proof.rejected_at_ms>=0
  &&Number.isSafeInteger(proof.retry_at_ms)&&proof.retry_at_ms>=proof.rejected_at_ms;
}

// The controller supplies this proof through its scoped result endpoint. No
// reconcile command accepts result bytes or caller-supplied receipt metadata.
export function validateRunnerReceipt(proof,binding,tool,args){
 const fail=()=>{throw Error('tool_uncertain');},hash=v=>typeof v==='string'&&/^[a-f0-9]{64}$/.test(v);
 const uuid=v=>typeof v==='string'&&/^(?:[a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})$/.test(v);
 const receipt=proof?.runner_receipt,id=receipt?.identity,key=tool.name==='shell.run'?'command':'code';
 if(!['python.run','node.run','shell.run'].includes(tool.name)||!args||Object.keys(args).length!==1||typeof args[key]!=='string'||!args[key].trim()
  ||tool.request_sha!==digest({name:tool.name,arguments:args})||proof?.state!=='confirmed'||proof.kind!=='tool'
  ||proof.admission_id!==tool.admission_id||proof.request_sha!==tool.request_sha||typeof proof.result!=='string'||proof.result_sha!==digest(proof.result)
  ||!receipt||receipt.contract!=='runner_result_recovery_v1'||receipt.version!==1||receipt.formatter!=='runner_text_v1'||receipt.cleanup_verified!==true
  ||!hash(receipt.operation_id)||!hash(receipt.identity_sha)||!hash(receipt.response_sha)||!uuid(receipt.controller_store_id)||!uuid(receipt.runner_store_id)
  ||!id||digest(id)!==receipt.identity_sha||canonical(proof.usage)!==canonical({runner_receipt:receipt}))fail();
 if(id.contract!==receipt.contract||id.version!==1||id.formatter!==receipt.formatter||id.operation_id!==receipt.operation_id
  ||id.controller_store_id!==receipt.controller_store_id||id.runner_store_id!==receipt.runner_store_id||!sameBinding(id.binding,binding)
  ||id.binding_sha!==digest(binding)||id.admission_id!==tool.admission_id||id.effect_id!==tool.effect_id||id.tool_name!==tool.name
  ||id.request_sha!==tool.request_sha||id.worker_request_sha!==digest({type:tool.name.split('.')[0],...args})||!safeId(id.legacy_attempt_id)
  ||id.research_context?.session_id!==binding.session_id||id.research_context?.cycle_id!==binding.cycle_id
  ||!(id.research_context?.research_id===''||safeId(id.research_context?.research_id))
  ||!Number.isSafeInteger(id.research_context?.research_revision)||id.research_context.research_revision<0)fail();
 return structuredClone(receipt);
}
