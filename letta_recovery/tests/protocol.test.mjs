import test from 'node:test';
import assert from 'node:assert/strict';
import {existsSync,readdirSync,readFileSync} from 'node:fs';
import {join} from 'node:path';
import {RecoveryProtocol} from '../protocol.mjs';
import {canonical,digest} from '../contracts.mjs';
import {directory,acquireLock,binding as baseBinding} from './fixtures.mjs';
import {textResponse} from './execution-fixture.mjs';

function fixture(t){
 const root=join(directory(t),'recovery');let provider=0,queue=0,scopeChecks=0;
 let rows=[];const results=new Map();
 const sdk={setScope(binding,scope){this.scope=scope;},checkScope(){scopeChecks++;},
 gate(){return {authorize:async(binding,claim,effect)=>({admission_id:effect.effect_id}),result:async({admission_id})=>results.get(admission_id)};},
 async executeProviderStep(request,admission){provider++;results.set(admission.admission_id,{state:'confirmed',admission_id:admission.admission_id,result:textResponse,result_sha:digest(textResponse),usage:{settled:true}});},
 readProjectionOperations(){return rows;},replaceProjection(binding,next){rows=next;}};
 const protocol=new RecoveryProtocol({root,sdk,connectionId:'connection-fixture',acquireLock});t.after(()=>protocol.close());
 const input={messages:[{role:'user',content:'fixture'}],tools:[]},binding={...baseBinding,store_id:protocol.storeId,input_sha:digest(input)};
 const scope={admission_id:'scope-fixture',scope_token:'a'.repeat(64)};
 const command=(type,fields={})=>protocol.handleRecoveryCommand({type,binding,...scope,...fields});
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
