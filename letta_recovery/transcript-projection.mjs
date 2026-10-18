import {canonical,digest} from './contracts.mjs';

/** Replay structured, confirmed operations, never derive results from transcript text. */
export function projectionOperations(snapshot,readPayload){
 const read=ref=>JSON.parse(readPayload(ref).toString('utf8'));
 const operations=[];
 const add=(id,kind,value)=>operations.push({operation_id:id,kind,value,binding_sha:digest(snapshot.binding),input_sha:snapshot.binding.input_sha,digest:digest({kind,value})});
 const input=read(snapshot.input_ref);add('input:'+snapshot.run_id,'input',input);
 for(const step of snapshot.steps){
  if(step.state!=='confirmed')continue;
  const response=read(step.result_ref),compaction=step.kind==='compaction';
  const value=compaction?{response,tail:read(step.context_ref)._recovery_compaction_tail}:response;
  add('provider:'+step.effect_id,compaction?'compaction':'provider',value);
  for(const tool of snapshot.tools.filter(tool=>tool.provider_id===step.effect_id&&['confirmed','cancelled'].includes(tool.state)))
   add('tool:'+tool.effect_id,'tool',tool.state==='cancelled'
    ?{call_id:tool.call_id,name:tool.name,cancelled:true,result:{status:'cancelled',reason:'lifecycle_stop',executed:false}}
    :{call_id:tool.call_id,name:tool.name,result:read(tool.result_ref)});
 }
 return operations;
}
export async function projectTranscript(snapshot,{sdk,readPayload,assertOwned,assertBudget}){
 const desired=projectionOperations(snapshot,readPayload),existing=await sdk.readProjectionOperations(snapshot.binding);
 assertOwned();const expected=new Map(desired.map(row=>[row.operation_id,row]));const seen=new Set();
 for(const row of existing){
  if(seen.has(row.operation_id)||row.digest!==digest({kind:row.kind,value:row.value}))throw Error('projection_conflict');seen.add(row.operation_id);
  const wanted=expected.get(row.operation_id);if(wanted&&canonical(wanted)!==canonical(row))throw Error('projection_conflict');
 }
 // Other input operations may be historical; the SDK adapter validates their baseline.
 const additions=desired.filter(row=>!seen.has(row.operation_id));
 if(additions.length)await sdk.replaceProjection(snapshot.binding,[...existing,...additions],assertOwned,assertBudget);
}
