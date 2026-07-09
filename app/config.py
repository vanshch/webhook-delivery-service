from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    redis_url: str = "redis://localhost:6379/0"
    webhook_secret: str = ""
    max_retry_attempts: int = 5
    idempotency_ttl_seconds: int = 86400
    port: int = 8000
    default_target_url: str = "http://httpbin.org/post"
    
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

settings = Settings()
