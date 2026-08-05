"""Delivery status inspection route."""

from fastapi import APIRouter, HTTPException, status
from loguru import logger
from app.core.event_ids import validate_event_id
from app.storage import redis_client
from app.core.delivery_state import get_delivery_state

router = APIRouter(prefix="/deliveries", tags=["deliveries"])

@router.get("/{event_id}")
async def get_delivery(event_id: str):
    """Retrieve delivery status and attempt details for a webhook event."""
    try:
        validate_event_id(event_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid event ID: {e}",
        )

    client = redis_client.get_async_redis()
    state = await get_delivery_state(client, event_id)
    if not state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Delivery status not found",
        )

    last_attempt_iso = (
        state.last_attempt_time.isoformat() if state.last_attempt_time else None
    )
    next_retry_iso = (
        state.next_retry_time.isoformat() if state.next_retry_time else None
    )
    final_delivery_iso = (
        state.final_delivery_time.isoformat() if state.final_delivery_time else None
    )

    return {
        "event_id": state.event_id,
        "status": state.status,
        "attempt_count": state.attempt_count,
        "last_attempt_time": last_attempt_iso,
        "last_error": state.last_error,
        "next_retry_time": next_retry_iso,
        "final_delivery_time": final_delivery_iso,
    }
