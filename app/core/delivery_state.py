"""Delivery state persistence and retrieval utilities backed by Redis."""

import json
import inspect
from typing import Optional
from datetime import datetime, timezone
from loguru import logger
from app.config import settings
from app.core.event_ids import validate_event_id
from app.models import DeliveryState

async def _maybe_await(res):
    if inspect.isawaitable(res):
        return await res
    return res

def get_delivery_key(event_id: str) -> str:
    """Return the Redis key for a delivery state record."""
    validate_event_id(event_id)
    return f"{settings.delivery_key_prefix}:{event_id}"

async def get_delivery_state(redis_conn, event_id: str) -> Optional[DeliveryState]:
    """Retrieve the current delivery state for an event ID."""
    key = get_delivery_key(event_id)
    raw = await _maybe_await(redis_conn.get(key))
    if not raw:
        return None
    try:
        return DeliveryState.model_validate_json(raw)
    except Exception as e:
        logger.error(f"Failed to parse delivery state for event {event_id}: {e}")
        return None

async def save_delivery_state(
    redis_conn,
    state: DeliveryState,
    ttl_seconds: Optional[int] = None,
) -> None:
    """Persist a delivery state transition to Redis.

    Must be called before acknowledging or removing the source message to preserve
    the at-least-once guarantee.
    """
    key = get_delivery_key(state.event_id)
    ttl = ttl_seconds if ttl_seconds is not None else settings.idempotency_ttl_seconds
    data_json = state.model_dump_json()
    await _maybe_await(redis_conn.set(key, data_json, ex=ttl))
