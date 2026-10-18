import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,writeFileSync} from 'node:fs';
import {executionFixture,textResponse} from './execution-fixture.mjs';

test('projection_commit_gap_does_not_duplicate',async t=>{
 const f=await executionFixture(t);await f.execution.advance(f.binding,f.claim);
 await f.execution.projectTranscript(f.store.load(f.binding));await f.execution.projectTranscript(f.store.load(f.binding));
 let rows=JSON.parse(readFileSync(f.projection));const original=rows.map(x=>x.operation_id);
 const recovered=await f.restart();await recovered.projectTranscript(f.store.load(f.binding));rows=JSON.parse(readFileSync(f.projection));
 assert.deepEqual(rows.map(x=>x.operation_id),original);assert.equal(new Set(original).size,original.length);
});
test('changed_projection_operation_digest_is_held',async t=>{
 const f=await executionFixture(t);await f.execution.advance(f.binding,f.claim);await f.execution.projectTranscript(f.store.load(f.binding));
 const rows=JSON.parse(readFileSync(f.projection));rows[0].digest='f'.repeat(64);writeFileSync(f.projection,JSON.stringify(rows));
 await assert.rejects(f.execution.projectTranscript(f.store.load(f.binding)),/projection_conflict/);
});
