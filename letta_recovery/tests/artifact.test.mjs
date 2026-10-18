import test from 'node:test';
import assert from 'node:assert/strict';
import {validateBinding, validateSnapshot, CONTRACT} from '../contracts.mjs';
import {createLocalRecovery} from '../entry.mjs';

export const binding = {store_id:'store-fixture',agent_id:'agent-fixture',conversation_id:'conversation-fixture',
 session_id:'session-fixture',cycle_id:'cycle-fixture',client_message_id:'input-fixture',consumer_id:'consumer-fixture',
 input_sha:'a'.repeat(64),owner_revision:'0',config_sha:'b'.repeat(64)};

test('invalid_binding_rejects_missing_identity',()=>{
 assert.throws(()=>validateBinding({}),/binding/);
 for(const change of [{client_message_id:''},{input_sha:'x'},{owner_revision:0},{unknown:'extra'},{agent_id:'../escape'}])
  assert.throws(()=>validateBinding({...binding,...change}),/binding/);
 assert.deepEqual(validateBinding(binding),binding);
});
test('invalid_snapshot_cannot_look_completed',()=>{
 const snapshot={version:1,binding,revision:1,fence:0,phase:'input_committed',run_id:'run-fixture',steps:[],tools:[],terminal:null};
 assert.deepEqual(validateSnapshot(snapshot),snapshot);
 for(const change of [{revision:true},{fence:-1},{phase:'completed'},{version:2}])
  assert.throws(()=>validateSnapshot({...snapshot,...change}),/snapshot/);
});
test('inactive_artifact_never_advertises_or_executes_recovery',()=>{
 let calls=0;const gate={authorize(){calls++;throw Error('must not dispatch');}};
 const recovery=createLocalRecovery({sdk:{},root:'/unused',enabled:false,gate});
 assert.equal(CONTRACT,'local_durable_recovery_v1');
 assert.equal(recovery.capability,null);
 assert.equal(calls,0);
});
