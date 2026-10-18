import test from 'node:test';
import assert from 'node:assert/strict';
import {CheckpointStore} from '../checkpoint-store.mjs';
import {executionFixture} from './execution-fixture.mjs';
import {digest} from '../contracts.mjs';

test('next_input_requires_acknowledged_settled_terminal_and_retains_prior_identity',async t=>{
 const f=await executionFixture(t);
 const next={...f.binding,cycle_id:'next-cycle',consumer_id:'next-consumer',client_message_id:'next-input'};
 await assert.rejects(Promise.resolve().then(()=>f.store.admitNext(next,f.input,f.binding,f.store.load(f.binding).revision)),/transition/);
 for(let n=0;n<2;n++)await f.execution.advance(f.binding,f.claim);
 let previous=f.store.load(f.binding);
 assert.throws(()=>f.store.admitNext(next,f.input,f.binding,previous.revision),/transition/);
 f.store.commit(f.binding,previous.revision,{type:'ack',terminal_id:previous.terminal.id,consumer_id:f.binding.consumer_id,fence:f.claim.fence,claim_id:f.claim.claim_id,settled:true});
 await f.ownership.release(f.claim);previous=f.store.load(f.binding);
 const admitted=f.store.admitNext(next,f.input,f.binding,previous.revision);
 assert.equal(admitted.phase,'input_committed');assert.ok(admitted.fence>previous.fence);assert.ok(admitted.revision>previous.revision);
 assert.notEqual(admitted.run_id,previous.run_id);assert.equal(f.store.load(f.binding).run_id,previous.run_id);
 assert.equal(f.store.load(next).run_id,admitted.run_id);
 assert.throws(()=>f.store.commit(f.binding,previous.revision,{type:'hold',reason:'owner_blocked'}),/binding/);
 const restarted=new CheckpointStore({root:f.store.root,acquireLock:f.store.acquireLock});
 assert.equal(restarted.load(f.binding).terminal.acknowledged,true);
 assert.throws(()=>restarted.admitNext({...next,cycle_id:'third-cycle'},f.input,f.binding,previous.revision),/binding|revision/);
});
