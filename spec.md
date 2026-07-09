# Build Spec — Webhook Delivery Service (Scaffold Only)

> Paste this whole document into your AI build tool (Claude Code, etc.). It tells the AI to construct a **working skeleton** of the repo and to **stub** the three security/reliability-critical modules so I implement them myself.

---

## 0. Role for the AI

You are scaffolding a production-style **webhook delivery service** in FastAPI. Build the full repo structure, all boilerplate, Docker, CI, and tests. **Do NOT implement the three critical modules listed in Section 2** — stub them exactly as specified so the owner writes the logic. The skeleton must boot, the health endpoint must work, and CI must pass green even with the stubs in place.

---

## 1. Tech stack (fixed — do not substitute)

- **API:** FastAPI + Uvicorn
- **Validation/config:** Pydantic v2 + pydantic-settings
- **Queue / state:** Redis (local container for dev; managed e.g. Upstash for prod)
- **Worker:** standalone Python process consuming the queue
- **Container:** Docker + Docker Compose (api + redis + worker)
- **CI/CD:** GitHub Actions
- **Tests:** pytest + httpx

---

## 2. ⛔ BUILD vs STUB — the most important rule

**STUB these three** (define the interface + docstring + `raise NotImplementedError`, nothing more). These are the owner's to implement:

| Module | File | What it will do (DO NOT WRITE THE LOGIC) |
|---|---|---|
| HMAC verification | `app/core/security.py` | Timing-safe signature check on incoming payloads |
| Idempotency / dedup | `app/core/idempotency.py` | Redis-backed duplicate suppression with TTL |
| Retry + DLQ state machine | `app/core/retry.py` | Backoff schedule + decide when a delivery dies to the DLQ |

**BUILD fully** (everything else): app entry + wiring, routes, models, config, Redis client setup, worker loop skeleton (calling into the stubs), health endpoint, Dockerfile, docker-compose, CI YAML, requirements, env example, README, and test skeletons.

> Wiring rule: the webhook route and worker should **call** the stubbed functions. Calling a stub will raise `NotImplementedError` — that's intended. The app must still **import and boot** cleanly, and `/health` must return 200.

---

## 3. Repo structure to generate

```
webhook-delivery-service/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app, route registration, startup/shutdown
│   ├── config.py               # Settings via pydantic-settings (reads env)
│   ├── models.py               # Pydantic models (see §5)
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── webhooks.py         # POST ingest — calls security + idempotency STUBS
│   │   └── health.py           # GET /health — fully working
│   ├── core/
│   │   ├── __init__.py
│   │   ├── security.py         # STUB — HMAC
│   │   ├── idempotency.py      # STUB — dedup
│   │   └── retry.py            # STUB — backoff + DLQ
│   ├── workers/
│   │   ├── __init__.py
│   │   └── delivery_worker.py  # queue consumer loop; calls retry STUB
│   └── storage/
│       ├── __init__.py
│       └── redis_client.py     # Redis connection factory (fully working)
├── tests/
│   ├── __init__.py
│   ├── conftest.py             # fixtures: test client, fake redis if easy
│   ├── test_health.py          # real, passing test
│   ├── test_security.py        # skeleton — see §8
│   ├── test_idempotency.py     # skeleton
│   └── test_retry.py           # skeleton
├── demo/                       # PHASE 2 — optional, see §9
│   └── index.html              # static placeholder only
├── .github/
│   └── workflows/
│       └── ci.yml
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── .gitignore
├── .dockerignore
└── README.md
```

---

## 4. Stub contracts (generate EXACTLY these — signatures + docstrings + NotImplementedError)

**`app/core/security.py`**
```python
def verify_signature(payload_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify the HMAC signature of an incoming webhook.

    TODO(owner): implement timing-safe HMAC-SHA256 comparison.
    - Compute HMAC over the raw request body using `secret`.
    - Compare against `signature_header` using a constant-time check.
    - Return True iff valid; never short-circuit on length.
    """
    raise NotImplementedError("HMAC verification not yet implemented")
```

**`app/core/idempotency.py`**
```python
def is_duplicate(idempotency_key: str) -> bool:
    """Return True if this webhook has already been seen.

    TODO(owner): implement Redis-backed dedup (e.g. SET NX) with a TTL window.
    """
    raise NotImplementedError("Idempotency check not yet implemented")


def mark_processed(idempotency_key: str, ttl_seconds: int) -> None:
    """Record that a webhook id has been handled, expiring after ttl_seconds.

    TODO(owner): set the key in Redis with the given TTL.
    """
    raise NotImplementedError("mark_processed not yet implemented")
```

**`app/core/retry.py`**
```python
def compute_backoff(attempt: int) -> float:
    """Return delay in seconds before the next retry for a given attempt number.

    TODO(owner): exponential backoff (optionally with jitter).
    """
    raise NotImplementedError("Backoff schedule not yet implemented")


def should_move_to_dlq(attempt: int, max_attempts: int) -> bool:
    """Return True when a delivery has exhausted its retries and must go to the DLQ.

    TODO(owner): implement the terminal condition.
    """
    raise NotImplementedError("DLQ decision not yet implemented")
```

