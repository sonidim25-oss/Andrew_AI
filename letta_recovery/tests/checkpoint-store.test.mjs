import test from 'node:test';
import assert from 'node:assert/strict';
import {existsSync,readdirSync,readFileSync,writeFileSync,symlinkSync,statSync,mkdirSync} from 'node:fs';
import {join} from 'node:path';
import {spawnSync} from 'node:child_process';
import {CheckpointStore} from '../checkpoint-store.mjs';
import {canonical,sha,digest} from '../contracts.mjs';
import {binding,acquireLock,directory} from './fixtures.mjs';

function initialized(t,limits){const root=join(directory(t),'state');const store=new CheckpointStore({root,acquireLock,limits});store.initialize(binding.store_id);return {store,root};}
function input(store){return store.commit(binding,null,{type:'input',input:{messages:[{role:'user',content:'fixture'}]}},[]);}

test('missing_inspection_does_not_create_state',t=>{
 const root=join(directory(t),'absent');const store=new CheckpointStore({root,acquireLock});
 assert.equal(store.inspect(binding).status,'missing');assert.equal(existsSync(root),false);
});
test('commit_survives_process_exit',t=>{
 const {store,root}=initialized(t);const snapshot=input(store);
 const module=new URL('../checkpoint-store.mjs',import.meta.url).href;
 const helper=new URL('./fixtures.mjs',import.meta.url).href;
 const source=`import {CheckpointStore} from '${module}';import {acquireLock,binding} from '${helper}';const s=new CheckpointStore({root:process.argv[1],acquireLock});s.commit(binding,1,{type:'provider_admit',effect_id:'step-1',request_sha:'${'c'.repeat(64)}'},[]);process.exit(19);`;
 const process=spawnSync('node',['--input-type=module','-e',source,root],{encoding:'utf8'});
 assert.equal(process.status,19,process.stderr);
 assert.equal(store.load(binding).phase,'provider_admitted');assert.equal(store.load(binding).run_id,snapshot.run_id);
 assert.equal(statSync(root).mode&0o777,0o700);
});
test('stale_revision_cannot_overwrite',t=>{
 const {store}=initialized(t);input(store);const second=store.commit(binding,1,{type:'provider_admit',effect_id:'step-1',request_sha:'c'.repeat(64)},[]);
 assert.throws(()=>store.commit(binding,1,{type:'hold',reason:'provider_uncertain'},[]),/revision/);
 assert.deepEqual(store.load(binding),second);
 assert.throws(()=>store.load({...binding,owner_revision:'1'}),/binding/);
});
test('corrupt_or_missing_blob_holds',t=>{
 const {store,root}=initialized(t);const snapshot=input(store);
 const blob=join(store.payloadStore(binding).root,snapshot.input_ref);
 writeFileSync(blob,'corrupt');assert.equal(store.inspect(binding).hold_reason,'state_corrupt');
});
test('size_and_unsafe_file_limits_hold',t=>{
 const {store,root}=initialized(t,{metadata:1048576,payload:16,conversation:268435456});
 assert.throws(()=>input(store),/storage_limit/);assert.equal(store.inspect(binding).status,'missing');
 const parent=directory(t),target=join(parent,'target');mkdirSync(target);symlinkSync(target,join(parent,'alias'));
 const unsafe=new CheckpointStore({root:join(parent,'alias','state'),acquireLock});
 assert.throws(()=>unsafe.initialize(binding.store_id),/unsafe/);
 const second=initialized(t);input(second.store);
 const checkpoint=second.store.checkpointPath(binding);
 writeFileSync(checkpoint,'{"version":1,"version":2}');
 assert.equal(second.store.inspect(binding).hold_reason,'state_corrupt');
});
test('exact_response_transition_requires_a_committed_matching_admission',t=>{
 const {store}=initialized(t);input(store);
 assert.throws(()=>store.commit(binding,1,{type:'provider_confirm',effect_id:'step-1',result:{text:'fixture'}},[]),/transition/);
 store.commit(binding,1,{type:'provider_admit',effect_id:'step-1',request_sha:'c'.repeat(64)},[]);
 const done=store.commit(binding,2,{type:'provider_confirm',effect_id:'step-1',result:{choices:[{message:{role:'assistant',content:'hello'}}]},usage:{cost:.001}},[]);
 assert.equal(done.phase,'provider_confirmed');assert.equal(done.steps.length,1);
 assert.equal(statSync(store.checkpointPath(binding)).mode&0o777,0o600);
});

