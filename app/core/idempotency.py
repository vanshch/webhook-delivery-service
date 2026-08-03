"""Idempotency and deduplication backed by an atomic Redis Lua script."""

from app.core.event_ids import validate_event_id
from app.storage import redis_client

IDEMPOTENCY_NAMESPACE = "idempotency:webhook"

DEDUPLICATE_AND_ENQUEUE_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then
    return 0
end
local enqueue_result = redis.pcall('XADD', KEYS[2], '*', ARGV[1], ARGV[2])
if enqueue_result.err then
    return redis.error_reply(enqueue_result.err)
end
local claim_result = redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[3])
if not claim_result then
    return redis.error_reply('idempotency claim failed after enqueue')
end
return 1
"""
def get_idempotency_key(event_id: str) -> str:
    """Return the fully namespaced Redis key for an event ID."""
    validate_event_id(event_id)
    return f"{IDEMPOTENCY_NAMESPACE}:{event_id}"


async def deduplicate_and_enqueue(
    event_id: str,
    payload_json: str,
    stream_name: str,
    ttl_seconds: int,
) -> bool:
    """Atomically deduplicate and enqueue an event with a Redis Lua script.

    Validates inputs before executing the Redis Lua script.
    The script orders operations so XADD succeeds before writing the idempotency key:
    1. Returns 0 if idempotency key already exists.
    2. Executes XADD to stream.
    3. Executes SET NX with EX ttl_seconds to mark idempotency.

    Returns:
        bool: True if newly accepted and enqueued, False if duplicate.
    """
    validate_event_id(event_id)
    if type(ttl_seconds) is not int or ttl_seconds <= 0:
        raise ValueError("idempotency ttl_seconds must be a positive integer")
    if not isinstance(stream_name, str) or not stream_name:
        raise ValueError("stream_name must be a non-empty string")
    if not isinstance(payload_json, str) or not payload_json:
        raise ValueError("payload_json must be a non-empty string")

    key = get_idempotency_key(event_id)
    if stream_name == key:
        raise ValueError("stream_name must differ from the idempotency key")
    client = redis_client.get_async_redis()

    result = await client.eval(
        DEDUPLICATE_AND_ENQUEUE_LUA,
        2,
        key,
        stream_name,
        "payload",
        payload_json,
        str(ttl_seconds),
    )
    return bool(result)
