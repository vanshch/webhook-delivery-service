"""Tests for the controlled receiver and public smoke-test helpers."""

import json

import pytest
from fastapi.testclient import TestClient

from scripts.controlled_receiver import app as receiver_app
from scripts.controlled_receiver import validate_runtime_config
from scripts.controlled_receiver import verify_hmac
from scripts.smoke_test import calculate_signature, run_smoke_tests


OUTBOUND_SECRET = "outbound_secret_for_receiver_tests"
CONTROL_SECRET = "control_secret_for_receiver_tests"


@pytest.fixture
def receiver_client(monkeypatch):
    monkeypatch.setenv("OUTBOUND_WEBHOOK_SECRET", OUTBOUND_SECRET)
    monkeypatch.setenv("SMOKE_CONTROL_SECRET", CONTROL_SECRET)
    client = TestClient(receiver_app)
    headers = {"Authorization": f"Bearer {CONTROL_SECRET}"}
    assert client.post("/control/reset", headers=headers).status_code == 200
    yield client, headers
    client.post("/control/reset", headers=headers)


def _delivery(event_id: str) -> tuple[bytes, dict[str, str]]:
    body = json.dumps({"id": event_id, "payload": {"ok": True}}).encode()
    return body, {
        "Content-Type": "application/json",
        "X-Signature": calculate_signature(OUTBOUND_SECRET, body),
    }


def test_hmac_helpers_accept_only_exact_valid_body():
    body = b'{"test":"data"}'
    signature = calculate_signature(OUTBOUND_SECRET, body)
    assert verify_hmac(body, signature, OUTBOUND_SECRET)
    assert not verify_hmac(body + b" ", signature, OUTBOUND_SECRET)
    assert not verify_hmac(body, "invalid", OUTBOUND_SECRET)
    assert not verify_hmac(body, signature, "")


def test_receiver_runtime_secrets_fail_closed(monkeypatch):
    monkeypatch.setenv("OUTBOUND_WEBHOOK_SECRET", "short")
    monkeypatch.setenv("SMOKE_CONTROL_SECRET", "another-short")
    with pytest.raises(RuntimeError, match="at least 32"):
        validate_runtime_config()

    shared = "shared_receiver_secret_value_32_chars"
    monkeypatch.setenv("OUTBOUND_WEBHOOK_SECRET", shared)
    monkeypatch.setenv("SMOKE_CONTROL_SECRET", shared)
    with pytest.raises(RuntimeError, match="must be distinct"):
        validate_runtime_config()


def test_receiver_rejects_invalid_hmac(receiver_client):
    client, control_headers = receiver_client
    response = client.post(
        "/webhook",
        content=b'{"id":"invalid-signature"}',
        headers={"X-Signature": "sha256=bad"},
    )
    assert response.status_code == 401
    assert client.get("/events", headers=control_headers).json()["events"] == []


def test_receiver_controls_and_event_state_require_authorization(receiver_client):
    client, _ = receiver_client
    assert client.get("/events").status_code == 401
    assert client.post("/control/reset").status_code == 401
    assert client.post("/control/fail-next").status_code == 401


def test_receiver_records_authenticated_delivery(receiver_client):
    client, control_headers = receiver_client
    body, delivery_headers = _delivery("delivery-1")
    response = client.post("/webhook", content=body, headers=delivery_headers)
    assert response.status_code == 200
    event = client.get("/events/delivery-1", headers=control_headers)
    assert event.status_code == 200
    assert event.json()["valid_signature"] is True


def test_receiver_simulates_one_retryable_failure(receiver_client):
    client, control_headers = receiver_client
    body, delivery_headers = _delivery("retry-1")
    configured = client.post("/control/fail-event/retry-1", headers=control_headers)
    assert configured.status_code == 200
    assert client.post("/webhook", content=body, headers=delivery_headers).status_code == 503
    assert client.post("/webhook", content=body, headers=delivery_headers).status_code == 200


def test_public_smoke_requires_https_before_network_calls():
    assert not run_smoke_tests(
        api_url="http://api.example.test",
        receiver_url="https://receiver.example.test",
        webhook_secret="inbound",
        control_secret="control",
    )
