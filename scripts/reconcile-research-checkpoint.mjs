/** Offline migration of one proven pre-write rejection into a paused terminal.
 * Controller and backend writers must be stopped; the Python administrator
 * coordinator holds the execution lock and verifies the controller receipt.
 * This is an explicit maintenance migration, not a runtime recovery rule.
 */
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {canonical,digest,sha,sameBinding,validateSnapshot,LIMITS} from '../letta_recovery/contracts.mjs';
import {atomicWrite,PayloadStore,readRegular,safeDirectory} from '../letta_recovery/payload-store.mjs';
import {transition} from '../letta_recovery/state-machine.mjs';

export function migrate({proof,original,receipt,terminal,write=false}){
 const fail=()=>{throw Error('Exact research rejection migration proof differs');};
 if(proof.contract!=='historical_research_rejection_inspection_v1'||!proof.read_only
    ||sha(Buffer.from(canonical(original)))!==proof.checkpoint_sha256
    ||receipt.state!=='confirmed'||receipt.kind!=='tool'||receipt.admission_id!==proof.tool_admission_id
    ||receipt.request_sha!==proof.request_sha||receipt.result!==proof.validation_result
    ||receipt.result_sha!==digest(receipt.result)
    ||receipt.usage?.historical_research_rejection?.evidence_sha!==digest(proof))fail();
 const state=validateSnapshot(structuredClone(original));
 const expectedTerminal={id:digest({proof:digest(proof),kind:'terminal'}).slice(0,32),
  run_id:state.run_id,consumer_id:proof.binding.consumer_id,stop_reason:'owner_input_pending',
  assistant_ids:[],yield_receipt_id:digest({proof:digest(proof),kind:'yield'}).slice(0,32),cancelled_admission_ids:[]};
 if(canonical(terminal)!==canonical(expectedTerminal))fail();
 const tools=state.tools.filter(t=>t.admission_id===proof.tool_admission_id);
 if(!sameBinding(state.binding,proof.binding)||state.phase!=='held'||state.hold_reason!=='tool_uncertain'
    ||state.held_phase!=='tool_admitted'||state.claim||state.terminal||tools.length!==1)fail();
 const tool=tools[0];
 if(tool.name!=='research.save'||tool.state!=='admitted'||tool.request_sha!==proof.request_sha
    ||tool.arguments_ref!==proof.arguments_sha||state.steps.some(s=>s.state!=='confirmed')
    ||state.tools.some(t=>t!==tool&&t.state!=='confirmed'))fail();
 const root=dirname(dirname(proof.checkpoint_path));
 const key=digest({agent_id:proof.binding.agent_id,conversation_id:proof.binding.conversation_id});
 if(proof.checkpoint_path!==join(root,'conversations',key+'.json')||!safeDirectory(root))fail();
 const identity=JSON.parse(readRegular(join(root,'identity.json'),LIMITS.metadata));
 if(identity.store_id!==proof.binding.store_id)fail();
 const payloads=new PayloadStore({root:join(root,'blobs',key)});
 const args=JSON.parse(payloads.readPayload(tool.arguments_ref));
 if(Object.hasOwn(args,'id')||typeof args.findings!=='string'||[...args.findings].length!==2116
    ||digest({name:'research.save',arguments:args})!==proof.request_sha)fail();
 const rejection=JSON.parse(receipt.result);
 if(rejection.status!=='error'||rejection.error?.code!=='invalid_research_arguments'
    ||rejection.error.field!=='findings'||rejection.error.max_characters!==2000)fail();
 Object.assign(tool,{state:'confirmed',result_ref:receipt.result_sha});
 state.payload_refs=[...new Set([...state.payload_refs,receipt.result_sha])];
 const next=transition(state,{type:'owner_yield',terminal});
 const bytes=Buffer.from(canonical(next));
 if(bytes.length>LIMITS.metadata||payloads.usage()+bytes.length+Buffer.byteLength(canonical(receipt.result))>LIMITS.conversation)fail();
 const current=readRegular(proof.checkpoint_path,LIMITS.metadata);
 if(sha(current)!==proof.checkpoint_sha256&&!current.equals(bytes))fail();
 if(write){
  payloads.putPayload(Buffer.from(canonical(receipt.result)));
  if(!current.equals(bytes))atomicWrite(proof.checkpoint_path,bytes);
 }
 return {checkpoint_sha256:sha(bytes),backend_revision:next.revision,terminal:next.terminal,
         result_recorded:write,provider_calls:0,tool_calls:0};
}

if(process.argv[1]===fileURLToPath(import.meta.url)){
 try{
  let raw='';for await(const part of process.stdin)raw+=part;
  process.stdout.write(JSON.stringify(migrate(JSON.parse(raw)))+'\n');
 }catch{process.stderr.write('Research checkpoint migration refused; keep services stopped.\n');process.exitCode=1;}
}
