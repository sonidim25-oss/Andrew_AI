import test from 'node:test';
import assert from 'node:assert/strict';
import {validateCommand} from '../wire.mjs';
import {CONTRACT} from '../contracts.mjs';
import {binding} from './fixtures.mjs';

test('commands_require_exact_contract_bounded_ids_and_known_fields',()=>{
 const command={type:'recovery_inspect',binding,contract:CONTRACT,version:1};
 assert.equal(validateCommand(command),command);
 for(const changes of [{version:2},{version:'1'},{contract:'foreign'},{unknown:'ignored?'},{attempt_id:'__proto__..'},{type:'recovery_invented'},{binding:{...binding,extra:1}}])assert.throws(()=>validateCommand({...command,...changes}));
 for(const expected_revision of [-1,NaN,Infinity,1.1,true,Number.MAX_SAFE_INTEGER+1])assert.throws(()=>validateCommand({type:'recovery_claim',contract:CONTRACT,version:1,binding,admission_id:'scope',scope_token:'a'.repeat(64),expected_revision}));
});

test('runner_tool_import_takes_evidence_fields_only_and_no_caller_claim_or_result',()=>{
 const command={type:'recovery_import_runner_tool',contract:CONTRACT,version:1,binding,admission_id:'scope',scope_token:'a'.repeat(64),expected_revision:3,effect_admission_id:'tool-admission'};
 assert.equal(validateCommand(command),command);
 for(const changes of [{claim_id:'claim'},{fence:1},{result:'caller supplied'},{runner_receipt:{}},{effect_admission_id:undefined},{expected_revision:-1}]){
  const changed={...command,...changes};for(const key of Object.keys(changed))if(changed[key]===undefined)delete changed[key];
  assert.throws(()=>validateCommand(changed),JSON.stringify(changes));
 }
});
