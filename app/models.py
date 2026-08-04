"""Pydantic models for webhook events, delivery status, and delivery attempts."""

import json
from pydantic import BaseModel, Field, field_validator
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from enum import Enum

from app.core.event_ids import validate_event_id
from app.config import settings

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

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("event_type must be a non-empty string")
        if len(v) > settings.max_event_type_length:
            raise ValueError(f"event_type exceeds maximum length of {settings.max_event_type_length} characters")
        return v

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(v, dict):
            raise ValueError("payload must be a dictionary")
        try:
            serialized = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            if len(serialized.encode("utf-8")) > settings.max_payload_bytes:
                raise ValueError(f"Payload size exceeds maximum allowed limit of {settings.max_payload_bytes} bytes")
        except (TypeError, ValueError) as e:
            if "Payload size exceeds" in str(e):
                raise
            raise ValueError(f"Payload must be JSON serializable: {e}")
        return v

    @field_validator("target_url")
    @classmethod
    def validate_target_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not v:
            raise ValueError("target_url must not be empty when provided")
        from app.core.target_validation import validate_delivery_target

        return validate_delivery_target(
            v,
            environment=settings.environment,
            allowed_target_hosts=settings.allowed_target_hosts,
            resolve_dns=False,
            max_url_length=settings.max_target_url_length,
        )

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
