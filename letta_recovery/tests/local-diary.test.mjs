import test from 'node:test';
import assert from 'node:assert/strict';
import {executionFixture,toolResponse} from './execution-fixture.mjs';
import {digest} from '../contracts.mjs';

async function held(t){
 const response=structuredClone(toolResponse);response.choices[0].message.tool_calls[0].function.name='save_diary_entry';
 const f=await executionFixture(t,{responses:[response],unknownTool:true,tools:[{type:'function',function:{name:'save_diary_entry',parameters:{type:'object'}}}]});
 for(let n=0;n<3;n++)await f.execution.advance(f.binding,f.claim);
 const tool=f.store.load(f.binding).tools[0],result=JSON.stringify({status:'error',error:'diary_not_committed',committed:false,evidence:'quiescent_local_diary_ledger'});
 const proof={state:'confirmed',kind:'tool',admission_id:tool.admission_id,request_sha:tool.request_sha,result,result_sha:digest(result),
  usage:{local_diary_reconciliation:{contract:'local_diary_destination_reconciliation_v1',evidence_sha:'a'.repeat(64),committed:false}}};
 f.results.set(tool.admission_id,proof);return {f,tool,proof};
}
test('exact local diary evidence retires failed input without dispatch and projects its error',async t=>{
 const {f,tool}=await held(t),before=f.store.load(f.binding);
 const state=await f.execution.reconcileLocalDiary(f.binding,f.claim,tool.admission_id);
 assert.equal(state.phase,'terminal_committed');assert.equal(state.terminal.stop_reason,'error');assert.equal(state.terminal.error_category,'diary_not_committed');
 assert.equal(state.tools[0].state,'confirmed');assert.equal(state.run_id,before.run_id);assert.deepEqual(f.counts,{provider:1,tool:1});
 await f.execution.projectTranscript(state,f.claim);
 assert.equal(f.sdk.readProjectionOperations().filter(row=>row.kind==='tool').length,1);
 assert.equal((await f.execution.reconcileLocalDiary(f.binding,f.claim,tool.admission_id)).revision,state.revision);
});
test('missing, foreign or unproven local diary evidence cannot clear the hold',async t=>{
 for(const change of [{state:'uncertain'},{kind:'provider'},{admission_id:'foreign'},{request_sha:'0'.repeat(64)},
  {result_sha:'0'.repeat(64)},{result:'not a result'},{usage:null},{usage:{local_diary_reconciliation:{contract:'other',evidence_sha:'a'.repeat(64),committed:false}}}]){
  const {f,tool,proof}=await held(t);f.results.set(tool.admission_id,{...proof,...change});const before=f.store.load(f.binding);
  await assert.rejects(f.execution.reconcileLocalDiary(f.binding,f.claim,tool.admission_id));assert.deepEqual(f.store.load(f.binding),before);
 }
});
test('other tools and other hold categories are never reclassified',async t=>{
 const {f,tool}=await held(t);
 await assert.rejects(f.execution.reconcileLocalDiary(f.binding,f.claim,'foreign'));
 const before=f.store.load(f.binding);f.store.commit(f.binding,before.revision,{type:'hold',reason:'restore_required',fence:f.claim.fence,claim_id:f.claim.claim_id});
 await assert.rejects(f.execution.reconcileLocalDiary(f.binding,f.claim,tool.admission_id));
 assert.equal(f.store.load(f.binding).hold_reason,'restore_required');assert.deepEqual(f.counts,{provider:1,tool:1});
});
test('interrupted local diary checkpoint commit reopens without executing anything again',async t=>{
 for(const when of ['before','after']){
  const {f,tool}=await held(t),commit=f.store.commit.bind(f.store);
  f.store.commit=(binding,revision,event,...rest)=>{
   if(event.type!=='local_diary_reconcile')return commit(binding,revision,event,...rest);
   if(when==='after')commit(binding,revision,event,...rest);
   throw Error('fixture process lost');
  };
  await assert.rejects(f.execution.reconcileLocalDiary(f.binding,f.claim,tool.admission_id));f.store.commit=commit;
  const restarted=await f.restart();assert.equal(f.store.load(f.binding).phase,when==='before'?'held':'terminal_committed');
  const state=await restarted.reconcileLocalDiary(f.binding,f.claim,tool.admission_id);assert.equal(state.phase,'terminal_committed');assert.deepEqual(f.counts,{provider:1,tool:1});
 }
});
