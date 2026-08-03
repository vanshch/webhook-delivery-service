import pytest
import hmac
import hashlib
import asyncio
from fastapi import status
from httpx import AsyncClient, ASGITransport
from redis.exceptions import ResponseError
from app.main import app as fastapi_app
from app.core.idempotency import (
    deduplicate_and_enqueue,
    get_idempotency_key,
)
from app.core.event_ids import validate_event_id
from app.config import settings


def test_duplicate_webhook_ignored(client, fake_redis):
    payload = b'{"id": "webhook_dup_1", "event_type": "test", "payload": {}}'

    # Generate signature
    mac = hmac.new(
        settings.webhook_secret.encode(),
        msg=payload,
        digestmod=hashlib.sha256,
    ).hexdigest()
    signature_header = f"sha256={mac}"

    # Send first request (should be accepted)
    response1 = client.post(
        "/webhooks",
        content=payload,
        headers={"x-signature": signature_header}
    )
    assert response1.status_code == status.HTTP_202_ACCEPTED
    assert response1.json() == {"status": "accepted"}

    # Verify it was enqueued in Redis Stream
    assert fake_redis.xlen(settings.stream_name) == 1

    # Send second request with the same ID (should be duplicate ignored)
    response2 = client.post(
        "/webhooks",
        content=payload,
        headers={"x-signature": signature_header}
    )
    assert response2.status_code == status.HTTP_200_OK
    assert response2.json() == {"status": "duplicate ignored"}

    # Verify no additional items were enqueued
    assert fake_redis.xlen(settings.stream_name) == 1


@pytest.mark.asyncio
async def test_100_concurrent_submissions(fake_redis):
    """Confirm 100 concurrent submissions produce exactly 1 stream entry and 1 accepted result."""
    payload = b'{"id": "concurrent_evt_100", "event_type": "test", "payload": {"foo": "bar"}}'
    mac = hmac.new(
        settings.webhook_secret.encode(),
        msg=payload,
        digestmod=hashlib.sha256,
    ).hexdigest()
    headers = {"x-signature": f"sha256={mac}"}

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        tasks = [
            async_client.post("/webhooks", content=payload, headers=headers)
            for _ in range(100)
        ]
        responses = await asyncio.gather(*tasks)

    status_codes = [r.status_code for r in responses]
    json_responses = [r.json() for r in responses]

    assert status_codes.count(status.HTTP_202_ACCEPTED) == 1
    assert status_codes.count(status.HTTP_200_OK) == 99
    assert json_responses.count({"status": "accepted"}) == 1
    assert json_responses.count({"status": "duplicate ignored"}) == 99
    assert fake_redis.xlen(settings.stream_name) == 1


@pytest.mark.asyncio
async def test_injected_xadd_failure_leaves_no_idempotency_key(fake_redis):
    """Confirm no idempotency key remains if XADD enqueueing fails."""
    event_id = "evt_xadd_failed"
    redis_key = get_idempotency_key(event_id)

    # Intentionally corrupt stream destination with a string key so XADD raises WRONGTYPE error
    fake_redis.set(settings.stream_name, "invalid_string_value")

    with pytest.raises(ResponseError):
        await deduplicate_and_enqueue(
            event_id=event_id,
            payload_json='{"id":"evt_xadd_failed"}',
            stream_name=settings.stream_name,
            ttl_seconds=60,
        )

    # Idempotency key must not exist because XADD failed before SET
    assert fake_redis.exists(redis_key) == 0


@pytest.mark.asyncio
async def test_acceptance_works_again_after_ttl_expiry(fake_redis):
    """Confirm event can be accepted again after idempotency TTL expires."""
    event_id = "evt_ttl_expiry"
    payload_json = '{"id":"evt_ttl_expiry"}'

    # First attempt -> accepted
    redis_key = get_idempotency_key(event_id)

    res1 = await deduplicate_and_enqueue(event_id, payload_json, settings.stream_name, ttl_seconds=60)
    assert res1 is True
    assert fake_redis.xlen(settings.stream_name) == 1

    # Immediate second attempt -> duplicate
    res2 = await deduplicate_and_enqueue(event_id, payload_json, settings.stream_name, ttl_seconds=60)
    assert res2 is False
    assert fake_redis.xlen(settings.stream_name) == 1

    # Expire the key explicitly to avoid a wall-clock sleep in the test suite.
    fake_redis.pexpire(redis_key, 1)
    await asyncio.sleep(0.01)
    assert fake_redis.exists(redis_key) == 0

    # Third attempt after TTL expiry -> accepted again
    res3 = await deduplicate_and_enqueue(event_id, payload_json, settings.stream_name, ttl_seconds=60)
    assert res3 is True
    assert fake_redis.xlen(settings.stream_name) == 2


def test_event_id_validation():
    """Test strict but practical event ID validation rules."""
    valid_ids = [
        "key_123",
        "evt-456:789_ABC",
        "123e4567-e89b-12d3-a456-426614174000",
        "a" * 128,
    ]
    for valid_id in valid_ids:
        assert validate_event_id(valid_id) == valid_id

    invalid_ids = [
        "",
        "evt 123",
        "evt\n123",
        "evt123\n",
        "evt;drop",
        "<script>",
        "a" * 129,
    ]
    for invalid_id in invalid_ids:
        with pytest.raises(ValueError):
            validate_event_id(invalid_id)


def test_api_rejects_invalid_event_id_format(client):
    """API returns 400 when event ID format/length is invalid."""
    payload = b'{"id": "invalid id with spaces", "event_type": "test", "payload": {}}'
    mac = hmac.new(
        settings.webhook_secret.encode(),
        msg=payload,
        digestmod=hashlib.sha256,
    ).hexdigest()

    response = client.post(
        "/webhooks",
        content=payload,
        headers={"x-signature": f"sha256={mac}"}
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.asyncio
async def test_deduplicate_and_enqueue_invalid_arguments(fake_redis):
    """Validate arguments before execution so SET/Lua cannot fail due to bad params."""
    with pytest.raises(ValueError):
        await deduplicate_and_enqueue("evt1", '{"a":1}', settings.stream_name, ttl_seconds=0)

    with pytest.raises(ValueError):
        await deduplicate_and_enqueue("evt1", '{"a":1}', settings.stream_name, ttl_seconds=True)

    with pytest.raises(ValueError):
        await deduplicate_and_enqueue("evt1", '{"a":1}', stream_name="", ttl_seconds=60)
