import test from 'node:test';
import assert from 'node:assert/strict';
import {LocalSDKAdapter} from '../sdk-adapter.mjs';
import {atomicTranscriptWrite} from '../entry.mjs';
import {directory} from './fixtures.mjs';
import {join} from 'node:path';
import {writeFileSync,readFileSync} from 'node:fs';

test('adapter_compaction_preserves_typed_tail_and_journals_exact_summary_request',async()=>{
 const sdk=new LocalSDKAdapter({});sdk.connection=async()=>({model:'fixture',contextWindow:5000,maxTokens:1000});
 const context={model:'fixture',_recovery_prepared:true,tools:[],messages:[{role:'system',content:'system fixture'},
 {role:'user',content:'old '.repeat(5000)},{role:'assistant',content:'old answer'},
 {role:'user',content:'current input'},{role:'assistant',content:null,tool_calls:[{id:'call-current',type:'function',function:{name:'fixture',arguments:'{}'}}]},
 {role:'tool',tool_call_id:'call-current',content:'exact saved result'}]};
 const planned=await sdk.prepareProviderStep({},context);
 assert.equal(planned.kind,'compaction');assert.equal(planned.request.messages.length,2);
 assert.ok(planned.request.messages[0].content.startsWith('Your task is to create a detailed summary of the conversation so far,'));
 const completed=await sdk.completeCompaction(planned.context,{choices:[{message:{content:'Exact confirmed summary.'}}]});
 assert.deepEqual(completed.messages.slice(-3),context.messages.slice(-3));
 assert.equal(completed.messages[0].content,'system fixture');assert.equal(completed.messages[1].content,'Conversation summary:\nExact confirmed summary.');
 assert.equal((await sdk.prepareProviderStep({},completed)).kind,'provider');
});

test('atomic_projection_retains_original_transcript_bytes',t=>{
 const path=join(directory(t),'messages.jsonl');
 const header={type:'session',id:'fixture'},message={id:'old',role:'user',content:[{type:'text',text:'prior fixture'}]};
 const original=JSON.stringify(header,null,0)+'\n'+JSON.stringify({type:'message',id:'original-id',parentId:null,message})+'\n';writeFileSync(path,original);
 const added={id:'new',role:'user',content:[{type:'text',text:'current fixture'}],metadata:{andrew_recovery:{operation_id:'input:fixture',digest:'a'.repeat(64)}}};
 const rewrite=[header,{type:'message',id:'changed-id',parentId:null,message},{type:'message',id:'random',parentId:'changed-id',message:added}].map(row=>JSON.stringify(row)).join('\n')+'\n';
 atomicTranscriptWrite(path,rewrite);const result=readFileSync(path,'utf8');assert.ok(result.startsWith(original));
 atomicTranscriptWrite(path,rewrite);assert.equal(readFileSync(path,'utf8'),result);
});
