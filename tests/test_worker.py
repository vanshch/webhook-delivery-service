import pytest
import json
import hashlib
import hmac
import time
from unittest.mock import AsyncMock, patch
import httpx
from app.core.security import verify_signature
from app.workers.delivery_worker import (
    deliver_webhook,
    process_job,
    poll_delay_queue,
    run_process_job
)
from app.models import IncomingWebhook
from app.config import settings

@pytest.fixture
def mock_httpx():
    return AsyncMock(spec=httpx.AsyncClient)

@pytest.mark.asyncio
async def test_deliver_webhook_success(mock_httpx):
    mock_httpx.post.return_value = httpx.Response(status_code=200)
    webhook = IncomingWebhook(id="123", event_type="test", payload={"foo": "bar"})
    
    success = await deliver_webhook(
        webhook,
        "http://example.com/target",
        mock_httpx,
    )
    assert success is True
    mock_httpx.post.assert_called_once()

@pytest.mark.asyncio
async def test_deliver_webhook_failure_status(mock_httpx):
    mock_httpx.post.return_value = httpx.Response(status_code=500)
    webhook = IncomingWebhook(id="123", event_type="test", payload={"foo": "bar"})
    
    success = await deliver_webhook(
        webhook,
        "http://example.com/target",
        mock_httpx,
    )
    assert success is False

@pytest.mark.asyncio
async def test_deliver_webhook_timeout(mock_httpx):
    mock_httpx.post.side_effect = httpx.ConnectTimeout("Timeout connecting to server")
    webhook = IncomingWebhook(id="123", event_type="test", payload={"foo": "bar"})
    
    success = await deliver_webhook(
        webhook,
        "http://example.com/target",
        mock_httpx,
    )
    assert success is False

