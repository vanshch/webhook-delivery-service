import pytest
import json
import hashlib
import hmac
import time
import asyncio
from unittest.mock import AsyncMock, patch
import httpx
from app.core.security import verify_signature
from app.workers.delivery_worker import (
    deliver_webhook,
    process_job,
    poll_delay_queue,
    run_process_job,
    ensure_consumer_group,
    promote_delayed_jobs,
    reclaim_stale_pending_jobs,
    DeliveryWorker,
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

    # Should not crash the worker, should quarantine
    await process_job(async_redis, "corrupt_json{", mock_httpx)

    # Quarantined
    assert await async_redis.llen(settings.quarantine_queue) == 1
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

    # Should log error, move to quarantine, and return without delivery
    await process_job(async_redis, job_json, mock_httpx)

    # Confirm item reached quarantine queue
    assert await async_redis.llen(settings.quarantine_queue) == 1
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

    main_stream_len = await async_redis.xlen(settings.stream_name)
    assert main_stream_len == 1
    stream_entries = await async_redis.xrange(settings.stream_name)
    assert stream_entries[0][1]["payload"] == due_job

    delay_queue_card = await async_redis.zcard("webhook_delay_queue")
    assert delay_queue_card == 1

# --- New Commit 2 Stream & Crash-Safety Tests ---

@pytest.mark.asyncio
async def test_crash_before_ack_reclaim(fake_redis, mock_httpx):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()
    mock_httpx.post.return_value = httpx.Response(status_code=200)

    await ensure_consumer_group(async_redis, settings.stream_name, settings.consumer_group)
    webhook = IncomingWebhook(id="crash-1", event_type="test", payload={"x": 1}, target_url="http://example.com/target")
    job_json = webhook.model_dump_json()

    msg_id = await async_redis.xadd(settings.stream_name, {"payload": job_json})

    # Worker 1 reads message from stream via consumer group but crashes/exits without XACK
    read_resp = await async_redis.xreadgroup(settings.consumer_group, "worker-1", {settings.stream_name: ">"}, count=1)
    assert len(read_resp[0][1]) == 1

    # Verify message is pending in worker-1's PEL
    pending_before = await async_redis.xpending_range(settings.stream_name, settings.consumer_group, min="-", max="+", count=10)
    assert len(pending_before) == 1
    assert pending_before[0]["consumer"] == "worker-1"

    # Worker 2 reclaims stale pending messages
    reclaimed = await reclaim_stale_pending_jobs(
        async_redis,
        stream_name=settings.stream_name,
        consumer_group=settings.consumer_group,
        worker_id="worker-2",
        min_idle_time_ms=0,
    )
    assert len(reclaimed) == 1
    reclaimed_msg_id, fields = reclaimed[0]
    assert reclaimed_msg_id == msg_id

    # Worker 2 processes reclaimed message and ACKs
    await process_job(async_redis, fields["payload"], mock_httpx, stream_msg_id=reclaimed_msg_id)

    # Verify message is no longer pending
    pending_after = await async_redis.xpending_range(settings.stream_name, settings.consumer_group, min="-", max="+", count=10)
    assert len(pending_after) == 0

@pytest.mark.asyncio
async def test_successful_ack_removes_from_pel(fake_redis, mock_httpx):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()
    mock_httpx.post.return_value = httpx.Response(status_code=200)

    await ensure_consumer_group(async_redis, settings.stream_name, settings.consumer_group)
    webhook = IncomingWebhook(id="ack-1", event_type="test", payload={"x": 1}, target_url="http://example.com/target")
    job_json = webhook.model_dump_json()

    msg_id = await async_redis.xadd(settings.stream_name, {"payload": job_json})
    await async_redis.xreadgroup(settings.consumer_group, "worker-1", {settings.stream_name: ">"}, count=1)

    original_xack = async_redis.xack

    async def execute_xack(*args, **kwargs):
        return await original_xack(*args, **kwargs)

    with patch.object(async_redis, "xack", AsyncMock(side_effect=execute_xack)) as xack:
        await process_job(async_redis, job_json, mock_httpx, stream_msg_id=msg_id)

    xack.assert_awaited_once_with(
        settings.stream_name,
        settings.consumer_group,
        msg_id,
    )

    pending = await async_redis.xpending_range(settings.stream_name, settings.consumer_group, min="-", max="+", count=10)
    assert len(pending) == 0

@pytest.mark.asyncio
async def test_retry_persistence_before_ack(fake_redis, mock_httpx):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()
    mock_httpx.post.return_value = httpx.Response(status_code=500)

    await ensure_consumer_group(async_redis, settings.stream_name, settings.consumer_group)
    webhook = IncomingWebhook(id="retry-1", event_type="test", payload={"x": 1}, target_url="http://example.com/target")
    job_json = webhook.model_dump_json()

    msg_id = await async_redis.xadd(settings.stream_name, {"payload": job_json})
    await async_redis.xreadgroup(settings.consumer_group, "worker-1", {settings.stream_name: ">"}, count=1)

    await process_job(async_redis, job_json, mock_httpx, stream_msg_id=msg_id)

    # Verify attempt key set in Redis
    assert await async_redis.get("attempt:retry-1") == "1"
    # Verify job in delay queue
    delay_jobs = await async_redis.zrange("webhook_delay_queue", 0, -1)
    assert len(delay_jobs) == 1
    # Verify ACK succeeded
    pending = await async_redis.xpending_range(settings.stream_name, settings.consumer_group, min="-", max="+", count=10)
    assert len(pending) == 0

@pytest.mark.asyncio
async def test_retry_persistence_failure_keeps_message_pending(fake_redis, mock_httpx):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()
    mock_httpx.post.return_value = httpx.Response(status_code=500)

    await ensure_consumer_group(async_redis, settings.stream_name, settings.consumer_group)
    webhook = IncomingWebhook(id="retry-persist-fail", event_type="test", payload={"x": 1}, target_url="http://example.com/target")
    job_json = webhook.model_dump_json()
    msg_id = await async_redis.xadd(settings.stream_name, {"payload": job_json})
    await async_redis.xreadgroup(settings.consumer_group, "worker-1", {settings.stream_name: ">"}, count=1)

    with patch.object(async_redis, "zadd", AsyncMock(side_effect=RuntimeError("delay queue unavailable"))):
        with pytest.raises(RuntimeError, match="delay queue unavailable"):
            await process_job(async_redis, job_json, mock_httpx, stream_msg_id=msg_id)

    pending = await async_redis.xpending_range(settings.stream_name, settings.consumer_group, min="-", max="+", count=10)
    assert len(pending) == 1

@pytest.mark.asyncio
async def test_dlq_persistence_before_ack(fake_redis, mock_httpx):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()
    mock_httpx.post.return_value = httpx.Response(status_code=500)

    await ensure_consumer_group(async_redis, settings.stream_name, settings.consumer_group)
    webhook = IncomingWebhook(id="dlq-1", event_type="test", payload={"x": 1}, target_url="http://example.com/target")
    job_json = webhook.model_dump_json()

    await async_redis.set("attempt:dlq-1", "5")
    msg_id = await async_redis.xadd(settings.stream_name, {"payload": job_json})
    await async_redis.xreadgroup(settings.consumer_group, "worker-1", {settings.stream_name: ">"}, count=1)

    await process_job(async_redis, job_json, mock_httpx, stream_msg_id=msg_id)

    # Verify DLQ item created & attempt deleted
    assert await async_redis.get("attempt:dlq-1") is None
    assert await async_redis.llen(settings.dlq_key) == 1
    # Verify ACK succeeded
    pending = await async_redis.xpending_range(settings.stream_name, settings.consumer_group, min="-", max="+", count=10)
    assert len(pending) == 0

@pytest.mark.asyncio
async def test_dlq_persistence_failure_keeps_message_pending(fake_redis, mock_httpx):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()
    mock_httpx.post.return_value = httpx.Response(status_code=500)

    await ensure_consumer_group(async_redis, settings.stream_name, settings.consumer_group)
    webhook = IncomingWebhook(id="dlq-persist-fail", event_type="test", payload={"x": 1}, target_url="http://example.com/target")
    job_json = webhook.model_dump_json()
    await async_redis.set("attempt:dlq-persist-fail", str(settings.max_retry_attempts))
    msg_id = await async_redis.xadd(settings.stream_name, {"payload": job_json})
    await async_redis.xreadgroup(settings.consumer_group, "worker-1", {settings.stream_name: ">"}, count=1)

    with patch.object(async_redis, "lpush", AsyncMock(side_effect=RuntimeError("DLQ unavailable"))):
        with pytest.raises(RuntimeError, match="DLQ unavailable"):
            await process_job(async_redis, job_json, mock_httpx, stream_msg_id=msg_id)

    pending = await async_redis.xpending_range(settings.stream_name, settings.consumer_group, min="-", max="+", count=10)
    assert len(pending) == 1

@pytest.mark.asyncio
async def test_malformed_job_quarantine(fake_redis, mock_httpx):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()

    await ensure_consumer_group(async_redis, settings.stream_name, settings.consumer_group)
    corrupt_json = "{bad_json: 123"

    msg_id = await async_redis.xadd(settings.stream_name, {"payload": corrupt_json})
    await async_redis.xreadgroup(settings.consumer_group, "worker-1", {settings.stream_name: ">"}, count=1)

    await process_job(async_redis, corrupt_json, mock_httpx, stream_msg_id=msg_id)

    # Verify item moved to quarantine queue
    assert await async_redis.llen(settings.quarantine_queue) == 1
    quarantine_raw = await async_redis.lindex(settings.quarantine_queue, 0)
    q_data = json.loads(quarantine_raw)
    assert q_data["job"] == corrupt_json
    # Verify message ACKed
    pending = await async_redis.xpending_range(settings.stream_name, settings.consumer_group, min="-", max="+", count=10)
    assert len(pending) == 0

@pytest.mark.asyncio
async def test_atomic_delayed_promotion(fake_redis):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()

    now = time.time()
    due_job = '{"id": "due_1"}'
    future_job = '{"id": "future_1"}'

    await async_redis.zadd(settings.delay_queue_key, {due_job: now - 5, future_job: now + 300})

    moved = await promote_delayed_jobs(async_redis)
    assert moved == 1

    # Verify due job moved to stream
    stream_len = await async_redis.xlen(settings.stream_name)
    assert stream_len == 1

    # Verify delay queue contains only future job
    assert await async_redis.zcard(settings.delay_queue_key) == 1

@pytest.mark.asyncio
async def test_delayed_promotion_write_failure_preserves_source_job(fake_redis):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()

    due_job = '{"id": "must_not_be_lost"}'
    await async_redis.zadd(settings.delay_queue_key, {due_job: time.time() - 5})
    await async_redis.set(settings.stream_name, "wrong-type")

    moved = await promote_delayed_jobs(async_redis)

    assert moved == 0
    assert await async_redis.zrange(settings.delay_queue_key, 0, -1) == [due_job]

@pytest.mark.asyncio
async def test_graceful_shutdown_waits_for_in_flight_tasks(fake_redis):
    from app.storage.redis_client import get_async_redis
    async_redis = get_async_redis()

    worker = DeliveryWorker(worker_id="worker-shutdown-test", block_ms=50)
    await ensure_consumer_group(async_redis, worker.stream_name, worker.consumer_group)

    webhook = IncomingWebhook(id="shut-1", event_type="test", payload={"a": 1}, target_url="http://example.com/target")
    job_json = webhook.model_dump_json()

    await async_redis.xadd(worker.stream_name, {"payload": job_json})

    completed_flag = []

    async def slow_deliver(webhook, target_url, client):
        await asyncio.sleep(0.1)
        completed_flag.append(True)
        return True

    with patch("app.workers.delivery_worker.deliver_webhook", side_effect=slow_deliver):
        run_task = asyncio.create_task(worker.run(async_redis))
        await asyncio.sleep(0.05)
        # Signal shutdown while task is in flight
        worker.signal_shutdown()
        await run_task

    assert completed_flag == [True]
    pending = await async_redis.xpending_range(worker.stream_name, worker.consumer_group, min="-", max="+", count=10)
    assert len(pending) == 0
