import { scenarios, applyScenario } from './scenarios.mjs';

const $ = (id) => document.getElementById(id);
const ids = { success: 'sample-order-1001', retry: 'sample-retry-1002', duplicate: 'sample-duplicate-1003', dead: 'sample-failure-1004' };
const actions = { PENDING: 'Waiting for a worker', RETRYING: 'Retry scheduled with backoff', DELIVERED: 'Acknowledged; delivery complete', DEAD: 'Preserved for operator recovery' };
const nextLabels = { success: ['Deliver event'], retry: ['Attempt delivery', 'Retry delivery'], duplicate: ['Submit same ID', 'Deliver original'], dead: ['Attempt delivery', 'Exhaust attempts'] };
let kind = 'success';
let index = 0;
let sample;

function chip(element, value) {
  element.textContent = value;
  element.className = `status-chip ${['PENDING', 'RETRYING', 'DELIVERED', 'DEAD'].includes(value) ? value.toLowerCase() : ''}`;
}

function drawScenario() {
  const steps = scenarios[kind];
  const step = steps[index];
  // Duplicate requests must not replace or reset the original delivery state.
  sample = applyScenario(sample ? [sample] : [], { kind, index, eventId: ids[kind] })[0];
  document.querySelectorAll('[data-scenario]').forEach((button) => {
    button.setAttribute('aria-pressed', String(button.dataset.scenario === kind));
  });
  $('trace').replaceChildren(...steps.map((item, position) => {
    const row = document.createElement('li');
    row.className = position < index ? 'complete' : position === index ? 'current' : 'upcoming';
    if (position === index) row.setAttribute('aria-current', 'step');
    const marker = document.createElement('span');
    marker.className = 'trace-marker';
    marker.setAttribute('aria-hidden', 'true');
    marker.textContent = position < index ? '✓' : String(position + 1).padStart(2, '0');
    const title = document.createElement('h3');
    title.textContent = item.title;
    const description = document.createElement('p');
    description.textContent = item.description;
    row.append(marker, title, description);
    return row;
  }));
  $('step-count').textContent = `STEP ${String(index + 1).padStart(2, '0')} / ${String(steps.length).padStart(2, '0')}`;
  chip($('sample-status'), sample.status);
  $('sample-id').textContent = sample.event_id;
  $('sample-attempts').textContent = String(sample.attempt_count);
  $('sample-response').textContent = step.duplicate ? '200 · Duplicate ignored' : sample.status === 'DELIVERED' ? '2xx · Receiver accepted' : ['RETRYING', 'DEAD'].includes(sample.status) ? '503 · Receiver unavailable' : '202 · Accepted';
  $('sample-action').textContent = actions[sample.status];
  $('sample-json').textContent = JSON.stringify(sample, null, 2);
  const finished = index === steps.length - 1;
  $('next-step').disabled = finished;
  $('next-step').textContent = finished ? 'Scenario complete ✓' : `${nextLabels[kind][index]} →`;
  $('scenario-note').textContent = step.duplicate ? 'Same ID. No second job. The original state stays intact.' : finished ? kind === 'dead' ? 'DEAD preserves the event; replay requires the protected CLI.' : 'Delivered is recorded after the receiver accepts the request.' : 'Acceptance creates a job. It does not confirm arrival.';
}

document.querySelectorAll('[data-scenario]').forEach((button) => button.addEventListener('click', () => {
  kind = button.dataset.scenario;
  index = 0;
  drawScenario();
}));
$('next-step').addEventListener('click', () => {
  index = Math.min(index + 1, scenarios[kind].length - 1);
  drawScenario();
});
$('reset').addEventListener('click', () => { index = 0; drawScenario(); });
$('scenario-note').setAttribute('role', 'status');
drawScenario();

async function getJSON(path) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(path, { signal: controller.signal, cache: 'no-store', credentials: 'omit', headers: { Accept: 'application/json' } });
    const body = await response.json();
    return { response, body };
  } finally {
    clearTimeout(timer);
  }
}

