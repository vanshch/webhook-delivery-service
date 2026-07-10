"""Pydantic models for webhook events, delivery status, and delivery attempts."""

from pydantic import BaseModel, Field
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from enum import Enum

class IncomingWebhook(BaseModel):
    id: str = Field(..., description="Unique identifier for the webhook, used as idempotency key")
    event_type: str
    payload: Dict[str, Any]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    target_url: Optional[str] = None

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
