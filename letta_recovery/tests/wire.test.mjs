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
