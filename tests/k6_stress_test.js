import http from 'k6/http';
import crypto from 'k6/crypto';
import { check, sleep } from 'k6';
import { Trend } from 'k6/metrics';

// The PowerShell runner exports the same development-only secret to the API and k6.
const SECRET = __ENV.WEBHOOK_SECRET || 'local-stress-secret';
const API_URL = __ENV.API_URL || 'http://localhost:8000/webhooks';
const payloadBytes = new Trend('payload_bytes');

export const options = {
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  stages: [
    { duration: '5s', target: 50 },   // Ramp-up to 50 virtual users (VUs)
    { duration: '20s', target: 50 },  // Stay at 50 VUs (sustained load)
    { duration: '5s', target: 0 },    // Ramp-down
  ],
  thresholds: {
    http_req_duration: ['p(95)<100'], // 95% of ingestion requests must complete below 100ms
    http_req_failed: ['rate==0'],
    checks: ['rate==1'],
  },
};

export default function () {
  // Create a unique id for idempotency
  const id = `k6-${__VU}-${__ITER}-${Date.now()}-${Math.floor(Math.random() * 1000000)}`;
  
  const payload = {
    id: id,
    event_type: 'k6.test.event',
    // target_url is intentionally omitted: this benchmark measures ingestion and enqueueing only.
    payload: {
      message: 'Hello from k6 load test!',
      index: __ITER,
      test_timestamp: new Date().toISOString(),
    },
  };

  const payloadString = JSON.stringify(payload);
  // The generated payload is ASCII-only, so JavaScript character length equals UTF-8 bytes.
  payloadBytes.add(payloadString.length);
  const signature = crypto.hmac('sha256', SECRET, payloadString, 'hex');

  const params = {
    headers: {
      'Content-Type': 'application/json',
      'x-signature': `sha256=${signature}`,
    },
  };

  const res = http.post(API_URL, payloadString, params);

  check(res, {
    'status is 202': (r) => r.status === 202,
  });

  sleep(0.02); // 20ms pacing delay between iterations for each VU
}