---

## 5. Models (`app/models.py`) — build fully

- `IncomingWebhook`: raw inbound shape (id / event type / payload dict / timestamp). The `id` doubles as the idempotency key.
- `DeliveryAttempt`: target URL, attempt number, status, last error, next-retry-at.
- `DeliveryStatus` enum: `PENDING`, `DELIVERED`, `RETRYING`, `DEAD`.
- Keep them minimal but typed; add sensible defaults.

---

## 6. Routes — build the wiring, leave critical calls pointing at stubs

**`routes/webhooks.py`** (`POST /webhooks`):
1. Read the **raw body** (needed for HMAC — don't pre-parse it away).
2. Call `verify_signature(...)` → on failure return `401`. *(Will raise NotImplementedError until implemented — fine.)*
3. Call `is_duplicate(...)` → if true return `200` with a "duplicate ignored" note.
4. Enqueue the delivery job to Redis; call `mark_processed(...)`.
5. Return `202 Accepted`.

**`routes/health.py`** (`GET /health`): return `{"status": "ok"}` with `200`. Fully working, no stubs.

---

## 7. Worker (`workers/delivery_worker.py`) — skeleton that calls the retry stub

- Loop: pull a job from the Redis queue → attempt HTTP delivery to the target URL → on failure consult `compute_backoff(...)` and `should_move_to_dlq(...)` → either requeue with delay or push to the DLQ.
- The delivery loop structure and Redis interaction are yours to build; the **decision functions stay stubbed**.
- Make it runnable as `python -m app.workers.delivery_worker`.

---

## 8. Tests — one real test, three green skeletons

- `test_health.py`: **real**, asserts `/health` → 200.
- `test_security.py`, `test_idempotency.py`, `test_retry.py`: write the test **structure** with clear `# TODO(owner): assert ...` bodies, and mark each with `@pytest.mark.skip(reason="awaiting implementation")` (or `xfail`) so **CI stays green on the skeleton**. Include the obvious cases as TODO comments (valid sig, tampered sig, duplicate suppressed, backoff grows, DLQ after max attempts).

---

## 9. Docker, Compose, CI, config — build fully

**`Dockerfile`**: slim Python base, install `requirements.txt`, copy app, run Uvicorn. Non-root user. `.dockerignore` excludes `.venv`, `__pycache__`, `.git`, tests if you prefer.

**`docker-compose.yml`**: three services — `api` (build, expose port, depends_on redis), `redis` (official image), `worker` (same build/image, command runs the worker). Shared env via `.env`. `api` must come up clean with `docker compose up`.

**`.env.example`**: `REDIS_URL`, `WEBHOOK_SECRET`, `MAX_RETRY_ATTEMPTS`, `IDEMPOTENCY_TTL_SECONDS`, `PORT`. No real secrets.

**`.github/workflows/ci.yml`**: on push/PR → set up Python → install deps → run `pytest` (must pass with skipped stub tests) → build the Docker image. Add a **commented `deploy` job placeholder** (`# TODO(owner): wire deploy to <host>`), since I'll pick the host. Note in a comment that prod Redis = managed (e.g. Upstash) rather than the compose container.

**`requirements.txt`**: fastapi, uvicorn[standard], pydantic, pydantic-settings, redis, httpx, pytest. Pin reasonable versions.

---

## 10. README.md — build a strong skeleton

Generate with these sections (fill placeholders I'll complete):
- **Title + one-line pitch.**
- **🔗 Live demo: `<PLACEHOLDER URL>`** — at the very top.
- **Architecture diagram** — a Mermaid diagram of: client → API (HMAC verify → dedup → enqueue) → Redis queue → worker (deliver → backoff/retry → DLQ).
- **The hard problems** (call these out explicitly as the differentiator): HMAC verification, idempotency/dedup, exponential backoff, dead-letter queue.
- **Local setup**: `docker compose up`, env vars, how to send a test webhook (curl example).
- **Tech stack** and **project layout**.

---

## 11. ⛔ What NOT to do

- Do **not** implement `security.py`, `idempotency.py`, or `retry.py` logic. Stubs only.
- Do **not** fill in the assertion bodies of the three stub test files.
- Do **not** swap the stack (no Flask, no Celery unless I ask, no SQL DB).
- Do **not** invent a deploy target — leave it as a commented placeholder.
- Keep `demo/index.html` a static placeholder; it's phase 2 and must not block core boot.

---

## 12. Done means

- `docker compose up` boots; `GET /health` → 200.
- `pytest` passes (health real; three stub suites skipped).
- CI workflow is valid and green.
- The three critical modules exist as clean, documented stubs awaiting my implementation.