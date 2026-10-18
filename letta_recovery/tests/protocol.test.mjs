import test from 'node:test';
import assert from 'node:assert/strict';
import {existsSync,readdirSync,readFileSync} from 'node:fs';
import {join} from 'node:path';
import {RecoveryProtocol} from '../protocol.mjs';
import {CONTRACT,canonical,digest} from '../contracts.mjs';
import {directory,acquireLock,binding as baseBinding} from './fixtures.mjs';
import {textResponse} from './execution-fixture.mjs';

function fixture(t){
 const root=join(directory(t),'recovery');let provider=0,queue=0,scopeChecks=0;
 let rows=[];const results=new Map();
 const sdk={assertAdoptable(){},checkEvidence(){},setScope(binding,scope){this.scope=scope;},checkScope(){scopeChecks++;},
 gate(){return {authorize:async(binding,claim,effect)=>({admission_id:effect.effect_id}),result:async({admission_id})=>results.get(admission_id)};},
 async executeProviderStep(request,admission){provider++;results.set(admission.admission_id,{state:'confirmed',admission_id:admission.admission_id,result:textResponse,result_sha:digest(textResponse),usage:{settled:true}});},
 readProjectionOperations(){return rows;},replaceProjection(binding,next){rows=next;}};
 const protocol=new RecoveryProtocol({root,sdk,connectionId:'connection-fixture',acquireLock});t.after(()=>protocol.close());
 const input={messages:[{role:'user',content:'fixture'}],tools:[]},binding={...baseBinding,store_id:protocol.storeId,input_sha:digest(input)};
 const scope={admission_id:'scope-fixture',scope_token:'a'.repeat(64)};
 const command=(type,fields={})=>protocol.handleRecoveryCommand({type,contract:CONTRACT,version:1,...(type==='recovery_capabilities'?{}:{binding,...scope}),...fields});
 return {root,protocol,command,input,binding,scope,get provider(){return provider;},get queue(){return queue;},get scopeChecks(){return scopeChecks;}};
}
async function admitted(f){await f.command('recovery_admit',{input:f.input});const revision=f.protocol.store.load(f.binding).revision;return f.command('recovery_claim',{expected_revision:revision});}
async function completed(f){for(let n=0;n<100;n++){const state=await f.command('recovery_status',{attempt_id:'attempt-fixture'});if(['terminal_committed','held'].includes(state.phase))return state;await new Promise(resolve=>setTimeout(resolve,10));}throw Error('fixture timed out');}

test('inspection_and_handshake_are_evidence_only',async t=>{
 const f=fixture(t);for(let n=0;n<2;n++){
  await f.command('recovery_capabilities');const state=await f.command('recovery_inspect');assert.equal(state.hold_reason,'legacy_unproven');
  await f.command('recovery_status');await f.command('recovery_subscribe');
 }
 assert.equal(existsSync(f.root),false);assert.equal(f.provider,0);assert.equal(f.scopeChecks,0);
});
test('lost_resume_reply_reuses_attempt_and_subscription_does_not_pump_unrelated_queue',async t=>{
 const f=fixture(t),claim=await admitted(f),fields={attempt_id:'attempt-fixture',expected_revision:claim.revision,claim_id:claim.claim_id,fence:claim.fence};
 await f.command('recovery_resume',fields);await f.command('recovery_resume',fields);
 const result=await completed(f);assert.equal(result.phase,'terminal_committed');assert.equal(f.provider,1);
 await f.command('recovery_subscribe',{attempt_id:'attempt-fixture'});await f.command('recovery_resume',fields);
 assert.equal(f.provider,1);assert.equal(f.queue,0);
 await assert.rejects(f.command('recovery_resume',{...fields,expected_revision:claim.revision+1}),/binding_mismatch/);
});
test('old_consumer_cannot_settle_new_input',async t=>{
 const f=fixture(t),claim=await admitted(f);await f.command('recovery_resume',{attempt_id:'attempt-fixture',expected_revision:claim.revision,claim_id:claim.claim_id,fence:claim.fence});
 const state=await completed(f),snapshot=f.protocol.store.load(f.binding);
 await assert.rejects(f.command('recovery_ack',{claim_id:claim.claim_id,fence:claim.fence,expected_revision:snapshot.revision,terminal_id:state.terminal.id,consumer_id:'old-consumer'}),/transition/);
 assert.equal(f.protocol.store.load(f.binding).terminal.acknowledged,false);
});
test('legacy_started_without_checkpoint_is_held',async t=>{
 const f=fixture(t);assert.equal((await f.command('recovery_inspect')).hold_reason,'legacy_unproven');
 await assert.rejects(f.command('recovery_claim',{expected_revision:1}),/stale revision/);assert.equal(f.provider,0);
});


