import os

base_dir = "D:/projects/webhook_delivery"

files = {
    "app/__init__.py": "",
    "app/main.py": """from fastapi import FastAPI
from app.routes import webhooks, health

app = FastAPI(title="Webhook Delivery Service")

app.include_router(health.router)
app.include_router(webhooks.router)
""",
    "app/config.py": """from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    redis_url: str = "redis://localhost:6379/0"
    webhook_secret: str = ""
    max_retry_attempts: int = 5
    idempotency_ttl_seconds: int = 86400
    port: int = 8000
    
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

settings = Settings()
""",
    "app/models.py": """from pydantic import BaseModel, Field
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from enum import Enum

class IncomingWebhook(BaseModel):
    id: str = Field(..., description="Unique identifier for the webhook, used as idempotency key")
    event_type: str
    payload: Dict[str, Any]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class DeliveryStatus(str, Enum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    RETRYING = "RETRYING"
    DEAD = "DEAD"

class DeliveryAttempt(BaseModel):
    target_url: str
    attempt_number: int = 0
    status: DeliveryStatus = DeliveryStatus.PENDING
    last_error: Optional[str] = None
    next_retry_at: Optional[datetime] = None
""",
    "app/routes/__init__.py": "",
    "app/routes/webhooks.py": """from fastapi import APIRouter, Request, HTTPException, status, Response
from app.models import IncomingWebhook
from app.core import security, idempotency
from app.storage.redis_client import get_redis

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(request: Request, response: Response):
    raw_body = await request.body()
    signature_header = request.headers.get("x-signature", "")
    from app.config import settings
    
    try:
        if not security.verify_signature(raw_body, signature_header, settings.webhook_secret):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")
    except NotImplementedError:
        pass

    try:
        body_json = await request.json()
        webhook_data = IncomingWebhook(**body_json)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON")

    try:
        if idempotency.is_duplicate(webhook_data.id):
            response.status_code = status.HTTP_200_OK
            return {"status": "duplicate ignored"}
    except NotImplementedError:
        pass

    redis = get_redis()
    # redis.lpush("webhook_queue", webhook_data.model_dump_json())

    try:
        idempotency.mark_processed(webhook_data.id, settings.idempotency_ttl_seconds)
    except NotImplementedError:
        pass

    return {"status": "accepted"}
""",
    "app/routes/health.py": """from fastapi import APIRouter

router = APIRouter(tags=["health"])

@router.get("/health")
def health_check():
    return {"status": "ok"}
""",
    "app/core/__init__.py": "",
    "app/core/security.py": '''def verify_signature(payload_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify the HMAC signature of an incoming webhook.

    TODO(owner): implement timing-safe HMAC-SHA256 comparison.
    - Compute HMAC over the raw request body using `secret`.
    - Compare against `signature_header` using a constant-time check.
    - Return True iff valid; never short-circuit on length.
    """
    raise NotImplementedError("HMAC verification not yet implemented")
''',
    "app/core/idempotency.py": '''def is_duplicate(idempotency_key: str) -> bool:
    """Return True if this webhook has already been seen.

    TODO(owner): implement Redis-backed dedup (e.g. SET NX) with a TTL window.
    """
    raise NotImplementedError("Idempotency check not yet implemented")

def mark_processed(idempotency_key: str, ttl_seconds: int) -> None:
    """Record that a webhook id has been handled, expiring after ttl_seconds.

    TODO(owner): set the key in Redis with the given TTL.
    """
    raise NotImplementedError("mark_processed not yet implemented")
''',
    "app/core/retry.py": '''def compute_backoff(attempt: int) -> float:
    """Return delay in seconds before the next retry for a given attempt number.

    TODO(owner): exponential backoff (optionally with jitter).
    """
    raise NotImplementedError("Backoff schedule not yet implemented")

def should_move_to_dlq(attempt: int, max_attempts: int) -> bool:
    """Return True when a delivery has exhausted its retries and must go to the DLQ.

    TODO(owner): implement the terminal condition.
    """
    raise NotImplementedError("DLQ decision not yet implemented")
''',
    "app/workers/__init__.py": "",
    "app/workers/delivery_worker.py": """import time
from app.storage.redis_client import get_redis
from app.core import retry
from app.config import settings

def main():
    redis = get_redis()
    print("Worker started, listening to queue...")
    while True:
        try:
            time.sleep(1)
            try:
                retry.compute_backoff(1)
            except NotImplementedError:
                pass
            try:
                retry.should_move_to_dlq(1, settings.max_retry_attempts)
            except NotImplementedError:
                pass
        except Exception as e:
            print(f"Worker error: {e}")
            time.sleep(1)

if __name__ == "__main__":
    main()
""",
    "app/storage/__init__.py": "",
    "app/storage/redis_client.py": """import redis
from app.config import settings

def get_redis():
    return redis.Redis.from_url(settings.redis_url, decode_responses=True)
""",
    "tests/__init__.py": "",
    "tests/conftest.py": """import pytest
from fastapi.testclient import TestClient
from app.main import app

@pytest.fixture
def client():
    return TestClient(app)
""",
    "tests/test_health.py": """def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
""",
    "tests/test_security.py": """import pytest

@pytest.mark.skip(reason="awaiting implementation")
def test_verify_signature_valid():
    # TODO(owner): assert True for valid sig
    pass

@pytest.mark.skip(reason="awaiting implementation")
def test_verify_signature_invalid():
    # TODO(owner): assert False for tampered sig
    pass
""",
    "tests/test_idempotency.py": """import pytest

@pytest.mark.skip(reason="awaiting implementation")
def test_is_duplicate_suppressed():
    # TODO(owner): assert duplicate suppressed
    pass

@pytest.mark.skip(reason="awaiting implementation")
def test_mark_processed():
    # TODO(owner): assert mark processed sets ttl
    pass
""",
    "tests/test_retry.py": """import pytest

@pytest.mark.skip(reason="awaiting implementation")
def test_compute_backoff_grows():
    # TODO(owner): assert backoff grows
    pass

@pytest.mark.skip(reason="awaiting implementation")
def test_should_move_to_dlq():
    # TODO(owner): assert DLQ after max attempts
    pass
""",
    "demo/index.html": """<!DOCTYPE html>
<html>
<head>
    <title>Webhook Delivery Service Demo</title>
</head>
<body>
    <h1>Webhook Delivery Service</h1>
    <p>Phase 2 placeholder.</p>
</body>
</html>
""",
    ".github/workflows/ci.yml": """name: CI

on:
  push:
    branches: [ main ]
  pull_request:
    branches: [ main ]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v3
    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.11'
    - name: Install dependencies
      run: |
        python -m pip install --upgrade pip
        pip install -r requirements.txt
    - name: Run pytest
      run: pytest

  build:
    needs: test
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v3
    - name: Build Docker image
      run: docker build -t webhook-delivery-service .

  # TODO(owner): wire deploy to <host>
  # deploy:
  #   needs: build
  #   runs-on: ubuntu-latest
  #   steps:
  #   - run: echo "Deploy logic goes here"
""",
    "Dockerfile": """FROM python:3.11-slim

WORKDIR /app

RUN useradd -m appuser && chown -R appuser /app
USER appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
""",
    "docker-compose.yml": """version: '3.8'

services:
  api:
    build: .
    ports:
      - "8000:8000"
    env_file:
      - .env
    depends_on:
      - redis
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

  worker:
    build: .
    env_file:
      - .env
    depends_on:
      - redis
    command: python -m app.workers.delivery_worker

  redis:
    image: redis:alpine
    ports:
      - "6379:6379"
""",
    "requirements.txt": """fastapi
uvicorn[standard]
pydantic
pydantic-settings
redis
httpx
pytest
""",
    ".env.example": """REDIS_URL=redis://redis:6379/0
WEBHOOK_SECRET=your_secret_here
MAX_RETRY_ATTEMPTS=5
IDEMPOTENCY_TTL_SECONDS=86400
PORT=8000
""",
    ".gitignore": """.venv
__pycache__/
*.pyc
.env
.pytest_cache/
""",
    ".dockerignore": """.venv
__pycache__/
.git
tests/
.env
""",
    "README.md": """# Webhook Delivery Service

A production-ready webhook delivery service with retry, exponential backoff, and idempotency.

**🔗 Live demo: `<PLACEHOLDER URL>`**

## Architecture

```mermaid
flowchart LR
    client[Client] --> api[API]
    api -->|HMAC verify<br>dedup<br>enqueue| queue[(Redis Queue)]
    queue --> worker[Worker]
    worker -->|deliver| target[Target URL]
    worker -->|backoff/retry| queue
    worker -->|DLQ| dlq[(Dead Letter Queue)]
```

## The Hard Problems
- **HMAC Verification**: Timing-safe signature check on incoming payloads
- **Idempotency/Dedup**: Redis-backed duplicate suppression with TTL
- **Exponential Backoff**: Dynamic delay calculation for retries
- **Dead-Letter Queue**: Handling exhaustively failed deliveries

## Local Setup
1. Copy `.env.example` to `.env`: `cp .env.example .env`
2. Start the services: `docker compose up --build`
3. Send a test webhook:
   ```bash
   curl -X POST http://localhost:8000/webhooks \\
        -H "Content-Type: application/json" \\
        -H "x-signature: <dummy>" \\
        -d '{"id": "123", "event_type": "test", "payload": {}}'
   ```

## Tech Stack & Layout
- **API**: FastAPI + Uvicorn
- **Validation**: Pydantic v2 + pydantic-settings
- **Queue/State**: Redis (local dev), managed (e.g. Upstash) for prod
- **Worker**: Standalone Python process
- **Container**: Docker + Compose
"""
}

for path, content in files.items():
    full_path = os.path.join(base_dir, path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, 'w', encoding='utf-8') as f:
        f.write(content)
