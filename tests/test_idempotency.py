import pytest
import hmac
import hashlib
from fastapi import status
from app.core.idempotency import is_duplicate, mark_processed
from app.config import settings

@pytest.mark.asyncio
async def test_is_duplicate_not_seen(fake_redis):
    assert await is_duplicate("key_123") is False

@pytest.mark.asyncio
async def test_is_duplicate_suppressed(fake_redis):
    await mark_processed("key_123", ttl_seconds=60)
    assert await is_duplicate("key_123") is True

@pytest.mark.asyncio
async def test_mark_processed_sets_ttl(fake_redis):
    await mark_processed("key_456", ttl_seconds=100)
    redis_key = "idempotency:key_456"
    assert fake_redis.get(redis_key) == "1"
    
    ttl = fake_redis.ttl(redis_key)
    assert 0 < ttl <= 100

@pytest.mark.asyncio
async def test_idempotency_ttl_expired(fake_redis):
    import asyncio
    await mark_processed("temp_key", ttl_seconds=1)
    assert await is_duplicate("temp_key") is True
    
    # Sleep to allow fakeredis to expire the key
    await asyncio.sleep(1.1)
    assert await is_duplicate("temp_key") is False

def test_duplicate_webhook_ignored(client, fake_redis):
    secret = "your_secret_here"
    payload = b'{"id": "webhook_dup_1", "event_type": "test", "payload": {}}'
    
    # Generate signature
    mac = hmac.new(secret.encode(), msg=payload, digestmod=hashlib.sha256).hexdigest()
    signature_header = f"sha256={mac}"
    
    # Send first request (should be accepted)
    response1 = client.post(
        "/webhooks",
        content=payload,
        headers={"x-signature": signature_header}
    )
    assert response1.status_code == status.HTTP_202_ACCEPTED
    assert response1.json() == {"status": "accepted"}
    
    # Verify it was enqueued in Redis
    assert fake_redis.llen("webhook_queue") == 1
    
    # Send second request with the same ID (should be duplicate ignored)
    response2 = client.post(
        "/webhooks",
        content=payload,
        headers={"x-signature": signature_header}
    )
    assert response2.status_code == status.HTTP_200_OK
    assert response2.json() == {"status": "duplicate ignored"}
    
    # Verify no additional items were enqueued
    assert fake_redis.llen("webhook_queue") == 1
