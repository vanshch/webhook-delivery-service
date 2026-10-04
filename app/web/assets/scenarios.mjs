// UI-only fixtures. Never fetch, sign, or send a real webhook from this module.
const startTime = Date.parse('2026-10-04T06:00:00Z');
const timestamp = (seconds) => new Date(startTime + seconds * 1000).toISOString();

export function deliveryState(eventId, status = 'PENDING', attempts = 0, seconds = 0) {
  return {
    event_id: eventId, status, attempt_count: attempts,
    last_attempt_time: attempts ? timestamp(seconds) : null,
    last_error: status === 'DEAD' ? `Delivery failed after ${attempts} attempts`
      : status === 'RETRYING' ? `Delivery attempt ${attempts} failed` : null,
    next_retry_time: status === 'RETRYING' ? timestamp(seconds + 3) : null,
    final_delivery_time: status === 'DELIVERED' ? timestamp(seconds) : null,
  };
}

export const scenarios = {
  success: [
    { label: 'Accept', title: 'Signature verified; job queued', description: 'The API atomically claims the event ID and enqueues a delivery. Acceptance is not yet confirmation of delivery.', response: '202 {"status":"accepted"}', status: 'PENDING', attempts: 0, seconds: 0 },
    { label: 'Deliver', title: 'Receiver accepts the event', description: 'The worker signs the exact request bytes, sends them, then records DELIVERED and acknowledges the stream entry.', status: 'DELIVERED', attempts: 1, seconds: 1 },
  ],
  retry: [
    { label: 'Accept', title: 'Event accepted', description: 'A delivery job exists in the Redis Stream and is awaiting a worker.', response: '202 {"status":"accepted"}', status: 'PENDING', attempts: 0, seconds: 0 },
    { label: 'Fail', title: 'First delivery attempt fails', description: 'In this example the receiver returns 503. Retry state is persisted before acknowledging the current stream message. Backoff includes jitter; these example timestamps are illustrative.', status: 'RETRYING', attempts: 1, seconds: 1 },
    { label: 'Recover', title: 'Retry reaches the recovered receiver', description: 'The delayed job returns to the stream and succeeds on attempt 2. Retry scheduling clears from the latest state.', status: 'DELIVERED', attempts: 2, seconds: 4 },
  ],
  duplicate: [
    { label: 'Accept', title: 'First request creates one job', description: 'The event ID is claimed for the configured idempotency window (24 hours by default).', response: '202 {"status":"accepted"}', status: 'PENDING', attempts: 0, seconds: 0 },
    { label: 'Repeat ID', title: 'Duplicate submission ignored', description: 'The same ID returns a duplicate response without creating another job or resetting the original delivery state. DUPLICATE is not a delivery status.', response: '200 {"status":"duplicate ignored"}', status: 'PENDING', attempts: 0, seconds: 0, duplicate: true },
    { label: 'Deliver', title: 'Original event is delivered', description: 'Only the original job proceeds. Receivers still need idempotency because delivery itself is at-least-once.', status: 'DELIVERED', attempts: 1, seconds: 1 },
  ],
  dead: [
    { label: 'Accept', title: 'Event accepted', description: 'The event starts as PENDING.', response: '202 {"status":"accepted"}', status: 'PENDING', attempts: 0, seconds: 0 },
    { label: 'Retry', title: 'Delivery fails and is scheduled again', description: 'This example skips the intermediate attempts to keep the walkthrough short. The default allows 5 total failed attempts before DLQ routing.', status: 'RETRYING', attempts: 1, seconds: 1 },
    { label: 'Dead-letter', title: 'Attempts exhausted; event preserved in DLQ', description: 'The final failed attempt records DEAD and safely moves the event to the dead-letter queue. An operator can inspect and replay it with the protected CLI.', status: 'DEAD', attempts: 5, seconds: 40 },
  ],
};

export function beginScenario(kind, sequence) {
  if (!Object.hasOwn(scenarios, kind)) throw new Error('Unknown scenario');
  return { kind, index: 0, eventId: `sample-${kind}-${sequence}`, finished: false };
}

export function advanceScenario(run) {
  const steps = scenarios[run.kind];
  const index = Math.min(run.index + 1, steps.length - 1);
  return { ...run, index, finished: index === steps.length - 1 };
}

export function applyScenario(records, run) {
  const step = scenarios[run.kind][run.index];
  // A duplicate response leaves even an independently updated original state intact.
  if (step.duplicate) return records;
  const state = deliveryState(run.eventId, step.status, step.attempts, step.seconds);
  return [state, ...records.filter((record) => record.event_id !== run.eventId)];
}
