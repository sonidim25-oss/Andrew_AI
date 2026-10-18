import {join} from 'node:path';
import {homedir} from 'node:os';
import {atomicWrite,readRegular} from './payload-store.mjs';
import {canonical,digest} from './contracts.mjs';
import {RecoveryProtocol} from './protocol.mjs';
import {LocalSDKAdapter} from './sdk-adapter.mjs';
import {readdirSync} from 'node:fs';
import {safeDirectory} from './payload-store.mjs';
import {guardLegacy,assertNotRestored} from './adoption.mjs';
export {assertAdoptable} from './adoption.mjs';

export const artifactEnabled=()=>process.env.ANDREW_LOCAL_RECOVERY_ARTIFACT==='1';
export function atomicTranscriptWrite(path,text){
 const generated=text.trimEnd().split('\n').map(line=>JSON.parse(line));
 if(generated.some(row=>row.message?.metadata?.andrew_recovery)){
  let previous='';try{previous=readRegular(path,268435456).toString('utf8');}catch(error){if(error.code!=='ENOENT')throw error;}
  const oldRows=previous.trim()?previous.trimEnd().split('\n').map(line=>JSON.parse(line)):[];
  const oldMessages=oldRows.filter(row=>row.message),messages=generated.filter(row=>row.message);
  if(oldMessages.length>messages.length||oldMessages.some((row,index)=>canonical(row.message)!==canonical(messages[index].message)))throw Error('projection_conflict');
  let parentId=oldRows.at(-1)?.id??null;
  const additions=messages.slice(oldMessages.length).map(row=>{
   const operationId=row.message.metadata?.andrew_recovery?.operation_id;if(!operationId)throw Error('projection_conflict');
   const next={...row,id:'recovery-'+digest(operationId),parentId};parentId=next.id;return JSON.stringify(next)+'\n';
  }).join('');
  const prefix=oldRows.length?previous+(previous.endsWith('\n')?'':'\n'):JSON.stringify(generated[0])+'\n';
  atomicWrite(path,Buffer.from(prefix+additions));return;
 }
 atomicWrite(path,Buffer.from(text));
}

const services=new WeakMap();
export async function handleRawFrame(data,{runtime,socket,connectionId,sdk}){
 if(!artifactEnabled())return false;
 let command;try{command=JSON.parse(typeof data==='string'?data:data.toString());}catch{return false;}
 if(typeof command?.type!=='string')return false;
 try{
  const root=join(process.env.LETTA_LOCAL_BACKEND_DIR??join(homedir(),'.letta'),'local-recovery-v1');
  if(!command.type.startsWith('recovery_')){
   const result=await guardLegacy(command,{root,sdk});
   if(result){socket.send(JSON.stringify({type:command.type+'_response',request_id:command.request_id,success:true,...result}));return true;}
   return false;
  }
  let state=services.get(runtime);
  if(!state){state={storeId:null,connections:new WeakMap()};services.set(runtime,state);}
  let service=state.connections.get(socket);
  if(!service){
   const adapter=new LocalSDKAdapter({backend:sdk.backend,resolveModel:sdk.resolveModel,root,assertAdoptable:sdk.assertAdoptable});
   service=new RecoveryProtocol({root,sdk:adapter,connectionId,acquireLock:sdk.acquireLock,storeId:state.storeId});
   state.storeId=service.storeId;state.connections.set(socket,service);
   socket.once('close',()=>{service.close().catch(()=>{});});
  }
  const result=await service.handleRecoveryCommand(command);
  socket.send(JSON.stringify({type:command.type+'_response',request_id:command.request_id,success:true,...result}));
 }catch(error){
  const category=error.message==='stale revision'?'revision_conflict':['binding_mismatch','authority_lost','legacy_unproven','owner_blocked','configuration_changed','projection_conflict','storage_limit','unsupported_contract','restore_required','state_corrupt','provider_uncertain','tool_uncertain'].includes(error.message)?error.message:'recovery_held';
  socket.send(JSON.stringify({type:command.type+'_response',request_id:command.request_id,success:false,error_category:category}));
 }
 return true;
}

/** The builder's bootstrap remains inactive unless explicitly selected. */
export function createLocalRecovery({sdk,root,enabled=false,gate}){
 return Object.freeze({capability:null});
}

// A disabled extension cannot reopen an adopted store through legacy startup.
if(!artifactEnabled()){
 const root=join(process.env.LETTA_LOCAL_BACKEND_DIR??join(homedir(),'.letta'),'local-recovery-v1');
 assertNotRestored(root);
 if(safeDirectory(join(root,'adopted'))&&readdirSync(join(root,'adopted')).length)throw Error('unsupported_contract');
}
