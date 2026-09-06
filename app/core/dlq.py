"""Dead Letter Queue inspection and crash-safe replay utilities."""

import inspect
import json
from typing import Any, Dict, List, Optional

from loguru import logger

from app.config import settings
from app.core.delivery_state import get_delivery_key
from app.core.idempotency import get_idempotency_key
from app.models import DeliveryState, DeliveryStatus, IncomingWebhook


REPLAY_DLQ_LUA = """
local dlq_type = redis.call('TYPE', KEYS[1]).ok
if dlq_type ~= 'none' and dlq_type ~= 'list' then
    return redis.error_reply('DLQ key has an incompatible type')
end

local stream_type = redis.call('TYPE', KEYS[5]).ok
if stream_type ~= 'none' and stream_type ~= 'stream' then
    return redis.error_reply('delivery stream has an incompatible type')
end

local found = false
for _, item in ipairs(redis.call('LRANGE', KEYS[1], 0, -1)) do
    if item == ARGV[1] then
        found = true
        break
    end
end
if not found then
    return 0
end

-- Persist the replay destination before removing its DLQ source. If a later
-- write fails, the source remains recoverable and at worst a duplicate exists.
local stream_id = redis.call('XADD', KEYS[5], '*', 'payload', ARGV[4])
redis.call('SET', KEYS[3], '1', 'EX', ARGV[2])
redis.call('SET', KEYS[4], ARGV[3], 'EX', ARGV[2])
redis.call('DEL', KEYS[2])
redis.call('LREM', KEYS[1], 1, ARGV[1])
return stream_id
"""


async def _maybe_await(result):
    if inspect.isawaitable(result):
        return await result
    return result


async def list_dlq_events(redis_conn) -> List[Dict[str, Any]]:
    """List all valid events currently in the dead-letter queue."""
    items = await _maybe_await(redis_conn.lrange(settings.dlq_key, 0, -1))
    results = []
    for raw in items:
        try:
            results.append(json.loads(raw))
        except (TypeError, ValueError) as exc:
            logger.error(f"Failed to parse DLQ item: {exc}")
    return results


async def inspect_dlq_event(redis_conn, event_id: str) -> Optional[Dict[str, Any]]:
    """Find and return full details of one DLQ event."""
    get_idempotency_key(event_id)
    events = await list_dlq_events(redis_conn)
    for event in events:
        if event.get("webhook", {}).get("id") == event_id:
            return event
    return None


async def replay_dlq_event(redis_conn, event_id: str) -> bool:
    """Atomically create a pending replay before removing its DLQ source."""
    get_idempotency_key(event_id)
    raw_items = await _maybe_await(redis_conn.lrange(settings.dlq_key, 0, -1))
    target_raw = None
    webhook = None

    for raw in raw_items:
        try:
            parsed = json.loads(raw)
            if parsed.get("webhook", {}).get("id") == event_id:
                target_raw = raw
                webhook = IncomingWebhook.model_validate(parsed["webhook"])
                break
        except (KeyError, TypeError, ValueError):
            continue

    if target_raw is None or webhook is None:
        logger.warning(f"DLQ event {event_id} not found for replay")
        return False

    pending_state = DeliveryState(
        event_id=event_id,
        status=DeliveryStatus.PENDING,
    )
    try:
        result = await _maybe_await(
            redis_conn.eval(
                REPLAY_DLQ_LUA,
                5,
                settings.dlq_key,
                f"attempt:{event_id}",
                get_idempotency_key(event_id),
                get_delivery_key(event_id),
                settings.stream_name,
                target_raw,
                str(settings.idempotency_ttl_seconds),
                pending_state.model_dump_json(),
                webhook.model_dump_json(),
            )
        )
    except Exception as exc:
        logger.error(f"Failed to replay DLQ event {event_id}: {exc}")
        return False

    if not result:
        logger.warning(f"DLQ event {event_id} disappeared before replay")
        return False

    logger.info(f"Successfully replayed DLQ event {event_id} to stream {settings.stream_name}")
    return True


async def replay_selected_events(redis_conn, event_ids: List[str]) -> Dict[str, Any]:
    """Replay selected DLQ events independently and report partial failures."""
    replayed = []
    failed = []

    for event_id in event_ids:
        if await replay_dlq_event(redis_conn, event_id):
            replayed.append(event_id)
        else:
            failed.append(event_id)

    return {"replayed": replayed, "failed": failed}
