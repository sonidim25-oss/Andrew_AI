import {join} from 'node:path';
import {readdirSync} from 'node:fs';
import {canonical,digest,safeId,validateSnapshot} from './contracts.mjs';
import {safeDirectory,readJSON,readRegular,atomicWrite} from './payload-store.mjs';

const pathFor=(root,kind,id)=>join(root,'adopted',kind+'-'+digest(id)+'.json');
export function adoption(root,kind,id){
 if(!safeId(id))throw Error('binding_mismatch');
 try{return readJSON(pathFor(root,kind,id));}catch(error){if(error.code==='ENOENT')return null;throw error;}
}
export function adopt(root,binding){
 safeDirectory(join(root,'adopted'),{create:true});
 for(const [kind,id] of [['agent',binding.agent_id],['conversation',binding.conversation_id]]){
  const prior=adoption(root,kind,id);
  if(prior&&prior.store_id!==binding.store_id)throw Error('binding_mismatch');
  if(!prior)atomicWrite(pathFor(root,kind,id),Buffer.from(canonical({version:1,store_id:binding.store_id,agent_id:binding.agent_id,conversation_id:binding.conversation_id})));
 }
}
const READS=new Set(['app_server_info','agent_list','agent_retrieve','conversation_list','conversation_retrieve',
 'conversation_messages_list','messages_list','message_retrieve','run_retrieve','run_steps_list','models_list',
 'list_providers','provider_list','tools_list','agent_memory_retrieve','agent_blocks_list']);
const SETUP=new Set(['connect_provider','agent_create','conversation_create']);
function scopes(value,depth=0,result=[]){
 if(depth>12)throw Error('unsupported_contract');
 if(!value||typeof value!=='object')return result;
 if(typeof value.agent_id==='string')result.push(['agent',value.agent_id]);
 if(typeof value.conversation_id==='string')result.push(['conversation',value.conversation_id]);
 for(const child of Object.values(value))if(child&&typeof child==='object')scopes(child,depth+1,result);
 return result;
}
function sameFields(current,patch){
 if(patch&&Object.getPrototypeOf(patch)===Object.prototype)return Object.entries(patch).every(([key,value])=>sameFields(current?.[key],value));
 return current!==undefined&&canonical(current)===canonical(patch);
}
// A model change keeps every other agent setting; tools, prompt and memory stay frozen.
function modelOnlyChange(current,body){
 if(!body||Object.getPrototypeOf(body)!==Object.prototype||!Object.hasOwn(body,'model')
   ||Object.keys(body).some(key=>!['model','model_settings'].includes(key)))return false;
 if(typeof body.model!=='string'||!/^openai-compatible\/[A-Za-z0-9][A-Za-z0-9._:\/-]{0,255}$/.test(body.model)||body.model.includes('..'))return false;
 if(typeof current?.model!=='string'||current.model===body.model)return false;
 return !Object.hasOwn(body,'model_settings')||sameFields(current,{model_settings:body.model_settings});
}
// Every journaled turn of the agent is finished, acknowledged and settled, with
// no claim and no legacy run, so no held work can continue under another model.
function agentQuiescent(root,agentId,backend){
 if(!(backend?.activeRunByConversation instanceof Map)||backend.activeRunByConversation.size)return false;
 const directory=join(root,'conversations');
 if(!safeDirectory(directory))return true;
 for(const name of readdirSync(directory)){
  if(!/^[a-f0-9]{64}\.json$/.test(name))continue;
  const snapshot=validateSnapshot(readJSON(join(directory,name)));
  if(snapshot.binding.agent_id!==agentId)continue;
  if(snapshot.phase!=='terminal_committed'||snapshot.terminal?.acknowledged!==true||snapshot.terminal?.settled!==true||snapshot.claim)return false;
 }
 return true;
}
export function assertNotRestored(root){
 try{readRegular(join(root,'restore-required.json'),1024);}catch(error){if(error.code==='ENOENT')return;throw error;}
 throw Error('restore_required');
}
export async function guardLegacy(command,{root,sdk}){
 if(READS.has(command.type))return false;
 assertNotRestored(root);
 if(SETUP.has(command.type))return false;
 const targets=scopes(command);let adopted=false;
 for(const [kind,id] of targets)if(adoption(root,kind,id))adopted=true;
 if(!targets.length&&safeDirectory(join(root,'adopted')))adopted=readdirSync(join(root,'adopted')).length>0;
 if(!adopted)return false;
 // Startup repeats the exact stored agent settings. Return them without mutation.
 if(command.type==='agent_update'){
  const current=sdk.backend.store.retrieveAgentRecord(command.agent_id);
  if(command.body&&Object.keys(command.body).length&&sameFields(current,command.body))return {agent:await sdk.backend.retrieveAgent(command.agent_id)};
  // The ordinary handler applies an owner-configured model change only between turns.
  if(modelOnlyChange(current,command.body)&&agentQuiescent(root,command.agent_id,sdk.backend))return false;
 }
 throw Error('unsupported_contract');
}

export function assertAdoptable(binding,{runtimeKey,dispositionPath,scopedRuntime,backend}){
 if(!(backend.activeRunByConversation instanceof Map))throw Error('unsupported_contract');
 if(backend.activeRunByConversation.has(binding.conversation_id))throw Error('legacy_unproven');
 if(scopedRuntime&&(scopedRuntime.pendingTurns||scopedRuntime.pendingInboundDispatches||scopedRuntime.queueRuntime?.length
   ||scopedRuntime.queuePumpActive||scopedRuntime.queuePumpScheduled||scopedRuntime.pendingApprovalBatchByToolCallId?.size
   ||scopedRuntime.pendingInterruptedContext||scopedRuntime.loopStatus!=='idle'))throw Error('legacy_unproven');
 if(!dispositionPath)throw Error('unsupported_contract');
 let ledger;try{ledger=JSON.parse(readRegular(dispositionPath,1048576));}catch(error){if(error.code==='ENOENT')return;throw error;}
 if(![2,3,4].includes(ledger.version)||!ledger.entries||!ledger.reservations)throw Error('state_corrupt');
 for(const [key,entry] of Object.entries(ledger.entries)){
  let identity;try{identity=JSON.parse(key);}catch{throw Error('state_corrupt');}
  if(!Array.isArray(identity)||identity.length!==3||!identity.every(value=>typeof value==='string')||!entry||typeof entry!=='object')throw Error('state_corrupt');
  if(identity[0]!==runtimeKey)continue;
  // Adoption freezes legacy execution entry points. Retained tombstones do not
  // prove the older input completed and cannot authorize resuming that input.
  if([binding.client_message_id,binding.consumer_id].includes(identity[2])||entry.disposition!=='started'||entry.replayCompleted!==true||entry.queuedInput!==undefined)throw Error('legacy_unproven');
 }
 for(const [key,entry] of Object.entries(ledger.reservations)){
  let identity;try{identity=JSON.parse(key);}catch{throw Error('state_corrupt');}
  if(!Array.isArray(identity)||identity.length!==3||!identity.every(value=>typeof value==='string')||!entry||typeof entry!=='object')throw Error('state_corrupt');
  if(identity[0]===runtimeKey)throw Error('legacy_unproven');
 }
}
