import {CONTRACT,canonical,LIMITS,safeId,validateBinding} from './contracts.mjs';

const fields={
 recovery_capabilities:[],
 recovery_inspect:['binding'],recovery_status:['binding','attempt_id'],recovery_subscribe:['binding','attempt_id'],
 recovery_admit:['binding','admission_id','scope_token','input','previous_binding','expected_revision'],
 recovery_claim:['binding','admission_id','scope_token','expected_revision'],
 recovery_resume:['binding','admission_id','scope_token','expected_revision','attempt_id','claim_id','fence'],
 recovery_renew:['binding','admission_id','scope_token','claim_id','fence'],
 recovery_release:['binding','admission_id','scope_token','claim_id','fence','attempt_id'],
 recovery_revoke:['binding','admission_id','scope_token','claim_id','fence','attempt_id'],
 recovery_ack:['binding','admission_id','scope_token','claim_id','fence','expected_revision','terminal_id','consumer_id'],
 recovery_result_read:['binding','admission_id','scope_token','claim_id','fence','effect_admission_id']
};
const optional=new Set(['request_id','attempt_id','previous_binding']);
export function validateCommand(command){
 if(!command||Object.getPrototypeOf(command)!==Object.prototype||!Object.hasOwn(fields,command.type))throw Error('unsupported_contract');
 if(command.contract!==CONTRACT||command.version!==1)throw Error('unsupported_contract');
 const allowed=new Set(['type','contract','version','request_id',...fields[command.type]]);
 // Evidence reads may carry scope credentials but never use them to authorize effects.
 if(['recovery_inspect','recovery_status','recovery_subscribe'].includes(command.type)){allowed.add('admission_id');allowed.add('scope_token');}
 if(Object.keys(command).some(key=>!allowed.has(key)))throw Error('unsupported_contract');
 for(const name of fields[command.type]){
  if(optional.has(name)||name==='expected_revision'&&command.type==='recovery_admit')continue;
  if(!Object.hasOwn(command,name))throw Error('binding_mismatch');
 }
 for(const [name,value] of Object.entries(command)){
  if(name.endsWith('_id')&&!safeId(value))throw Error('binding_mismatch');
  if(['binding','previous_binding'].includes(name))validateBinding(value);
  if(['expected_revision','fence'].includes(name)&&(!Number.isSafeInteger(value)||value<(name==='fence'?1:0)))throw Error('binding_mismatch');
  if(name==='scope_token'&&(typeof value!=='string'||!/^[a-f0-9]{64}$/.test(value)))throw Error('authority_lost');
 }
 if(command.type==='recovery_admit'){
  const input=command.input;
  if(!input||Object.getPrototypeOf(input)!==Object.prototype||Object.keys(input).some(key=>!['model','messages','tools','parallel_tool_calls'].includes(key))
    ||!Array.isArray(input.messages)||input.messages.length!==1||input.messages[0]?.role!=='user'||typeof input.messages[0]?.content!=='string'
    ||Object.keys(input.messages[0]).some(key=>!['role','content'].includes(key))||!input.messages[0].content.trim()
    ||!Array.isArray(input.tools)||input.tools.length>256||input.model!==undefined&&(typeof input.model!=='string'||input.model.length>256)
    ||input.parallel_tool_calls!==undefined&&input.parallel_tool_calls!==false)throw Error('unsupported_contract');
  const names=new Set();for(const tool of input.tools){
   if(tool?.type!=='function'||!safeId(tool.function?.name)||names.has(tool.function.name)||!tool.function.parameters||typeof tool.function.parameters!=='object'||Array.isArray(tool.function.parameters))throw Error('unsupported_contract');
   names.add(tool.function.name);
  }
 }
 if(command.type==='recovery_admit'&&Object.hasOwn(command,'previous_binding')!==Object.hasOwn(command,'expected_revision'))throw Error('binding_mismatch');
 if(Buffer.byteLength(canonical(command))>LIMITS.payload)throw Error('storage_limit');
 return command;
}
