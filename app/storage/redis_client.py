"""Redis connection management — provides singleton sync and async clients."""

import redis
import redis.asyncio as aioredis
from app.config import settings

_redis_client = None
_async_redis_client = None

def get_redis():
    """Return a singleton synchronous Redis client."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client

def get_async_redis():
    """Return a singleton async Redis client."""
    global _async_redis_client
    if _async_redis_client is None:
        _async_redis_client = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
    return _async_redis_client
