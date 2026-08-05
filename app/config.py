"""Application configuration loaded from environment variables and .env file."""

import hmac
import ipaddress
from typing import Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.target_validation import (
    blocked_ip_reason,
    normalize_hostname,
    validate_delivery_target,
)


INVALID_PLACEHOLDER_SECRETS = {
    "secret",
    "change_me",
    "replace_me",
    "placeholder",
    "your_secret_here",
    "your_outbound_secret_here",
    "default",
    "123456",
    "topsecret",
}
VALID_ENVIRONMENTS = {"development", "test", "production"}
MIN_PRODUCTION_SECRET_LENGTH = 32


class Settings(BaseSettings):
    """Service configuration with environment-based overrides."""

    environment: str = "development"
    redis_url: str = "redis://localhost:6379/0"
    webhook_secret: str = ""
    outbound_webhook_secret: str = ""
    max_retry_attempts: int = 5
    idempotency_ttl_seconds: int = 86_400
    port: int = 8_000
    default_target_url: str | None = None
    allowed_target_hosts: list[str] = Field(default_factory=list)

    max_request_body_bytes: int = Field(default=1_048_576, gt=0, le=10_485_760)
    max_payload_bytes: int = Field(default=262_144, gt=0, le=10_485_760)
    max_event_type_length: int = Field(default=128, gt=0, le=256)
    max_target_url_length: int = Field(default=2_048, gt=0, le=8_192)

    stream_name: str = "webhook_stream"
    consumer_group: str = "webhook_workers"
    quarantine_queue: str = "webhook_quarantine"
    dlq_key: str = "webhook_dlq"
    delay_queue_key: str = "webhook_delay_queue"
    stream_claim_min_idle_ms: int = 60_000

    worker_heartbeat_key_prefix: str = Field(
        default="worker:heartbeat", min_length=1, max_length=128
    )
    worker_heartbeat_ttl_seconds: int = Field(default=10, gt=0, le=300)
    worker_heartbeat_interval_seconds: float = Field(default=3.0, gt=0, le=60)
    delivery_key_prefix: str = Field(default="delivery", min_length=1, max_length=128)
    cli_admin_key: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @field_validator("environment", mode="before")
    @classmethod
    def normalize_environment(cls, value: Any) -> str:
        normalized = str(value).strip().lower()
        if normalized not in VALID_ENVIRONMENTS:
            raise ValueError(
                f"environment must be one of: {', '.join(sorted(VALID_ENVIRONMENTS))}"
            )
        return normalized

    @field_validator("allowed_target_hosts")
    @classmethod
    def normalize_allowed_target_hosts(cls, values: list[str]) -> list[str]:
        normalized_hosts: list[str] = []
        for value in values:
            if "://" in value or "/" in value or "@" in value:
                raise ValueError("allowed_target_hosts entries must be hostnames only")
            host = normalize_hostname(value)
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address is not None and blocked_ip_reason(address):
                raise ValueError("allowed_target_hosts must not contain non-public IP addresses")
            if host not in normalized_hosts:
                normalized_hosts.append(host)
        return normalized_hosts

    @model_validator(mode="after")
    def validate_security_configuration(self) -> "Settings":
        if self.max_payload_bytes > self.max_request_body_bytes:
            raise ValueError("max_payload_bytes must not exceed max_request_body_bytes")
        if self.worker_heartbeat_interval_seconds >= self.worker_heartbeat_ttl_seconds:
            raise ValueError(
                "worker_heartbeat_interval_seconds must be shorter than "
                "worker_heartbeat_ttl_seconds"
            )

        if self.environment != "production":
            return self

        for field_name, secret in (
            ("webhook_secret", self.webhook_secret),
            ("outbound_webhook_secret", self.outbound_webhook_secret),
            ("cli_admin_key", self.cli_admin_key),
        ):
            normalized_secret = (secret or "").strip()
            lowered_secret = normalized_secret.lower()
            if (
                len(normalized_secret) < MIN_PRODUCTION_SECRET_LENGTH
                or lowered_secret in INVALID_PLACEHOLDER_SECRETS
                or lowered_secret.startswith(("your_", "change_me", "replace_me"))
            ):
                raise ValueError(
                    f"Production startup failed: {field_name} must be a non-placeholder "
                    f"secret of at least {MIN_PRODUCTION_SECRET_LENGTH} characters"
                )

        if hmac.compare_digest(self.webhook_secret, self.outbound_webhook_secret):
            raise ValueError(
                "Production startup failed: inbound and outbound secrets must be separate"
            )
        if not self.allowed_target_hosts:
            raise ValueError("Production startup failed: allowed_target_hosts must not be empty")

        if self.default_target_url:
            validate_delivery_target(
                self.default_target_url,
                environment=self.environment,
                allowed_target_hosts=self.allowed_target_hosts,
                resolve_dns=False,
                max_url_length=self.max_target_url_length,
            )

        return self


settings = Settings()
