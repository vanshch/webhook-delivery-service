import test from 'node:test';
import assert from 'node:assert/strict';
import { beginScenario, advanceScenario, applyScenario } from '../app/web/assets/scenarios.mjs';

test('retry recovery preserves one event and clears failure metadata on arrival', () => {
  let run = beginScenario('retry', 1);
  let records = applyScenario([], run);
  run = advanceScenario(run);
  records = applyScenario(records, run);
  assert.equal(records[0].status, 'RETRYING');
  assert.ok(records[0].next_retry_time);
  assert.ok(records[0].last_error);
  run = advanceScenario(run);
  records = applyScenario(records, run);
  assert.equal(records.length, 1);
  assert.equal(records[0].status, 'DELIVERED');
  assert.equal(records[0].attempt_count, 2);
  assert.equal(records[0].last_error, null);
  assert.equal(records[0].next_retry_time, null);
  assert.ok(records[0].final_delivery_time);
  assert.deepEqual(advanceScenario(run), run);
});

test('duplicate ingestion cannot overwrite an independently delivered original', () => {
  let run = beginScenario('duplicate', 2);
  const records = applyScenario([], run);
  records[0].status = 'DELIVERED';
  records[0].attempt_count = 1;
  run = advanceScenario(run);
  assert.equal(applyScenario(records, run), records);
  assert.equal(records.length, 1);
  assert.equal(records[0].status, 'DELIVERED');
  assert.equal(records[0].attempt_count, 1);
});

test('exhausted attempts preserve a dead event without future retry or arrival', () => {
  let run = beginScenario('dead', 3);
  let records = applyScenario([], run);
  while (!run.finished) {
    run = advanceScenario(run);
    records = applyScenario(records, run);
  }
  assert.equal(records.length, 1);
  assert.equal(records[0].status, 'DEAD');
  assert.equal(records[0].attempt_count, 5);
  assert.equal(records[0].next_retry_time, null);
  assert.equal(records[0].final_delivery_time, null);
});

test('acceptance is pending, and success requires a receiver attempt', () => {
  const run = beginScenario('success', 4);
  const pending = applyScenario([], run);
  assert.equal(pending[0].status, 'PENDING');
  assert.equal(pending[0].attempt_count, 0);
  assert.equal(pending[0].final_delivery_time, null);
  const arrived = applyScenario(pending, advanceScenario(run));
  assert.equal(arrived[0].status, 'DELIVERED');
  assert.equal(arrived[0].attempt_count, 1);
  assert.ok(arrived[0].final_delivery_time);
});
