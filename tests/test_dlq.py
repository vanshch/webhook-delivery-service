"""Tests for DLQ listing, inspection, replay, and persistence failure paths."""

import pytest
import json
import httpx
from unittest.mock import AsyncMock
from app.config import settings
from app.storage import redis_client
from app.models import IncomingWebhook, DeliveryState, DeliveryStatus
from app.core.delivery_state import get_delivery_state
from app.core.delivery_state import save_delivery_state
from app.core.idempotency import get_idempotency_key
from app.core.dlq import (
    list_dlq_events,
    inspect_dlq_event,
    replay_dlq_event,
    replay_selected_events,
)
from app.workers.delivery_worker import process_job


@pytest.mark.asyncio
async def test_dlq_list_and_inspect(fake_redis):
    async_redis = redis_client.get_async_redis()
    event_id = "evt_dlq_test_1"
    webhook_data = IncomingWebhook(
        id=event_id,
        event_type="order.failed",
        payload={"order_id": 999},
        target_url="https://example.com/dead",
    )
    dlq_item = {
        "webhook": webhook_data.model_dump(mode="json"),
        "last_attempt": 5,
        "failed_at": 1700000000.0,
        "target_url": "https://example.com/dead",
        "last_error": "Delivery failed after 5 attempts",
    }
    await async_redis.lpush(settings.dlq_key, json.dumps(dlq_item))

    # Test list_dlq_events
    events = await list_dlq_events(async_redis)
    assert len(events) == 1
    assert events[0]["webhook"]["id"] == event_id

    # Test inspect_dlq_event
    inspected = await inspect_dlq_event(async_redis, event_id)
    assert inspected is not None
    assert inspected["webhook"]["id"] == event_id
    assert inspected["last_attempt"] == 5

    # Inspect non-existent event
    assert await inspect_dlq_event(async_redis, "evt_missing") is None


@pytest.mark.asyncio
async def test_dlq_replay_creates_new_pending_delivery_safely(fake_redis):
    async_redis = redis_client.get_async_redis()
    event_id = "evt_dlq_replay_1"
    webhook_data = IncomingWebhook(
        id=event_id,
        event_type="payment.failed",
        payload={"amount": 100},
        target_url="https://example.com/pay",
    )
    dlq_item = {
        "webhook": webhook_data.model_dump(mode="json"),
        "last_attempt": 5,
        "failed_at": 1700000000.0,
        "target_url": "https://example.com/pay",
        "last_error": "Delivery failed after 5 attempts",
    }
    await async_redis.lpush(settings.dlq_key, json.dumps(dlq_item))
    await async_redis.set(f"attempt:{event_id}", "5")

    # Replay event
    success = await replay_dlq_event(async_redis, event_id)
    assert success is True

    # 1. Event is removed from DLQ
    assert await async_redis.llen(settings.dlq_key) == 0

    # 2. Attempt counter is reset
    assert await async_redis.exists(f"attempt:{event_id}") == 0

    # 3. Delivery status reset to PENDING with 0 attempts
    state = await get_delivery_state(async_redis, event_id)
    assert state is not None
    assert state.status == DeliveryStatus.PENDING
    assert state.attempt_count == 0
    assert state.last_error is None

    # 4. Idempotency key refreshed
    idempotency_key = get_idempotency_key(event_id)
    assert await async_redis.exists(idempotency_key) == 1

    # 5. Enqueued to stream
    assert await async_redis.xlen(settings.stream_name) == 1


@pytest.mark.asyncio
async def test_replay_selected_events(fake_redis):
    async_redis = redis_client.get_async_redis()
    eids = ["evt_batch_1", "evt_batch_2"]
    for eid in eids:
        wh = IncomingWebhook(id=eid, event_type="type", payload={})
        dlq_item = {"webhook": wh.model_dump(mode="json"), "last_attempt": 5}
        await async_redis.lpush(settings.dlq_key, json.dumps(dlq_item))

    res = await replay_selected_events(async_redis, eids)
    assert res["replayed"] == eids
    assert res["failed"] == []
    assert await async_redis.llen(settings.dlq_key) == 0
    assert await async_redis.xlen(settings.stream_name) == 2


@pytest.mark.asyncio
async def test_replay_enqueue_failure_preserves_dlq_and_state(fake_redis):
    async_redis = redis_client.get_async_redis()
    event_id = "evt_replay_persist_fail"
    webhook = IncomingWebhook(
        id=event_id,
        event_type="payment.failed",
        payload={"amount": 100},
        target_url="https://example.com/pay",
    )
    dlq_item = {
        "webhook": webhook.model_dump(mode="json"),
        "last_attempt": 5,
    }
    raw_dlq_item = json.dumps(dlq_item)
    await async_redis.lpush(settings.dlq_key, raw_dlq_item)
    await async_redis.set(f"attempt:{event_id}", "5")
    await async_redis.set(get_idempotency_key(event_id), "original")
    await save_delivery_state(
        async_redis,
        DeliveryState(
            event_id=event_id,
            status=DeliveryStatus.DEAD,
            attempt_count=5,
        ),
    )
    await async_redis.set(settings.stream_name, "wrong-type")

    assert await replay_dlq_event(async_redis, event_id) is False

    assert await async_redis.lrange(settings.dlq_key, 0, -1) == [raw_dlq_item]
    assert await async_redis.get(f"attempt:{event_id}") == "5"
    assert await async_redis.get(get_idempotency_key(event_id)) == "original"
    state = await get_delivery_state(async_redis, event_id)
    assert state is not None
    assert state.status == DeliveryStatus.DEAD
    assert state.attempt_count == 5


@pytest.mark.asyncio
async def test_persistence_failure_prevents_acknowledgement(fake_redis, monkeypatch):
    """Negative test: If state persistence fails, stream message is NOT acknowledged."""
    async_redis = redis_client.get_async_redis()
    event_id = "evt_persist_fail_1"
    webhook_data = IncomingWebhook(
        id=event_id,
        event_type="test.event",
        payload={"key": "val"},
        target_url="https://example.com/webhook",
    )
    job_json = webhook_data.model_dump_json()

    # Mock xack to track call count
    mock_xack = AsyncMock()
    monkeypatch.setattr(async_redis, "xack", mock_xack)

    # Ingest mock HTTP 200 delivery
    mock_res_ok = httpx.Response(200, request=httpx.Request("POST", "https://example.com/webhook"))
    async def mock_post_ok(*args, **kwargs):
        return mock_res_ok

    # Inject failure into save_delivery_state (e.g. Redis write error)
    import app.workers.delivery_worker
    async def failing_save_delivery_state(*args, **kwargs):
        raise RuntimeError("Redis disk error on state persistence")

    monkeypatch.setattr(app.workers.delivery_worker, "save_delivery_state", failing_save_delivery_state)

    async with httpx.AsyncClient() as http_client:
        monkeypatch.setattr(http_client, "post", mock_post_ok)
        with pytest.raises(RuntimeError, match="Redis disk error"):
            await process_job(
                redis_conn=async_redis,
                job_json=job_json,
                client=http_client,
                stream_msg_id="100-0",
            )

    # Prove stream message was NOT acknowledged
    assert mock_xack.call_count == 0
