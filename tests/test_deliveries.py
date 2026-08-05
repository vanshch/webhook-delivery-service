"""Tests for delivery status tracking endpoint and status transitions."""

import pytest
import hmac
import hashlib
import json
import httpx
from fastapi import status
from app.config import settings
from app.models import IncomingWebhook
from app.storage import redis_client
from app.workers.delivery_worker import process_job


def generate_webhook_signature(payload_bytes: bytes) -> str:
    mac = hmac.new(
        settings.webhook_secret.encode(),
        msg=payload_bytes,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return f"sha256={mac}"


def test_get_delivery_invalid_event_id(client):
    response = client.get("/deliveries/invalid;id")
    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_get_delivery_not_found(client):
    response = client.get("/deliveries/evt_non_existent")
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "Delivery status not found"


def test_delivery_status_starts_at_pending(client, fake_redis):
    event_id = "evt_status_pending_1"
    payload = json.dumps({"id": event_id, "event_type": "user.created", "payload": {"foo": "bar"}}).encode()
    sig = generate_webhook_signature(payload)

    response = client.post("/webhooks", content=payload, headers={"x-signature": sig})
    assert response.status_code == status.HTTP_202_ACCEPTED

    # Check status endpoint
    status_resp = client.get(f"/deliveries/{event_id}")
    assert status_resp.status_code == status.HTTP_200_OK
    data = status_resp.json()
    assert data["event_id"] == event_id
    assert data["status"] == "PENDING"
    assert data["attempt_count"] == 0
    assert data["last_attempt_time"] is None
    assert data["last_error"] is None
    assert data["next_retry_time"] is None
    assert data["final_delivery_time"] is None


@pytest.mark.asyncio
async def test_delivery_status_moves_pending_retrying_delivered(client, fake_redis, monkeypatch):
    async_redis = redis_client.get_async_redis()
    event_id = "evt_status_flow_1"
    webhook_data = IncomingWebhook(
        id=event_id,
        event_type="test.event",
        payload={"key": "val"},
        target_url="https://example.com/webhook",
    )
    job_json = webhook_data.model_dump_json()

    # Ingest event
    payload_bytes = job_json.encode("utf-8")
    sig = generate_webhook_signature(payload_bytes)
    client.post("/webhooks", content=payload_bytes, headers={"x-signature": sig})

    # Initially PENDING
    res_pending = client.get(f"/deliveries/{event_id}").json()
    assert res_pending["status"] == "PENDING"

    # Attempt 1: Delivery fails (HTTP 500) -> state becomes RETRYING
    mock_res_fail = httpx.Response(500, request=httpx.Request("POST", "https://example.com/webhook"))
    async def mock_post_fail(*args, **kwargs):
        return mock_res_fail

    async with httpx.AsyncClient() as http_client:
        monkeypatch.setattr(http_client, "post", mock_post_fail)
        await process_job(
            redis_conn=async_redis,
            job_json=job_json,
            client=http_client,
            stream_msg_id="1-0",
        )

    res_retry = client.get(f"/deliveries/{event_id}").json()
    assert res_retry["status"] == "RETRYING"
    assert res_retry["attempt_count"] == 1
    assert res_retry["last_attempt_time"] is not None
    assert res_retry["next_retry_time"] is not None

    # Attempt 2: Delivery succeeds (HTTP 200) -> state becomes DELIVERED
    mock_res_ok = httpx.Response(200, request=httpx.Request("POST", "https://example.com/webhook"))
    async def mock_post_ok(*args, **kwargs):
        return mock_res_ok

    async with httpx.AsyncClient() as http_client:
        monkeypatch.setattr(http_client, "post", mock_post_ok)
        await process_job(
            redis_conn=async_redis,
            job_json=job_json,
            client=http_client,
            stream_msg_id="2-0",
        )

    res_delivered = client.get(f"/deliveries/{event_id}").json()
    assert res_delivered["status"] == "DELIVERED"
    assert res_delivered["attempt_count"] == 2
    assert res_delivered["final_delivery_time"] is not None
    assert res_delivered["last_error"] is None


@pytest.mark.asyncio
async def test_delivery_status_moves_to_dead_on_exhaustion(client, fake_redis, monkeypatch):
    async_redis = redis_client.get_async_redis()
    event_id = "evt_status_dead_1"
    webhook_data = IncomingWebhook(
        id=event_id,
        event_type="test.event",
        payload={"key": "val"},
        target_url="https://example.com/webhook",
    )
    job_json = webhook_data.model_dump_json()

    # Set attempt counter near max limit (max_retry_attempts is 5)
    await async_redis.set(f"attempt:{event_id}", "4")

    mock_res_fail = httpx.Response(500, request=httpx.Request("POST", "https://example.com/webhook"))
    async def mock_post_fail(*args, **kwargs):
        return mock_res_fail

    async with httpx.AsyncClient() as http_client:
        monkeypatch.setattr(http_client, "post", mock_post_fail)
        await process_job(
            redis_conn=async_redis,
            job_json=job_json,
            client=http_client,
            stream_msg_id="5-0",
        )

    res_dead = client.get(f"/deliveries/{event_id}").json()
    assert res_dead["status"] == "DEAD"
    assert res_dead["attempt_count"] == 5
    assert res_dead["last_error"] is not None
