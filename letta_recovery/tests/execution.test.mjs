import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,readdirSync,writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {digest} from '../contracts.mjs';
import {executionFixture,textResponse,toolResponse} from './execution-fixture.mjs';

test('confirmed_response_is_not_requested_again',async t=>{
 const f=await executionFixture(t);await f.execution.advance(f.binding,f.claim);
 assert.equal(f.store.load(f.binding).phase,'provider_confirmed');
 const recovered=await f.restart();await recovered.advance(f.binding,f.claim);
 assert.equal(f.counts.provider,1);assert.equal(f.store.load(f.binding).phase,'terminal_committed');
});
test('confirmed_tool_result_is_not_executed_again',async t=>{
 const f=await executionFixture(t,{responses:[toolResponse,textResponse]});
 for(let i=0;i<3;i++)await f.execution.advance(f.binding,f.claim);
 assert.equal(f.store.load(f.binding).phase,'tool_confirmed');assert.equal(f.counts.tool,1);
 const recovered=await f.restart();for(let i=0;i<3;i++)await recovered.advance(f.binding,f.claim);
 assert.equal(f.counts.tool,1);assert.equal(f.counts.provider,2);
 assert.equal(f.store.load(f.binding).phase,'terminal_committed');
});
test('admitted_unknown_effect_holds',async t=>{
 const f=await executionFixture(t,{unknownProvider:true});await f.execution.advance(f.binding,f.claim);
 assert.equal(f.store.load(f.binding).phase,'held');assert.equal(f.counts.provider,1);
 const recovered=await f.restart();await recovered.advance(f.binding,f.claim);assert.equal(f.counts.provider,1);
});
test('provider_success_backend_commit_gap_reuses_exact_gate_evidence',async t=>{
 const f=await executionFixture(t);const original=f.store.commit.bind(f.store);let crashed=false;
 f.store.commit=(binding,revision,event,payloads)=>{if(event.type==='provider_confirm'&&!crashed){crashed=true;throw Error('simulated backend loss');}return original(binding,revision,event,payloads);};
 await assert.rejects(f.execution.advance(f.binding,f.claim),/backend loss/);f.store.commit=original;
 assert.equal(f.store.load(f.binding).phase,'provider_admitted');
 const recovered=await f.restart();await recovered.advance(f.binding,f.claim);
 assert.equal(f.counts.provider,1);assert.equal(f.store.load(f.binding).phase,'provider_confirmed');
});
test('compaction_keeps_unfinished_payloads',async t=>{
 const f=await executionFixture(t,{responses:[toolResponse,textResponse]});await f.execution.advance(f.binding,f.claim);
 const before=f.store.load(f.binding).payload_refs;
 assert.equal(f.execution.collectSettledPayloads(f.binding,{settled:true}),0);
 for(const ref of before)assert.ok(f.store.readPayload(f.binding,ref).length>0);
});
test('unacknowledged_or_unsettled_payloads_are_not_collected',async t=>{
 const f=await executionFixture(t);await f.execution.advance(f.binding,f.claim);await f.execution.advance(f.binding,f.claim);
 const orphan=f.store.payloadStore(f.binding).putPayload(Buffer.from('orphan')).sha;
 assert.equal(f.execution.collectSettledPayloads(f.binding,{settled:true}),0);
 let current=f.store.load(f.binding);f.store.commit(f.binding,current.revision,{type:'ack',terminal_id:current.terminal.id,consumer_id:f.binding.consumer_id,fence:f.claim.fence,claim_id:f.claim.claim_id});
 assert.equal(f.execution.collectSettledPayloads(f.binding,{settled:false}),0);
 assert.equal(f.execution.collectSettledPayloads(f.binding,{settled:true,binding:f.binding,admission_ids:f.store.load(f.binding).steps.map(x=>x.admission_id)}),1);
 assert.equal(f.store.inspect(f.binding).phase,'terminal_committed');
});

test('compaction_is_a_journaled_provider_step_and_is_not_repeated_after_restart',async t=>{
 const summary={...textResponse,id:'summary-fixture',choices:[{index:0,message:{role:'assistant',content:'Fixture summary.'},finish_reason:'stop'}]};
 const f=await executionFixture(t,{responses:[summary,textResponse]});
 const kinds=[],authorize=f.gate.authorize;f.gate.authorize=async (...args)=>{kinds.push(args[2].kind);return authorize(...args);};
 f.sdk.prepareProviderStep=(binding,context)=>context.compacted?{kind:'provider',request:context}:{kind:'compaction',request:{model:'fixture',messages:[{role:'user',content:'Summarize fixture'}]}};
 f.sdk.completeCompaction=(context,response)=>({...context,compacted:true,messages:[{role:'system',content:response.choices[0].message.content},{role:'user',content:'fixture'}]});
 await f.execution.advance(f.binding,f.claim);assert.equal(f.counts.provider,1);
 const refs=f.store.load(f.binding).payload_refs;
 const recovered=await f.restart();await recovered.advance(f.binding,f.claim);
 assert.equal(f.store.load(f.binding).phase,'continuation_ready');
 for(const ref of refs)assert.ok(f.store.readPayload(f.binding,ref).length>0);
 await recovered.advance(f.binding,f.claim);await recovered.advance(f.binding,f.claim);
 assert.deepEqual(kinds,['compaction','provider']);assert.equal(f.counts.provider,2);
 assert.equal(f.store.load(f.binding).phase,'terminal_committed');
});

test('confirmed_lifecycle_stop_finishes_without_another_provider_request',async t=>{
 const response=structuredClone(toolResponse);response.choices[0].message.tool_calls[0].function.name='sleep';
 response.choices[0].message.tool_calls.push({id:'cancelled-call',type:'function',function:{name:'recent_activity',arguments:'{}'}});
 const f=await executionFixture(t,{responses:[response],tools:['sleep','recent_activity'].map(name=>({type:'function',function:{name,parameters:{type:'object'}}}))});
 const result=f.gate.result;f.gate.result=async admission=>{const proof=await result(admission);return admission.kind==='tool'?{...proof,terminal_stop:'sleep'}:proof;};
 for(let n=0;n<4;n++)await f.execution.advance(f.binding,f.claim);
 assert.equal(f.store.load(f.binding).phase,'terminal_committed');assert.equal(f.counts.provider,1);assert.equal(f.counts.tool,1);
 assert.equal(f.store.load(f.binding).terminal.lifecycle_stop,'sleep');
 await f.execution.projectTranscript(f.store.load(f.binding));
 const rows=JSON.parse(readFileSync(f.projection));
 assert.deepEqual(rows.find(row=>row.value?.call_id==='cancelled-call').value.result,{status:'cancelled',reason:'lifecycle_stop',executed:false});
});
