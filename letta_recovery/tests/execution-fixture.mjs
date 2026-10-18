import {join} from 'node:path';
import {readFileSync,existsSync} from 'node:fs';
import {CheckpointStore} from '../checkpoint-store.mjs';
import {OwnershipManager} from '../ownership.mjs';
import {RecoveryExecution} from '../execution.mjs';
import {atomicWrite} from '../payload-store.mjs';
import {canonical,digest} from '../contracts.mjs';
import {binding as baseBinding,acquireLock,directory} from './fixtures.mjs';

export const textResponse={id:'provider-fixture',choices:[{index:0,message:{role:'assistant',content:'Fixture completed.'},finish_reason:'stop'}],usage:{prompt_tokens:5,completion_tokens:3,cost:.001}};
export const toolResponse={id:'provider-tool-fixture',choices:[{index:0,message:{role:'assistant',content:null,tool_calls:[{id:'call-fixture',type:'function',function:{name:'recent_activity',arguments:'{}'}}]},finish_reason:'tool_calls'}],usage:{prompt_tokens:5,completion_tokens:3,cost:.001}};
export async function executionFixture(t,{responses=[textResponse],unknownProvider=false,unknownTool=false}={}){
 const root=directory(t),input={model:'fixture',messages:[{role:'user',content:'fixture'}],tools:[{type:'function',function:{name:'recent_activity',parameters:{type:'object'}}}]};
 const binding={...baseBinding,input_sha:digest(input)},store=new CheckpointStore({root:join(root,'state'),acquireLock});store.initialize(binding.store_id);
 const ownership=new OwnershipManager({store}),results=new Map(),counts={provider:0,tool:0},projection=join(root,'projection.json');
 const gate={async authorize(bound,claim,effect){ownership.assertOwned(claim);return {...effect,admission_id:effect.effect_id,binding:bound,token:'fixture-token',fence:claim.fence};},async result(admission){return results.get(admission.admission_id)??{state:'uncertain',admission_id:admission.admission_id};}};
 const sdk={async executeProviderStep(request,admission){counts.provider++;if(unknownProvider)throw Error('fixture response lost');const result=responses.shift();results.set(admission.admission_id,{state:'confirmed',admission_id:admission.admission_id,result,result_sha:digest(result),usage:{settled:true,cost:.001}});return result;},
 async executeTool(call,admission){counts.tool++;if(unknownTool)throw Error('fixture tool result lost');const result={entries:['fixture event']};results.set(admission.admission_id,{state:'confirmed',admission_id:admission.admission_id,result,result_sha:digest(result),usage:null});return result;},
 readProjectionOperations(){return existsSync(projection)?JSON.parse(readFileSync(projection)):[];},replaceProjection(scope,rows){atomicWrite(projection,Buffer.from(canonical(rows)));}};
 const execution=new RecoveryExecution({store,ownership,gate,sdk});await execution.admitInput(binding,input);
 let claim=await ownership.claim(binding,1,'fixture-admission');t.after(()=>ownership.release(claim));
 return {root,input,binding,store,ownership,gate,sdk,execution,results,counts,projection,get claim(){return claim;},async restart(){await ownership.release(claim);claim=await ownership.claim(binding,store.load(binding).revision,'replacement-admission');return new RecoveryExecution({store,ownership,gate,sdk});}};
}
