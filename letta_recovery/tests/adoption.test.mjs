import test from 'node:test';
import assert from 'node:assert/strict';
import {join} from 'node:path';
import {adopt,guardLegacy,assertAdoptable} from '../adoption.mjs';
import {directory,binding} from './fixtures.mjs';
import {safeDirectory,atomicWrite} from '../payload-store.mjs';

test('adopted_conversation_rejects_every_legacy_execution_entry_and_keeps_reads',async t=>{
 const root=directory(t);adopt(root,binding);
 for(const type of ['runtime_start','sync','message','user_message','approval_response','external_tool_call_response','abort_message','turn_finished_ack','update_toolset','conversation_delete','runtime_external_tools_update','teleport_request']){
  await assert.rejects(guardLegacy({type,runtime:{agent_id:binding.agent_id,conversation_id:binding.conversation_id}},{root,sdk:{}}),/unsupported_contract/);
 }
 assert.equal(await guardLegacy({type:'conversation_messages_list',conversation_id:binding.conversation_id},{root,sdk:{}}),false);
 assert.equal(await guardLegacy({type:'runtime_start',agent_id:'another-agent',conversation_id:'another-conversation'},{root,sdk:{}}),false);
 await assert.rejects(guardLegacy({type:'runtime_start'},{root,sdk:{}}),/unsupported_contract/);
});

test('legacy_input_identity_cannot_be_readmitted_without_checkpoint',t=>{
 const root=directory(t),path=join(root,'legacy.json'),runtimeKey='scope-fixture';
 const backend={activeRunByConversation:new Map()},args={backend,runtimeKey,dispositionPath:path};
 assertAdoptable(binding,args);
 for(const disposition of ['started','queued']){
  const key=JSON.stringify([runtimeKey,'input',binding.client_message_id]);
  atomicWrite(path,Buffer.from(JSON.stringify({version:4,entries:{[key]:{disposition,replayCompleted:true}},reservations:{}})));
  assert.throws(()=>assertAdoptable(binding,args),/legacy_unproven/);
 }
 backend.activeRunByConversation.set(binding.conversation_id,'active-run');
 assert.throws(()=>assertAdoptable(binding,args),/legacy_unproven/);
});

test('distinct_new_input_can_freeze_old_tombstones_without_certifying_or_replaying_them',async t=>{
 const root=directory(t),path=join(root,'legacy.json'),runtimeKey='scope-fixture';
 const backend={activeRunByConversation:new Map()},args={backend,runtimeKey,dispositionPath:path};
 const key=JSON.stringify([runtimeKey,'input','older-unproven-input']);
 atomicWrite(path,Buffer.from(JSON.stringify({version:4,entries:{[key]:{disposition:'started',replayCompleted:true}},reservations:{}})));
 const original=(await import('node:fs')).readFileSync(path);
 assertAdoptable(binding,args);adopt(root,binding);
 assert.deepEqual((await import('node:fs')).readFileSync(path),original);
 await assert.rejects(guardLegacy({type:'runtime_start',agent_id:binding.agent_id,conversation_id:binding.conversation_id},{root,sdk:{}}),/unsupported_contract/);
 for(const entry of [{disposition:'queued',replayCompleted:true},{disposition:'started',queuedInput:{},replayCompleted:true}]){
  atomicWrite(path,Buffer.from(JSON.stringify({version:4,entries:{[key]:entry},reservations:{}})));
  assert.throws(()=>assertAdoptable(binding,args),/legacy_unproven/);
 }
 atomicWrite(path,Buffer.from(JSON.stringify({version:4,entries:{},reservations:{[key]:{}}})));
 assert.throws(()=>assertAdoptable(binding,args),/legacy_unproven/);
});
