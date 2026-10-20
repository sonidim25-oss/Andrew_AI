import test from 'node:test';
import assert from 'node:assert/strict';
import {executionFixture,textResponse} from './execution-fixture.mjs';

function rejectOnce(f,{delay=0}={}){
 const execute=f.sdk.executeProviderStep;let rejected=false;
 f.sdk.executeProviderStep=async(request,admission)=>{
  if(rejected)return execute(request,admission);
  rejected=true;f.counts.provider++;
  const item=f.store.load(f.binding).steps.at(-1);
  f.results.set(admission.admission_id,{state:'rejected',admission_id:admission.admission_id,
   request_sha:item.request_sha,http_status:429,attempt:1,rejected_at_ms:Date.now(),retry_at_ms:Date.now()+delay});
  throw Error('HTTP 429');
 };
}

test('rate_limit_restarts_with_same_step_and_input_without_repeating_a_confirmed_response',async t=>{
 const f=await executionFixture(t);rejectOnce(f);
 await f.execution.advance(f.binding,f.claim);
 const before=f.store.load(f.binding);assert.equal(before.phase,'provider_backoff');assert.equal(f.counts.provider,1);
 const restarted=await f.restart();await restarted.advance(f.binding,f.claim);
 const after=f.store.load(f.binding);assert.equal(after.phase,'provider_confirmed');assert.equal(f.counts.provider,2);
 assert.equal(after.steps.length,1);assert.equal(after.steps[0].effect_id,before.steps[0].effect_id);
 assert.equal(after.input_ref,before.input_ref);assert.equal(after.run_id,before.run_id);
 await restarted.advance(f.binding,f.claim);assert.equal(f.store.load(f.binding).phase,'terminal_committed');assert.equal(f.counts.provider,2);
});

test('rejection_receipt_closes_backend_crash_gap_before_backoff_checkpoint',async t=>{
 const f=await executionFixture(t);rejectOnce(f);
 const commit=f.store.commit.bind(f.store);f.store.commit=(binding,revision,event,...rest)=>{
  if(event.type==='provider_reject')throw Error('crash before rejection checkpoint');return commit(binding,revision,event,...rest);
 };
 await assert.rejects(f.execution.advance(f.binding,f.claim),/crash/);f.store.commit=commit;
 assert.equal(f.store.load(f.binding).phase,'provider_admitted');
 const restarted=await f.restart();await restarted.advance(f.binding,f.claim);
 assert.equal(f.store.load(f.binding).phase,'provider_backoff');assert.equal(f.counts.provider,1);
 await restarted.advance(f.binding,f.claim);assert.equal(f.counts.provider,2);
});

test('retry_after_blocks_dispatch_and_lost_ownership_interrupts_wait',async t=>{
 const f=await executionFixture(t);rejectOnce(f,{delay:60000});
 await f.execution.advance(f.binding,f.claim);
 await f.execution.advance(f.binding,f.claim);assert.equal(f.counts.provider,1);
 const waiting=f.execution.advance(f.binding,f.claim);
 await f.ownership.release(f.claim);
 await assert.rejects(waiting,/authority lost/);assert.equal(f.counts.provider,1);
});

test('an_old_backoff_checkpoint_cannot_replay_a_newer_uncertain_retry',async t=>{
 const f=await executionFixture(t);rejectOnce(f);await f.execution.advance(f.binding,f.claim);
 f.results.clear(); // Controller already admitted retry; its response is now unknown.
 const restarted=await f.restart();await restarted.advance(f.binding,f.claim);
 assert.equal(f.store.load(f.binding).phase,'held');assert.equal(f.counts.provider,1);
});

test('invalid_or_mismatched_rejection_never_authorizes_retry',async t=>{
 for(const change of [{http_status:503},{request_sha:'0'.repeat(64)},{attempt:0},{retry_at_ms:NaN}]){
  const f=await executionFixture(t);rejectOnce(f);
  const result=f.gate.result;f.gate.result=async admission=>({...await result(admission),...change});
  await f.execution.advance(f.binding,f.claim);
  assert.equal(f.store.load(f.binding).phase,'held');assert.equal(f.counts.provider,1);
 }
});
