"""Pydantic models for webhook events, delivery status, and delivery attempts."""

from pydantic import BaseModel, Field, field_validator
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from enum import Enum

from app.core.event_ids import validate_event_id

class IncomingWebhook(BaseModel):
    id: str = Field(..., description="Unique identifier for the webhook, used as idempotency key")
    event_type: str
    payload: Dict[str, Any]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    target_url: Optional[str] = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        return validate_event_id(v)

class DeliveryStatus(str, Enum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    RETRYING = "RETRYING"
    DEAD = "DEAD"

class DeliveryAttempt(BaseModel):
    target_url: str
    attempt_number: int = 0
    status: DeliveryStatus = DeliveryStatus.PENDING
    last_error: Optional[str] = None
    next_retry_at: Optional[datetime] = None
