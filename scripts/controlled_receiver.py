"""Temporary, authenticated receiver used by the live deployment smoke test."""

import argparse
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request, status


app = FastAPI(title="Controlled Webhook Receiver")
received_events: list[dict[str, Any]] = []
fail_next_requests = 0
fail_event_ids: set[str] = set()


def validate_runtime_config() -> None:
    """Fail startup on missing, short, or reused receiver secrets."""
    outbound = os.getenv("OUTBOUND_WEBHOOK_SECRET", "")
    control = os.getenv("SMOKE_CONTROL_SECRET", "")
    for name, value in (
        ("OUTBOUND_WEBHOOK_SECRET", outbound),
        ("SMOKE_CONTROL_SECRET", control),
    ):
        if len(value.strip()) < 32:
            raise RuntimeError(f"{name} must contain at least 32 characters")
    if hmac.compare_digest(outbound, control):
        raise RuntimeError("Receiver HMAC and control secrets must be distinct")


def verify_hmac(body: bytes, signature_header: str | None, secret: str) -> bool:
    """Return whether the header authenticates the exact raw body bytes."""
    if not secret or not signature_header:
        return False
    supplied = signature_header.removeprefix("sha256=")
    if supplied == signature_header:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied)


def require_control_auth(authorization: str | None = Header(default=None)) -> None:
    """Protect receiver state and failure controls with a separate bearer secret."""
    secret = os.getenv("SMOKE_CONTROL_SECRET", "")
    prefix = "Bearer "
    if not secret or not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    if not hmac.compare_digest(authorization[len(prefix) :], secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


@app.post("/webhook")
async def receive_webhook(request: Request):
    """Reject unauthenticated deliveries and record successful deliveries."""
    global fail_next_requests

    body = await request.body()
    signature = request.headers.get("x-signature")
    secret = os.getenv("OUTBOUND_WEBHOOK_SECRET", "")
    if not verify_hmac(body, signature, secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON") from exc

    event_id = payload.get("id")
    if not isinstance(event_id, str) or not event_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing event ID")

    if fail_next_requests > 0:
        fail_next_requests -= 1
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Simulated temporary failure",
        )
    if event_id in fail_event_ids:
        fail_event_ids.remove(event_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Simulated temporary failure",
        )

    event_record = {
        "id": event_id,
        "payload": payload,
        "valid_signature": True,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    received_events.append(event_record)
    return {"status": "accepted", "event_id": event_id, "valid_signature": True}


@app.get("/events")
def get_events(authorization: str | None = Header(default=None)):
    require_control_auth(authorization)
    return {"events": received_events}


@app.get("/events/{event_id}")
def get_event(event_id: str, authorization: str | None = Header(default=None)):
    require_control_auth(authorization)
    matching = [event for event in received_events if event["id"] == event_id]
    if not matching:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    return matching[-1]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/control/fail-next")
def fail_next(
    count: int = Query(default=1, ge=1, le=10),
    authorization: str | None = Header(default=None),
):
    global fail_next_requests
    require_control_auth(authorization)
    fail_next_requests += count
    return {"status": "configured", "fail_next_requests": fail_next_requests}


@app.post("/control/fail-event/{event_id}")
def fail_event(event_id: str, authorization: str | None = Header(default=None)):
    require_control_auth(authorization)
    fail_event_ids.add(event_id)
    return {"status": "configured", "fail_event_id": event_id}


@app.post("/control/reset")
def reset(authorization: str | None = Header(default=None)):
    global fail_next_requests
    require_control_auth(authorization)
    received_events.clear()
    fail_next_requests = 0
    fail_event_ids.clear()
    return {"status": "reset"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Controlled webhook smoke receiver")
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    validate_runtime_config()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
