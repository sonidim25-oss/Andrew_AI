import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {executionFixture,toolResponse,textResponse} from './execution-fixture.mjs';

function gate(f,change={}){
 f.gate.ownerYield=async binding=>({state:'owner_yield',binding,receipt_id:'owner-yield-receipt',
  cancelled_admission_ids:[],admission_ids:[...f.store.load(binding).steps,...f.store.load(binding).tools].filter(item=>item.admission_id).map(item=>item.admission_id).sort(),...change});
}

test('owner_handoff_cancels_prepared_tools_and_projects_truthful_cancellation',async t=>{
 const f=await executionFixture(t,{responses:[toolResponse,textResponse]});gate(f);
 await f.execution.advance(f.binding,f.claim);await f.execution.advance(f.binding,f.claim);
 const yielded=await f.execution.yieldOwner(f.binding,f.claim);
 assert.equal(yielded.phase,'terminal_committed');assert.equal(yielded.terminal.stop_reason,'owner_input_pending');
 assert.equal(yielded.tools[0].state,'cancelled');assert.equal(yielded.steps[0].state,'confirmed');
 assert.deepEqual(f.counts,{provider:1,tool:0});
 await f.execution.projectTranscript(yielded,f.claim);
 const rows=JSON.parse(readFileSync(f.projection));const cancellation=rows.find(row=>row.kind==='tool');
 assert.deepEqual(cancellation.value.result,{status:'cancelled',reason:'owner_input_pending',executed:false});
 assert.equal((await f.execution.yieldOwner(f.binding,f.claim)).terminal.id,yielded.terminal.id);
});

test('held_owner_boundary_can_be_cancelled_under_a_fresh_fence',async t=>{
 const f=await executionFixture(t,{responses:[toolResponse]});gate(f);
 await f.execution.advance(f.binding,f.claim);await f.execution.advance(f.binding,f.claim);
 f.execution._hold(f.binding,f.claim,'owner_blocked');const restarted=await f.restart();
 const yielded=await restarted.yieldOwner(f.binding,f.claim);
 assert.equal(yielded.terminal.stop_reason,'owner_input_pending');assert.equal(f.counts.tool,0);
});

test('unknown_admitted_tool_cannot_be_cancelled_as_prepared',async t=>{
 const f=await executionFixture(t,{responses:[toolResponse],unknownTool:true});gate(f);
 for(let n=0;n<3;n++)await f.execution.advance(f.binding,f.claim);
 assert.equal(f.store.load(f.binding).hold_reason,'tool_uncertain');
 await assert.rejects(f.execution.yieldOwner(f.binding,f.claim),/owner_blocked/);
 assert.equal(f.counts.tool,1);assert.equal(f.store.load(f.binding).tools[0].state,'admitted');
});

test('mismatched_or_missing_yield_evidence_never_commits_a_terminal',async t=>{
 const f=await executionFixture(t);gate(f,{admission_ids:['unrelated-effect']});
 await assert.rejects(f.execution.yieldOwner(f.binding,f.claim),/owner_blocked/);
 assert.equal(f.store.load(f.binding).phase,'input_committed');assert.deepEqual(f.counts,{provider:0,tool:0});
});

for(const kind of ['provider','tool'])test(`owner_after_${kind}_authorization_cancels_only_durably_undispatched_effect`,async t=>{
 const f=await executionFixture(t,{responses:[toolResponse]});gate(f);
 if(kind==='tool'){await f.execution.advance(f.binding,f.claim);await f.execution.advance(f.binding,f.claim);}
 f.sdk[kind==='tool'?'executeTool':'executeProviderStep']=async()=>{throw Error('owner_blocked');};
 f.gate.ownerYield=async binding=>{
  const items=[...f.store.load(binding).steps,...f.store.load(binding).tools].filter(item=>item.admission_id);
  return {state:'owner_yield',binding,receipt_id:'cancelled-before-dispatch',admission_ids:items.map(item=>item.admission_id).sort(),cancelled_admission_ids:items.filter(item=>item.state==='admitted').map(item=>item.admission_id).sort()};
 };
 const yielded=await f.execution.advance(f.binding,f.claim);
 assert.equal(yielded.terminal?.stop_reason,'owner_input_pending');
 assert.equal((kind==='tool'?yielded.tools:yielded.steps).at(-1).state,'cancelled');
 assert.deepEqual(f.counts,{provider:kind==='tool'?1:0,tool:0});
});
