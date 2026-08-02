"""Application configuration loaded from environment variables and .env file."""

from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """Service configuration with env-based overrides. See .env.example for defaults."""
    redis_url: str = "redis://localhost:6379/0"
    webhook_secret: str = ""
    outbound_webhook_secret: str = ""
    max_retry_attempts: int = 5
    idempotency_ttl_seconds: int = 86400
    port: int = 8000
    default_target_url: str = "http://httpbin.org/post"
    
    stream_name: str = "webhook_stream"
    consumer_group: str = "webhook_workers"
    quarantine_queue: str = "webhook_quarantine"
    dlq_key: str = "webhook_dlq"
    delay_queue_key: str = "webhook_delay_queue"
    stream_claim_min_idle_ms: int = 60000

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

settings = Settings()
