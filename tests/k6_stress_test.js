import http from 'k6/http';
import crypto from 'k6/crypto';
import { check, sleep } from 'k6';

// Read secret from environment variable or default to the one in .env
const SECRET = __ENV.WEBHOOK_SECRET || 'your_secret_here';
const API_URL = 'http://localhost:8000/webhooks';

export const options = {
  stages: [
    { duration: '5s', target: 50 },   // Ramp-up to 50 virtual users (VUs)
    { duration: '20s', target: 50 },  // Stay at 50 VUs (sustained load)
    { duration: '5s', target: 0 },    // Ramp-down
  ],
  thresholds: {
    http_req_duration: ['p(95)<100'], // 95% of ingestion requests must complete below 100ms
  },
};

export default function () {
  // Create a unique id for idempotency
  const id = `k6-${__VU}-${__ITER}-${Date.now()}-${Math.floor(Math.random() * 1000000)}`;
  
  // Distribute target endpoints (80% success, 20% fail)
  const targetUrl = Math.random() < 0.8 
    ? 'http://localhost:8081/success-endpoint' 
    : 'http://localhost:8081/fail-endpoint';

  const payload = {
    id: id,
    event_type: 'k6.test.event',
    payload: {
      message: 'Hello from k6 load test!',
      index: __ITER,
      test_timestamp: new Date().toISOString(),
    },
    target_url: targetUrl,
  };

  const payloadString = JSON.stringify(payload);
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
