import {createHash} from 'node:crypto';
export const CONTRACT='local_durable_recovery_v1';
export const LIMITS=Object.freeze({metadata:1048576,payload:16777216,conversation:268435456});
export const PHASES=Object.freeze(['input_committed','provider_admitted','provider_confirmed','tool_prepared','tool_admitted','tool_confirmed','continuation_ready','terminal_committed','held']);
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
  validateBinding(value.binding);canonical(value);return value;
 }catch{throw Error('invalid snapshot');}
}
export const sameBinding=(left,right)=>canonical(validateBinding(left))===canonical(validateBinding(right));