@pytest.mark.asyncio
async def test_deliver_webhook_signs_exact_utf8_request_body(monkeypatch):
    inbound_secret = "inbound-secret"
    outbound_secret = "outbound-secret"
    monkeypatch.setattr(settings, "webhook_secret", inbound_secret)
    monkeypatch.setattr(
        settings,
        "outbound_webhook_secret",
        outbound_secret,
    )

    received = {}

    async def receiver(request: httpx.Request) -> httpx.Response:
        received["body"] = await request.aread()
        received["signature"] = request.headers["x-signature"]
        return httpx.Response(status_code=204)

    webhook = IncomingWebhook(
        id="unicode-123",
        event_type="order.created",
        payload={
            "customer": {"name": "Vansh चौहान"},
            "items": [{"name": "café", "quantity": 2}],
        },
    )

    transport = httpx.MockTransport(receiver)
    async with httpx.AsyncClient(transport=transport) as client:
        success = await deliver_webhook(
            webhook,
            "https://receiver.example/webhooks",
            client,
        )

    expected_payload = {
        "id": webhook.id,
        "event_type": webhook.event_type,
        "payload": webhook.payload,
        "timestamp": webhook.timestamp.isoformat(),
    }
    expected_body = json.dumps(
        expected_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    expected_signature = "sha256=" + hmac.new(
        outbound_secret.encode("utf-8"),
        msg=received["body"],
        digestmod=hashlib.sha256,
    ).hexdigest()
    inbound_signature = "sha256=" + hmac.new(
        inbound_secret.encode("utf-8"),
        msg=received["body"],
        digestmod=hashlib.sha256,
    ).hexdigest()

    assert success is True
    assert received["body"] == expected_body
    assert json.loads(received["body"]) == expected_payload
    assert received["signature"] == expected_signature
    assert received["signature"] != inbound_signature

    tampered_body = received["body"].replace(
        "café".encode("utf-8"),
        "tea".encode("utf-8"),
    )
    assert verify_signature(
        received["body"],
        received["signature"],
        outbound_secret,
    )
    assert not verify_signature(
        tampered_body,
        received["signature"],
        outbound_secret,
    )

@pytest.mark.asyncio
async def test_process_job_happy_path(fake_redis, mock_httpx):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()
    
    mock_httpx.post.return_value = httpx.Response(status_code=200)
    webhook = IncomingWebhook(id="123", event_type="test", payload={"foo": "bar"}, target_url="http://example.com/target")
    job_json = webhook.model_dump_json()
    
    await async_redis.set("attempt:123", "1")
    
    await process_job(async_redis, job_json, mock_httpx)
    
    # Check that attempt key is deleted
    assert await async_redis.get("attempt:123") is None

@pytest.mark.asyncio
async def test_process_job_failure_retry(fake_redis, mock_httpx):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()
    
    mock_httpx.post.return_value = httpx.Response(status_code=500)
    webhook = IncomingWebhook(id="123", event_type="test", payload={"foo": "bar"}, target_url="http://example.com/target")
    job_json = webhook.model_dump_json()
    
    await process_job(async_redis, job_json, mock_httpx)
    
    # Check attempt key is incremented to 1 (from 0)
    assert await async_redis.get("attempt:123") == "1"
    
    # Check that the job is scheduled in the delay queue
    delay_jobs = await async_redis.zrange("webhook_delay_queue", 0, -1)
    assert len(delay_jobs) == 1
    assert delay_jobs[0] == job_json

@pytest.mark.asyncio
async def test_process_job_max_retries_to_dlq(fake_redis, mock_httpx):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()
    
    mock_httpx.post.return_value = httpx.Response(status_code=500)
    webhook = IncomingWebhook(id="123", event_type="test", payload={"foo": "bar"}, target_url="http://example.com/target")
    job_json = webhook.model_dump_json()
    
    # Set attempt count to 5 (max retry attempts is 5 by default)
    await async_redis.set("attempt:123", "5")
    
    await process_job(async_redis, job_json, mock_httpx)
    
    # Check attempt key is deleted
    assert await async_redis.get("attempt:123") is None
    
    # Confirm it was pushed to DLQ
    dlq_len = await async_redis.llen("webhook_dlq")
    assert dlq_len == 1
    
    dlq_item_json = await async_redis.lindex("webhook_dlq", 0)
    dlq_item = json.loads(dlq_item_json)
    assert dlq_item["webhook"]["id"] == "123"
    assert dlq_item["last_attempt"] == 6
    assert dlq_item["target_url"] == "http://example.com/target"

@pytest.mark.asyncio
async def test_process_job_corrupt_json(fake_redis, mock_httpx):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()
    
    # Should not crash the worker
    await process_job(async_redis, "corrupt_json{", mock_httpx)
    
    # Nothing in queue or DLQ
    assert await async_redis.llen("webhook_dlq") == 0

@pytest.mark.asyncio
async def test_process_job_missing_target_url(
    fake_redis,
    monkeypatch,
    mock_httpx,
):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()
    
    # Temporarily remove default target URL from settings
    monkeypatch.setattr(settings, "default_target_url", "")
    
    webhook = IncomingWebhook(id="123", event_type="test", payload={"foo": "bar"})
    job_json = webhook.model_dump_json()
    
    # Should log error and return without delivery
    await process_job(async_redis, job_json, mock_httpx)
    
    # Confirm nothing scheduled for retry or DLQ
    assert await async_redis.zcard("webhook_delay_queue") == 0
    assert await async_redis.llen("webhook_dlq") == 0

@pytest.mark.asyncio
async def test_poll_delay_queue(fake_redis):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()
    
    now = time.time()
    due_job = '{"id": "due"}'
    future_job = '{"id": "future"}'
    
    await async_redis.zadd("webhook_delay_queue", {due_job: now - 10, future_job: now + 10})
    
    class BreakLoop(Exception):
        pass
        
    with patch("app.workers.delivery_worker.asyncio.sleep", side_effect=BreakLoop):
        with pytest.raises(BreakLoop):
            await poll_delay_queue(async_redis)
            
    main_queue_len = await async_redis.llen("webhook_queue")
    assert main_queue_len == 1
    moved_job = await async_redis.lindex("webhook_queue", 0)
    assert moved_job == due_job
    
    delay_queue_card = await async_redis.zcard("webhook_delay_queue")
    assert delay_queue_card == 1
