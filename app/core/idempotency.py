from app.storage import redis_client

async def is_duplicate(idempotency_key: str) -> bool:
    """Return True if this webhook has already been seen.

    Uses Redis to check if the idempotency key exists.
    """
    client = redis_client.get_async_redis()
    key = f"idempotency:{idempotency_key}"
    return bool(await client.exists(key))


async def mark_processed(idempotency_key: str, ttl_seconds: int) -> None:
    """Record that a webhook id has been handled, expiring after ttl_seconds.

    Sets the key in Redis with a TTL.
    """
    client = redis_client.get_async_redis()
    key = f"idempotency:{idempotency_key}"
    await client.set(key, "1", ex=ttl_seconds)
