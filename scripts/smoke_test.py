"""Run the public end-to-end deployment smoke test."""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx


def calculate_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _wait_for_status(
    client: httpx.Client,
    api_url: str,
    event_id: str,
    timeout: float,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = client.get(f"{api_url}/deliveries/{event_id}")
            if response.status_code == 200:
                state = response.json()
                if state.get("status") in {"DELIVERED", "DEAD"}:
                    return state
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return None


def run_smoke_tests(
    *,
    api_url: str,
    receiver_url: str,
    webhook_secret: str,
    control_secret: str,
    timeout: float = 60.0,
    require_https: bool = True,
    client: httpx.Client | None = None,
) -> bool:
    """Verify readiness, delivery, HMAC, deduplication, and retry recovery."""
    api_url = api_url.rstrip("/")
    receiver_url = receiver_url.rstrip("/")
    if require_https:
        for name, url in (("API_URL", api_url), ("RECEIVER_URL", receiver_url)):
            if urlparse(url).scheme != "https":
                print(f"FAIL: {name} must use HTTPS for a production smoke test.")
                return False
    if not webhook_secret or not control_secret:
        print("FAIL: WEBHOOK_SECRET and SMOKE_CONTROL_SECRET are required.")
        return False

    owns_client = client is None
    client = client or httpx.Client(timeout=10.0, follow_redirects=False)
    control_headers = {"Authorization": f"Bearer {control_secret}"}

    try:
        print(f"Testing public deployment at {api_url}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = client.get(f"{api_url}/readyz")
                if response.status_code == 200 and response.json().get("status") == "ok":
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        else:
            print("FAIL: readiness did not become healthy.")
            return False

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                receiver_health = client.get(f"{receiver_url}/health")
                if receiver_health.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        else:
            print("FAIL: controlled receiver did not become healthy.")
            return False

        reset_response = client.post(
            f"{receiver_url}/control/reset",
            headers=control_headers,
        )
        if reset_response.status_code != 200:
            print(f"FAIL: receiver reset returned {reset_response.status_code}.")
            return False

        event_id = f"smoke-{uuid.uuid4().hex}"
        payload = {
            "id": event_id,
            "event_type": "smoke.delivery",
            "payload": {"source": "deployment-smoke"},
            "target_url": f"{receiver_url}/webhook",
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Signature": calculate_signature(webhook_secret, body),
        }
        accepted = client.post(f"{api_url}/webhooks", content=body, headers=headers)
        if accepted.status_code != 202:
            print(f"FAIL: ingestion returned {accepted.status_code}: {accepted.text}")
            return False

        delivered = _wait_for_status(client, api_url, event_id, timeout)
        if not delivered or delivered.get("status") != "DELIVERED":
            print("FAIL: initial delivery did not reach DELIVERED.")
            return False

        received = client.get(f"{receiver_url}/events/{event_id}", headers=control_headers)
        if received.status_code != 200 or not received.json().get("valid_signature"):
            print("FAIL: receiver did not validate the exact outbound body HMAC.")
            return False

        initial_attempts = delivered.get("attempt_count")
        duplicate = client.post(f"{api_url}/webhooks", content=body, headers=headers)
        if duplicate.status_code != 200 or duplicate.json().get("status") != "duplicate ignored":
            print(f"FAIL: duplicate response was {duplicate.status_code}: {duplicate.text}")
            return False
        time.sleep(2)
        events = client.get(f"{receiver_url}/events", headers=control_headers)
        if events.status_code != 200:
            print(f"FAIL: receiver event listing returned {events.status_code}.")
            return False
        matching = [
            event
            for event in events.json().get("events", [])
            if event.get("id") == event_id
        ]
        duplicate_state = client.get(f"{api_url}/deliveries/{event_id}")
        if (
            len(matching) != 1
            or duplicate_state.status_code != 200
            or duplicate_state.json().get("attempt_count") != initial_attempts
        ):
            print("FAIL: duplicate request created another delivery attempt.")
            return False

        retry_id = f"smoke-retry-{uuid.uuid4().hex}"
        failure = client.post(
            f"{receiver_url}/control/fail-event/{retry_id}",
            headers=control_headers,
        )
        if failure.status_code != 200:
            print(f"FAIL: receiver failure control returned {failure.status_code}.")
            return False

        retry_payload = {
            "id": retry_id,
            "event_type": "smoke.retry",
            "payload": {"source": "deployment-smoke"},
            "target_url": f"{receiver_url}/webhook",
        }
        retry_body = json.dumps(
            retry_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        retry_response = client.post(
            f"{api_url}/webhooks",
            content=retry_body,
            headers={
                "Content-Type": "application/json",
                "X-Signature": calculate_signature(webhook_secret, retry_body),
            },
        )
        if retry_response.status_code != 202:
            print(f"FAIL: retry ingestion returned {retry_response.status_code}.")
            return False

        retry_state = _wait_for_status(client, api_url, retry_id, timeout)
        if (
            not retry_state
            or retry_state.get("status") != "DELIVERED"
            or retry_state.get("attempt_count", 0) < 2
        ):
            print("FAIL: temporary failure did not recover through retry.")
            return False

        print("PASS: public end-to-end delivery, HMAC, deduplication, and retry verified.")
        return True
    finally:
        if owns_client:
            client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Public deployment smoke test")
    parser.add_argument("--api-url", default=os.getenv("API_URL"))
    parser.add_argument("--receiver-url", default=os.getenv("RECEIVER_URL"))
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--allow-http", action="store_true", help="Local testing only")
    args = parser.parse_args()

    missing = [
        name
        for name, value in (
            ("API_URL", args.api_url),
            ("RECEIVER_URL", args.receiver_url),
            ("WEBHOOK_SECRET", os.getenv("WEBHOOK_SECRET")),
            ("SMOKE_CONTROL_SECRET", os.getenv("SMOKE_CONTROL_SECRET")),
        )
        if not value
    ]
    if missing:
        parser.error(f"missing required configuration: {', '.join(missing)}")

    success = run_smoke_tests(
        api_url=args.api_url,
        receiver_url=args.receiver_url,
        webhook_secret=os.environ["WEBHOOK_SECRET"],
        control_secret=os.environ["SMOKE_CONTROL_SECRET"],
        timeout=args.timeout,
        require_https=not args.allow_http,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
