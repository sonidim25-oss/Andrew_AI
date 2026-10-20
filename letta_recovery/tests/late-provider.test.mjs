import test from 'node:test';
import assert from 'node:assert/strict';
import {executionFixture,textResponse} from './execution-fixture.mjs';
import {digest} from '../contracts.mjs';

async function held(t){
 const f=await executionFixture(t,{unknownProvider:true});await f.execution.advance(f.binding,f.claim);
 const item=f.store.load(f.binding).steps.at(-1);
 f.results.set(item.admission_id,{state:'confirmed',admission_id:item.admission_id,kind:item.kind,request_sha:item.request_sha,result:textResponse,result_sha:digest(textResponse),usage:{settled:true,cost:.001}});
 return {f,item};
}
test('late_provider_result_is_confirmed_without_dispatch_or_input_change',async t=>{
 const {f,item}=await held(t),before=f.store.load(f.binding);const restarted=await f.restart();
 const result=await restarted.reconcileProvider(f.binding,f.claim,item.admission_id);
 assert.equal(result.phase,'provider_confirmed');assert.equal(result.steps.at(-1).result_ref,digest(textResponse));
 assert.equal(result.run_id,before.run_id);assert.equal(result.input_ref,before.input_ref);assert.deepEqual(f.counts,{provider:1,tool:0});
 const revision=result.revision;
 assert.equal((await restarted.reconcileProvider(f.binding,f.claim,item.admission_id)).revision,revision);
});
test('late_compaction_result_keeps_its_kind_and_does_not_repeat_inference',async t=>{
 const f=await executionFixture(t,{unknownProvider:true});f.sdk.prepareProviderStep=async(binding,context)=>({kind:'compaction',request:context,context});
 await f.execution.advance(f.binding,f.claim);const item=f.store.load(f.binding).steps.at(-1);
 f.results.set(item.admission_id,{state:'confirmed',admission_id:item.admission_id,kind:'compaction',request_sha:item.request_sha,result:textResponse,result_sha:digest(textResponse),usage:{settled:true}});
 const result=await f.execution.reconcileProvider(f.binding,f.claim,item.admission_id);
 assert.equal(result.phase,'provider_confirmed');assert.equal(result.steps.at(-1).kind,'compaction');assert.equal(f.counts.provider,1);
});
test('late_429_receipt_recovers_backoff_without_retrying',async t=>{
 const {f,item}=await held(t);
 f.results.set(item.admission_id,{state:'rejected',admission_id:item.admission_id,request_sha:item.request_sha,http_status:429,attempt:1,rejected_at_ms:Date.now(),retry_at_ms:Date.now()+60000});
 const result=await f.execution.reconcileProvider(f.binding,f.claim,item.admission_id);
 assert.equal(result.phase,'provider_backoff');assert.equal(f.counts.provider,1);
});
test('unsettled_missing_or_mismatched_receipts_leave_the_hold_intact',async t=>{
 for(const change of [{state:'uncertain'},{admission_id:'other'},{request_sha:'0'.repeat(64)},{kind:'tool'},{usage:{settled:false}},{result_sha:'0'.repeat(64)}]){
  const {f,item}=await held(t);f.results.set(item.admission_id,{...f.results.get(item.admission_id),...change});
  const before=f.store.load(f.binding);await assert.rejects(f.execution.reconcileProvider(f.binding,f.claim,item.admission_id));
  assert.deepEqual(f.store.load(f.binding),before);assert.equal(f.counts.provider,1);
 }
});
test('other_hold_categories_and_foreign_admissions_cannot_use_provider_reconciliation',async t=>{
 const {f,item}=await held(t);await assert.rejects(f.execution.reconcileProvider(f.binding,f.claim,'other-admission'));
 f.store.commit(f.binding,f.store.load(f.binding).revision,{type:'hold',reason:'tool_uncertain',fence:f.claim.fence,claim_id:f.claim.claim_id});
 await assert.rejects(f.execution.reconcileProvider(f.binding,f.claim,item.admission_id));assert.equal(f.counts.provider,1);
});

test('process_loss_at_receipt_commit_reopens_as_held_or_confirmed_without_replay',async t=>{
 for(const when of ['before','after']){
  const {f,item}=await held(t),commit=f.store.commit.bind(f.store);
  f.store.commit=(binding,revision,event,...rest)=>{
   if(event.type!=='provider_reconcile')return commit(binding,revision,event,...rest);
   if(when==='after')commit(binding,revision,event,...rest);
   throw Error('fixture process lost');
  };
  await assert.rejects(f.execution.reconcileProvider(f.binding,f.claim,item.admission_id),/process lost/);
  f.store.commit=commit;
  const restarted=await f.restart();
  assert.equal(f.store.load(f.binding).phase,when==='before'?'held':'provider_confirmed');
  const result=await restarted.reconcileProvider(f.binding,f.claim,item.admission_id);
  assert.equal(result.phase,'provider_confirmed');assert.equal(f.counts.provider,1);
 }
});
