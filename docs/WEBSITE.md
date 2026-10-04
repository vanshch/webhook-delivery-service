# Public project page

The public entry point is https://api.webhookdelivery.dev/. FastAPI serves `app/web/index.html`, four explicitly named assets under `/assets/web/`, and two historical JSON reports under `/evidence/soak` and `/evidence/benchmark`. The API, Caddy routing, authentication, worker, queue, and delivery guarantees keep their existing contracts. The image already copies the page and evidence; no JavaScript dependency, build step, additional service or CORS policy is needed.

## Visitor flow

1. Understand the signed-ingest → durable queue → worker → receiver path.
2. Step through success, retry recovery, duplicate suppression and exhausted attempts in a clearly labelled simulation.
3. Check live readiness or inspect a known event ID using the existing API.
4. Open historical engineering evidence, source code or API docs.

The simulation sends no events. Browser requests are limited to same-origin GETs; no credentials or browser storage are used. Readiness runs once on load and on an explicit refresh, with an eight-second timeout. A failed check renders unknown rather than assuming health. Lookup is explicit, validates the existing 1–128 character ID contract and renders API fields as text. A 404 is described as unknown or expired, rather than a failed delivery. There is no global event list, live throughput claim or public replay control.

## Serving boundary

The page applies a restrictive self-origin Content Security Policy, blocks embedding, disables device permissions, and supplies referrer and MIME protections. Asset MIME types are explicit across Windows and Linux. Page and assets use revalidation to avoid stale modules after a release. File routes use fixed allowlists; they cannot serve project configuration, local handoff files, credentials, raw experiment logs or arbitrary reports. API docs retain their own existing policy.

The two evidence cards identify historical test conditions and distinguish local ingestion from production delivery capacity. The active hosting provider is not part of the frontend contract. Original prototypes under `demo/dashboard` and ignored comparison artifacts are preserved separately.

## Verification

```powershell
.venv/Scripts/python.exe -m pytest tests/ -q
node --test tests/website_scenarios.test.mjs
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8792
```

Open http://127.0.0.1:8792/. Local readiness may report unavailable when production dependencies are absent; the page and simulation work independently of Redis.

Session browser QA uses locally installed Playwright and Axe libraries (no MCP/plugin or skill) to check all four scenarios, safe rendering, lookup/error states, readiness failures, keyboard navigation, reduced motion, desktop/mobile accessibility and overflow at 1440, 390 and 320 pixels. Ignored screenshots/report are under `tools/frontend-review/integrated`. Production verification additionally requires trusted HTTPS, live readiness, a known-ID lookup and the repository's full signed smoke test.
