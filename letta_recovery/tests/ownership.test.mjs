import test from 'node:test';
import assert from 'node:assert/strict';
import {join} from 'node:path';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {CheckpointStore} from '../checkpoint-store.mjs';
import {OwnershipManager} from '../ownership.mjs';
import {binding,acquireLock,directory} from './fixtures.mjs';

const make=t=>{const root=join(directory(t),'state');const store=new CheckpointStore({root,acquireLock});store.initialize(binding.store_id);store.commit(binding,null,{type:'input',input:{messages:[{role:'user',content:'fixture'}]}},[]);return {root,store};};
const clock=()=>{let value=1000;return {now:()=>value,advance:amount=>value+=amount};};

test('live_os_lock_blocks_expired_lease_takeover',async t=>{
 const {store}=make(t),time=clock();const first=new OwnershipManager({store,clock:time.now,bootId:'boot-fixture'}),second=new OwnershipManager({store,clock:time.now,bootId:'boot-fixture'});
 const claim=await first.claim(binding,1,'admission-1');t.after(()=>first.release(claim));
 assert.equal(claim.expires_monotonic_ms,16000);time.advance(15001);
 await assert.rejects(second.claim(binding,store.load(binding).revision,'admission-2'),/owned/);
 assert.throws(()=>first.assertOwned(claim),/authority/);
});
test('stale_fence_cannot_dispatch',async t=>{
 const {store}=make(t),time=clock();const first=new OwnershipManager({store,clock:time.now,bootId:'boot-fixture'}),second=new OwnershipManager({store,clock:time.now,bootId:'boot-fixture'});
 const old=await first.claim(binding,1,'admission-1');await first.release(old);
 const next=await second.claim(binding,store.load(binding).revision,'admission-2');t.after(()=>second.release(next));
 assert.equal(next.fence,old.fence+1);assert.throws(()=>first.assertOwned(old),/authority/);
 second.assertOwned(next);
});
test('boot_change_invalidates_lease',async t=>{
 const {store}=make(t),time=clock();let boot='boot-1';const manager=new OwnershipManager({store,clock:time.now,bootId:()=>boot});
 const claim=await manager.claim(binding,1,'admission-1');t.after(()=>manager.release(claim));boot='boot-2';
 assert.throws(()=>manager.assertOwned(claim),/authority/);
});
test('clock_jump_cannot_extend_authority',async t=>{
 const {store}=make(t),time=clock();const manager=new OwnershipManager({store,clock:time.now,bootId:'boot-1'});
 const claim=await manager.claim(binding,1,'admission-1');t.after(()=>manager.release(claim));time.advance(-1);
 assert.throws(()=>manager.assertOwned(claim),/authority/);
});
test('renewal_preserves_fence_and_uses_fifteen_second_lease',async t=>{
 const {store}=make(t),time=clock();const manager=new OwnershipManager({store,clock:time.now,bootId:'boot-1'});
 const claim=await manager.claim(binding,1,'admission-1');t.after(()=>manager.release(claim));time.advance(5000);
 const renewed=await manager.renew(claim);assert.equal(renewed.fence,claim.fence);assert.equal(renewed.expires_monotonic_ms,21000);
 manager.assertOwned(renewed);
});
test('parent_exit_releases_execution_lock',async t=>{
 const {store,root}=make(t);
 const own=new URL('../ownership.mjs',import.meta.url).href,st=new URL('../checkpoint-store.mjs',import.meta.url).href,fixture=new URL('./fixtures.mjs',import.meta.url).href;
 const code=`import {OwnershipManager} from '${own}';import {CheckpointStore} from '${st}';import {binding,acquireLock} from '${fixture}';const store=new CheckpointStore({root:process.argv[1],acquireLock});const manager=new OwnershipManager({store});const claim=await manager.claim(binding,1,'parent-admission');console.log(JSON.stringify(claim));setInterval(()=>{},1000);`;
 const child=spawn('node',['--input-type=module','-e',code,root],{stdio:['pipe','pipe','pipe']});t.after(()=>child.kill());
 let line='';for await(const chunk of child.stdout){line+=chunk;if(line.includes('\n'))break;}
 const claim=JSON.parse(line);assert.equal(claim.fence,1);child.kill('SIGKILL');await once(child,'exit');
 const replacement=new OwnershipManager({store});let next;
 for(let i=0;i<30;i++){
  try{next=await replacement.claim(binding,store.load(binding).revision,'replacement-admission');break;}
  catch(error){if(!/owned/.test(error.message))throw error;await new Promise(r=>setTimeout(r,10));}
 }
 assert.ok(next);t.after(()=>replacement.release(next));assert.equal(next.fence,2);
});

test('stale_worker_result_cannot_overwrite_successor_even_with_current_revision',async t=>{
 const {store}=make(t);const first=new OwnershipManager({store}),second=new OwnershipManager({store});
 const old=await first.claim(binding,1,'old-admission');await first.release(old);
 const replacement=await second.claim(binding,store.load(binding).revision,'new-admission');t.after(()=>second.release(replacement));
 const before=store.load(binding);
 assert.throws(()=>store.commit(binding,before.revision,{type:'provider_admit',effect_id:'stale-step',request_sha:'c'.repeat(64),fence:old.fence,claim_id:old.claim_id},[]),/authority/);
 assert.deepEqual(store.load(binding),before);
});
