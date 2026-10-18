import {join,resolve} from 'node:path';
import {randomUUID} from 'node:crypto';
import {chmodSync,existsSync,readdirSync,lstatSync} from 'node:fs';
import {canonical,digest,LIMITS,validateBinding,validateSnapshot,sameBinding,safeId} from './contracts.mjs';
import {PayloadStore,safeDirectory,readJSON,atomicWrite} from './payload-store.mjs';
import {transition} from './state-machine.mjs';

export class CheckpointStore{
 constructor({root,acquireLock,limits={}}){this.root=resolve(root);this.acquireLock=acquireLock;this.limits={...LIMITS,...limits};}
 initialize(storeId){
  if(!safeId(storeId)||typeof this.acquireLock!=='function')throw Error('invalid store');
  safeDirectory(this.root,{create:true});chmodSync(this.root,0o700);
  const path=join(this.root,'identity.json'),release=this.acquireLock(path);
  try{if(existsSync(path)){if(readJSON(path).store_id!==storeId)throw Error('binding mismatch');}
   else atomicWrite(path,Buffer.from(canonical({version:1,store_id:storeId})));}
  finally{release();}
  safeDirectory(join(this.root,'conversations'),{create:true});safeDirectory(join(this.root,'blobs'),{create:true});
 }
 verifyIdentity(binding){
  validateBinding(binding);if(!safeDirectory(this.root))return false;
  const identity=readJSON(join(this.root,'identity.json'));if(identity.version!==1||identity.store_id!==binding.store_id)throw Error('binding mismatch');return true;
 }
 payloadStore(binding){validateBinding(binding);return new PayloadStore({root:join(this.root,'blobs',digest({agent_id:binding.agent_id,conversation_id:binding.conversation_id})),limits:this.limits});}
 checkpointPath(binding){validateBinding(binding);return join(this.root,'conversations',digest({agent_id:binding.agent_id,conversation_id:binding.conversation_id})+'.json');}
 archiveDirectory(binding){return join(this.root,'conversations',digest({agent_id:binding.agent_id,conversation_id:binding.conversation_id})+'.history');}
 archivePath(binding){return join(this.archiveDirectory(binding),digest(binding)+'.json');}
 retainedPayloadRefs(binding){
  const refs=new Set(this.load(binding).payload_refs),directory=this.archiveDirectory(binding);
  if(safeDirectory(directory))for(const name of readdirSync(directory)){
   const archived=validateSnapshot(readJSON(join(directory,name),this.limits.metadata));
   if(this.checkpointPath(archived.binding)!==this.checkpointPath(binding))throw Error('state_corrupt');
   for(const ref of archived.payload_refs)refs.add(ref);
  }
  return refs;
 }
 retainedMetadataBytes(binding,{includeProjection=true}={}){
  let bytes=0;const projection=this.checkpointPath(binding).replace(/\.json$/,'.projection.json');
  if(includeProjection&&existsSync(projection)){
   const stat=lstatSync(projection);if(!stat.isFile()||stat.isSymbolicLink())throw Error('state_corrupt');bytes+=stat.size;
  }
  const directory=this.archiveDirectory(binding);if(!safeDirectory(directory))return bytes;
  for(const name of readdirSync(directory)){
   const stat=lstatSync(join(directory,name));if(!/^[a-f0-9]{64}\.json$/.test(name)||!stat.isFile()||stat.isSymbolicLink())throw Error('state_corrupt');bytes+=stat.size;
  }return bytes;
 }
 assertProjectionBudget(binding,bytes){
  if(bytes>this.limits.payload||this.payloadStore(binding).usage()+this.retainedMetadataBytes(binding,{includeProjection:false})+lstatSync(this.checkpointPath(binding)).size+bytes>this.limits.conversation)throw Error('storage_limit');
 }
 load(binding,{currentOnly=false}={}){
  if(!this.verifyIdentity(binding))return null;
  if(!safeDirectory(join(this.root,'conversations')))return null;
  let snapshot;try{snapshot=validateSnapshot(readJSON(this.checkpointPath(binding),this.limits.metadata));}
  catch(error){if(error.code==='ENOENT')return null;throw error;}
  if(!sameBinding(snapshot.binding,binding)){
   if(currentOnly)throw Error('binding mismatch');
   try{snapshot=validateSnapshot(readJSON(this.archivePath(binding),this.limits.metadata));}catch(error){if(error.code==='ENOENT')throw Error('binding mismatch');throw error;}
   if(!sameBinding(snapshot.binding,binding))throw Error('binding mismatch');
  }
  for(const ref of snapshot.payload_refs??[])this.payloadStore(binding).readPayload(ref);
  return snapshot;
 }
 inspect(binding){
  try{const state=this.load(binding);if(!state)return {status:'missing',hold_reason:'legacy_unproven'};
   return {status:state.phase==='held'?'held':'found',binding:state.binding,revision:state.revision,fence:state.fence,phase:state.phase,
    hold_reason:state.hold_reason??null,result_digests:[...state.steps,...state.tools].filter(item=>item.result_ref).map(item=>item.result_ref)};
  }catch(error){return {status:'held',hold_reason:/binding/.test(error.message)?'binding_mismatch':'state_corrupt'};}
 }
 commit(binding,expectedRevision,event,payloads=[]){
  if(!this.verifyIdentity(binding))throw Error('missing store');
  safeDirectory(join(this.root,'conversations'));const path=this.checkpointPath(binding),release=this.acquireLock(path);
  try{
   const previous=this.load(binding,{currentOnly:true});if(existsSync(this.archivePath(binding)))throw Error('binding mismatch');if((previous?.revision??null)!==expectedRevision)throw Error('stale revision');
   if(previous?.fence>0&&!['claim','renew','release'].includes(event.type)
     &&(!previous.claim||event.fence!==previous.fence||event.claim_id!==previous.claim.claim_id))throw Error('authority lost');
   const prepared=structuredClone(event),refs=new Set(previous?.payload_refs??[]);
   const put=value=>{const ref=this.payloadStore(binding).putPayload(Buffer.from(canonical(value))).sha;refs.add(ref);return ref;};
   for(const key of ['input','result','context','request'])if(Object.hasOwn(prepared,key)){prepared[key+'_ref']=put(prepared[key]);delete prepared[key];}
   if(Array.isArray(prepared.calls))for(const call of prepared.calls)if(Object.hasOwn(call,'arguments')){call.arguments_ref=put(call.arguments);delete call.arguments;}
   for(const payload of payloads)put(payload);
   let next;
   if(previous===null){
    if(event.type!=='input'||digest(event.input)!==binding.input_sha)throw Error('invalid input binding');
    next={version:1,binding:structuredClone(binding),revision:1,fence:0,phase:'input_committed',run_id:randomUUID(),steps:[],tools:[],terminal:null,input_ref:prepared.input_ref};
   }else next=transition({...previous,payload_refs:[...refs]},prepared);
   next.payload_refs=[...refs];validateSnapshot(next);
   const bytes=Buffer.from(canonical(next));if(bytes.length>this.limits.metadata)throw Error('storage_limit');
   if(this.payloadStore(binding).usage()+bytes.length+this.retainedMetadataBytes(binding)>this.limits.conversation)throw Error('storage_limit');
   atomicWrite(path,bytes);return next;
  }finally{release();}
 }
 admitNext(binding,input,previousBinding,expectedRevision){
  validateBinding(binding);validateBinding(previousBinding);
  if(digest(input)!==binding.input_sha||sameBinding(binding,previousBinding)||binding.agent_id!==previousBinding.agent_id||binding.conversation_id!==previousBinding.conversation_id||binding.store_id!==previousBinding.store_id)throw Error('binding mismatch');
  if(!this.verifyIdentity(binding))throw Error('missing store');
  const path=this.checkpointPath(binding),release=this.acquireLock(path);
  try{
   const previous=this.load(previousBinding,{currentOnly:true});
   if(previous.revision!==expectedRevision)throw Error('stale revision');
   if(previous.phase!=='terminal_committed'||previous.terminal?.acknowledged!==true||previous.terminal?.settled!==true||previous.claim)throw Error('invalid transition');
   // A binding can never be admitted again after it has become historical.
   if(existsSync(this.archivePath(binding)))throw Error('binding mismatch');
   const directory=this.archiveDirectory(previousBinding);safeDirectory(directory,{create:true});
   const archived=Buffer.from(canonical(previous)),archive=this.archivePath(previousBinding);
   if(existsSync(archive)){if(!readJSON(archive)||canonical(readJSON(archive))!==canonical(previous))throw Error('state_corrupt');}
   else atomicWrite(archive,archived);
   const ref=this.payloadStore(binding).putPayload(Buffer.from(canonical(input))).sha;
   const next={version:1,binding:structuredClone(binding),revision:previous.revision+1,fence:previous.fence+1,claim:null,phase:'input_committed',run_id:randomUUID(),steps:[],tools:[],terminal:null,input_ref:ref,payload_refs:[ref]};
   validateSnapshot(next);const bytes=Buffer.from(canonical(next));
   if(bytes.length>this.limits.metadata||this.payloadStore(binding).usage()+this.retainedMetadataBytes(binding)+bytes.length>this.limits.conversation)throw Error('storage_limit');
   atomicWrite(path,bytes);return next;
  }finally{release();}
 }
 readPayload(binding,ref){return this.payloadStore(binding).readPayload(ref);}
}
