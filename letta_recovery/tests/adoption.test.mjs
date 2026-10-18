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

test('legacy_started_or_queued_input_without_checkpoint_prevents_adoption',t=>{
 const root=directory(t),path=join(root,'legacy.json'),runtimeKey='scope-fixture';
 const backend={activeRunByConversation:new Map()},args={backend,runtimeKey,dispositionPath:path};
 assertAdoptable(binding,args);
 for(const disposition of ['started','queued']){
  const key=JSON.stringify([runtimeKey,'input','old-input']);
  atomicWrite(path,Buffer.from(JSON.stringify({version:4,entries:{[key]:{disposition,replayCompleted:true}},reservations:{}})));
  assert.throws(()=>assertAdoptable(binding,args),/legacy_unproven/);
 }
 backend.activeRunByConversation.set(binding.conversation_id,'active-run');
 assert.throws(()=>assertAdoptable(binding,args),/legacy_unproven/);
});