test('quota_is_scoped_to_one_conversation',t=>{
 const {store}=initialized(t,{conversation:1800});
 const firstInput={messages:[{role:'user',content:'x'.repeat(600)}]},secondInput={messages:[{role:'user',content:'y'.repeat(600)}]};
 const first={...binding,input_sha:digest(firstInput)},other={...binding,conversation_id:'other-conversation',input_sha:digest(secondInput)};
 store.commit(first,null,{type:'input',input:firstInput},[]);
 store.commit(other,null,{type:'input',input:secondInput},[]);
 assert.equal(store.inspect(other).phase,'input_committed');
 assert.equal(store.load(first).revision,1);
});

test('projection_bytes_count_toward_the_conversation_quota',t=>{
 const {store}=initialized(t,{conversation:2000});input(store);
 assert.throws(()=>store.assertProjectionBudget(binding,1900),/storage_limit/);
 writeFileSync(store.checkpointPath(binding).replace(/\.json$/,'.projection.json'),'x'.repeat(1900));
 assert.throws(()=>store.commit(binding,1,{type:'provider_admit',effect_id:'step-1',request_sha:'c'.repeat(64)}),/storage_limit/);
});
test('malformed_confirmed_checkpoint_is_not_completion_evidence',t=>{
 const {store}=initialized(t);input(store);const path=store.checkpointPath(binding);
 const value=JSON.parse(readFileSync(path));value.phase='provider_confirmed';
 writeFileSync(path,canonical(value));assert.equal(store.inspect(binding).hold_reason,'state_corrupt');
});
test('crash_at_checkpoint_replacement_keeps_only_committed_boundaries',t=>{
 for(const when of ['before','after']){
  const {store,root}=initialized(t);input(store);
  const module=new URL('../checkpoint-store.mjs',import.meta.url).href,helper=new URL('./fixtures.mjs',import.meta.url).href;
  const code=`import fs from 'node:fs';import {syncBuiltinESMExports} from 'node:module';import {CheckpointStore} from '${module}';import {acquireLock,binding} from '${helper}';const original=fs.renameSync;fs.renameSync=(a,b)=>{if(b.includes('/conversations/')&&b.endsWith('.json')){if('${when}'==='before')process.exit(18);original(a,b);process.exit(19);}return original(a,b);};syncBuiltinESMExports();const s=new CheckpointStore({root:process.argv[1],acquireLock});s.commit(binding,1,{type:'provider_admit',effect_id:'step-1',request_sha:'${'c'.repeat(64)}',request:{model:'fixture'}},[]);`;
  const child=spawnSync('node',['--input-type=module','-e',code,root],{encoding:'utf8'});
  assert.equal(child.status,when==='before'?18:19,child.stderr);
  assert.equal(store.load(binding).revision,when==='before'?1:2);
  assert.equal(store.inspect(binding).phase,when==='before'?'input_committed':'provider_admitted');
 }
});

test('fifo_symlink_and_actual_default_byte_limits_are_rejected',t=>{
 const {store,root}=initialized(t);input(store);
 assert.throws(()=>store.payloadStore(binding).putPayload(Buffer.alloc(16777217)),/storage_limit/);
 assert.throws(()=>store.commit(binding,1,{type:'claim',claim:{fence:1,padding:'x'.repeat(1048576)}},[]),/storage_limit/);
 const path=store.checkpointPath(binding);const fs=process.getBuiltinModule('fs');fs.unlinkSync(path);
 const made=spawnSync('mkfifo',[path]);assert.equal(made.status,0);
 assert.equal(store.inspect(binding).hold_reason,'state_corrupt');fs.unlinkSync(path);
 const elsewhere=join(root,'unrelated');writeFileSync(elsewhere,'{}');symlinkSync(elsewhere,path);
 assert.equal(store.inspect(binding).hold_reason,'state_corrupt');
});