async function checkReadiness() {
  const button = $('refresh-health');
  if (button.disabled) return;
  button.disabled = true;
  button.textContent = 'Checking…';
  $('health-badge').textContent = 'Checking…';
  $('health-badge').className = 'health-badge neutral';
  $('redis-state').textContent = 'Checking…';
  $('worker-state').textContent = 'Checking…';
  $('health-message').textContent = 'Requesting /readyz…';
  try {
    const { response, body } = await getJSON('/readyz');
    if (![200, 503].includes(response.status) || !['ok', 'unhealthy'].includes(body.status)) throw new Error('Invalid readiness response');
    const healthy = response.ok && body.status === 'ok' && body.redis === 'connected' && body.worker_heartbeat === 'healthy';
    $('health-badge').className = `health-badge ${healthy ? 'healthy' : 'unhealthy'}`;
    $('health-badge').textContent = healthy ? 'Ready to process' : 'Not ready';
    $('redis-state').textContent = body.redis === 'connected' ? 'Connected' : body.redis === 'unavailable' ? 'Unavailable' : 'Not reported';
    $('worker-state').textContent = body.worker_heartbeat === 'healthy' ? 'Healthy' : body.worker_heartbeat === 'stale' ? 'Stale' : 'Not checked';
    $('health-message').textContent = `Live check at ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })} · HTTP ${response.status}`;
  } catch {
    $('health-badge').className = 'health-badge neutral';
    $('health-badge').textContent = 'Check unavailable';
    $('redis-state').textContent = 'Unknown';
    $('worker-state').textContent = 'Unknown';
    $('health-message').textContent = 'Could not check readiness. Try refreshing; no healthy result is assumed.';
  } finally {
    button.disabled = false;
    button.textContent = 'Refresh ↻';
  }
}
$('refresh-health').addEventListener('click', checkReadiness);
checkReadiness();

const formatTime = (value) => {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : `${date.toLocaleString()} (local time)`;
};

$('lookup-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('lookup-button');
  if (button.disabled) return;
  const eventId = $('event-id').value.trim();
  $('lookup-result').hidden = true;
  if (!/^[A-Za-z0-9_:-]{1,128}$/.test(eventId)) {
    $('lookup-message').textContent = 'Use 1–128 letters, numbers, underscores, hyphens or colons.';
    return;
  }
  button.disabled = true;
  button.textContent = 'Looking…';
  $('lookup-message').textContent = 'Fetching current delivery state…';
  try {
    const { response, body } = await getJSON(`/deliveries/${encodeURIComponent(eventId)}`);
    if (response.status === 404) {
      $('lookup-message').textContent = 'No retained status found. This ID may be unknown or expired; a 404 does not prove delivery failed.';
      return;
    }
    if (!response.ok) {
      $('lookup-message').textContent = response.status === 400 ? 'The API rejected this event ID.' : 'The service could not complete this lookup. Try again.';
      return;
    }
    if (body.event_id !== eventId || !Object.hasOwn(actions, body.status) || !Number.isInteger(body.attempt_count)) throw new Error('Invalid delivery response');
    $('live-event-id').textContent = body.event_id;
    chip($('live-status'), body.status);
    const fields = [['Attempts', body.attempt_count], ['Last attempt', formatTime(body.last_attempt_time)], ['Next retry', formatTime(body.next_retry_time)], ['Delivered at', formatTime(body.final_delivery_time)], ['Last error', body.last_error ?? '—']];
    $('live-fields').replaceChildren(...fields.map(([name, value]) => {
      const row = document.createElement('div');
      const label = document.createElement('dt');
      const detail = document.createElement('dd');
      label.textContent = name;
      detail.textContent = String(value);
      row.append(label, detail);
      return row;
    }));
    $('live-json').textContent = JSON.stringify(body, null, 2);
    $('lookup-result').hidden = false;
    $('lookup-message').textContent = 'Live delivery state retrieved. Displayed timestamps use your local timezone.';
  } catch {
    $('lookup-message').textContent = 'Could not retrieve delivery state. Check the connection and try again.';
  } finally {
    button.disabled = false;
    button.textContent = 'Inspect →';
  }
});
