import pytest
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
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
    with patch("app.workers.delivery_worker.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        yield mock_client

@pytest.mark.asyncio
async def test_deliver_webhook_success(mock_httpx):
    mock_httpx.post.return_value = httpx.Response(status_code=200)
    webhook = IncomingWebhook(id="123", event_type="test", payload={"foo": "bar"})
    
    success = await deliver_webhook(webhook, "http://example.com/target")
    assert success is True
    mock_httpx.post.assert_called_once()

@pytest.mark.asyncio
async def test_deliver_webhook_failure_status(mock_httpx):
    mock_httpx.post.return_value = httpx.Response(status_code=500)
    webhook = IncomingWebhook(id="123", event_type="test", payload={"foo": "bar"})
    
    success = await deliver_webhook(webhook, "http://example.com/target")
    assert success is False

@pytest.mark.asyncio
async def test_deliver_webhook_timeout(mock_httpx):
    mock_httpx.post.side_effect = httpx.ConnectTimeout("Timeout connecting to server")
    webhook = IncomingWebhook(id="123", event_type="test", payload={"foo": "bar"})
    
    success = await deliver_webhook(webhook, "http://example.com/target")
    assert success is False

@pytest.mark.asyncio
async def test_process_job_happy_path(fake_redis, mock_httpx):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()
    
    mock_httpx.post.return_value = httpx.Response(status_code=200)
    webhook = IncomingWebhook(id="123", event_type="test", payload={"foo": "bar"}, target_url="http://example.com/target")
    job_json = webhook.model_dump_json()
    
    await async_redis.set("attempt:123", "1")
    
    await process_job(async_redis, job_json)
    
    # Check that attempt key is deleted
    assert await async_redis.get("attempt:123") is None

@pytest.mark.asyncio
async def test_process_job_failure_retry(fake_redis, mock_httpx):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()
    
    mock_httpx.post.return_value = httpx.Response(status_code=500)
    webhook = IncomingWebhook(id="123", event_type="test", payload={"foo": "bar"}, target_url="http://example.com/target")
    job_json = webhook.model_dump_json()
    
    await process_job(async_redis, job_json)
    
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
    
    await process_job(async_redis, job_json)
    
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
async def test_process_job_corrupt_json(fake_redis):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()
    
    # Should not crash the worker
    await process_job(async_redis, "corrupt_json{")
    
    # Nothing in queue or DLQ
    assert await async_redis.llen("webhook_dlq") == 0

@pytest.mark.asyncio
async def test_process_job_missing_target_url(fake_redis, monkeypatch):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()
    
    # Temporarily remove default target URL from settings
    monkeypatch.setattr(settings, "default_target_url", "")
    
    webhook = IncomingWebhook(id="123", event_type="test", payload={"foo": "bar"})
    job_json = webhook.model_dump_json()
    
    # Should log error and return without delivery
    await process_job(async_redis, job_json)
    
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