test('claim_cannot_authorize_another_binding_or_admission',async t=>{
 const f=fixture(t),claim=await admitted(f);
 const fields={claim_id:claim.claim_id,fence:claim.fence};
 const before=readFileSync(f.protocol.store.checkpointPath(f.binding));
 for(const type of ['recovery_renew','recovery_release','recovery_revoke','recovery_ack','recovery_resume']){
  const extra=type==='recovery_resume'?{attempt_id:'foreign-attempt',expected_revision:claim.revision}:type==='recovery_ack'?{expected_revision:claim.revision,terminal_id:'foreign-terminal',consumer_id:f.binding.consumer_id}:{};
  await assert.rejects(f.command(type,{...fields,...extra,binding:{...f.binding,cycle_id:'another-cycle'}}),/authority_lost/);
  await assert.rejects(f.command(type,{...fields,...extra,admission_id:'another-scope'}),/authority_lost/);
 }
 assert.deepEqual(readFileSync(f.protocol.store.checkpointPath(f.binding)),before);
 assert.equal(f.provider,0);
});

test('disconnected_connection_cannot_acquire_or_resume',async t=>{
 const f=fixture(t);await f.command('recovery_admit',{input:f.input});
 await f.protocol.close();
 await assert.rejects(f.command('recovery_claim',{expected_revision:f.protocol.store.load(f.binding).revision}),/authority_lost/);
 assert.equal(f.protocol.store.load(f.binding).claim,undefined);assert.equal(f.provider,0);
});

test('disconnect_during_claim_validation_releases_new_lock',async t=>{
 const f=fixture(t);await f.command('recovery_admit',{input:f.input});
 let entered,finish;const checking=new Promise(resolve=>entered=resolve),blocked=new Promise(resolve=>finish=resolve);
 f.protocol.sdk.checkScope=async()=>{entered();await blocked;};
 const pending=f.command('recovery_claim',{expected_revision:f.protocol.store.load(f.binding).revision});
 const rejected=assert.rejects(pending,/authority_lost/);
 await checking;await f.protocol.close();assert.equal(f.protocol.store.load(f.binding).claim,null);finish();await rejected;
 assert.equal(f.protocol.claims.size,0);assert.equal(f.protocol.store.load(f.binding).claim,null);assert.equal(f.provider,0);
});


test('raw_results_require_a_live_connection_claim_and_controller_scope',async t=>{
 const f=fixture(t),claim=await admitted(f);
 await f.command('recovery_resume',{attempt_id:'attempt-fixture',expected_revision:claim.revision,claim_id:claim.claim_id,fence:claim.fence});
 await completed(f);const snapshot=f.protocol.store.load(f.binding),effect_admission_id=snapshot.steps[0].admission_id;
 await assert.rejects(f.command('recovery_result_read',{effect_admission_id,claim_id:'foreign-claim',fence:claim.fence}),/authority_lost/);
 const fields={effect_admission_id,claim_id:claim.claim_id,fence:claim.fence};
 const checks=f.scopeChecks,result=await f.command('recovery_result_read',fields);
 assert.deepEqual(result.result,textResponse);assert.equal(f.scopeChecks,checks+1);
 f.protocol.sdk.checkScope=async()=>{throw Error('owner_blocked');};
 await assert.rejects(f.command('recovery_result_read',fields),/owner_blocked/);
});


test('disconnect_during_resume_validation_cannot_schedule_an_effect',async t=>{
 const f=fixture(t),claim=await admitted(f);
 let entered,finish;const checking=new Promise(resolve=>entered=resolve),blocked=new Promise(resolve=>finish=resolve);
 f.protocol.sdk.checkScope=async()=>{entered();await blocked;};
 const pending=f.command('recovery_resume',{attempt_id:'late-attempt',expected_revision:claim.revision,claim_id:claim.claim_id,fence:claim.fence});
 const rejected=assert.rejects(pending,/authority_lost/);
 await checking;await f.protocol.close();finish();await rejected;
 assert.equal(f.protocol.store.load(f.binding).attempts,undefined);
 assert.equal(f.protocol.jobs.size,0);assert.equal(f.provider,0);
});
