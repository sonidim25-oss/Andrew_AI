import {atomicWrite,readRegular} from './payload-store.mjs';
import {canonical,digest} from './contracts.mjs';

const baselines=new Map();
function readTranscript(path){
 try{return readRegular(path,268435456).toString('utf8');}
 catch(error){if(error.code!=='ENOENT')throw error;return '';}
}

// The pinned SDK exposes active messages separately from its append-only history.
// Bind that active snapshot to the exact disk bytes before any synchronous write.
export function withTranscriptBaseline(path,messages,write){
 if(baselines.has(path))throw Error('projection_conflict');
 baselines.set(path,{messages:structuredClone(messages),text:readTranscript(path)});
 try{return write();}finally{baselines.delete(path);}
}

export function atomicTranscriptWrite(path,text){
 const generated=text.trimEnd().split('\n').map(line=>JSON.parse(line));
 if(generated.some(row=>row.message?.metadata?.andrew_recovery)){
  const previous=readTranscript(path),baseline=baselines.get(path);
  if(baseline&&baseline.text!==previous)throw Error('projection_conflict');
  const oldRows=previous.trim()?previous.trimEnd().split('\n').map(line=>JSON.parse(line)):[];
  const oldMessages=baseline?.messages??oldRows.filter(row=>row.message).map(row=>row.message);
  const messages=generated.filter(row=>row.message);
  if(oldMessages.length>messages.length||oldMessages.some((message,index)=>canonical(message)!==canonical(messages[index].message)))throw Error('projection_conflict');
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
