"""Health, liveness, and readiness probe routes."""

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from loguru import logger
from app.storage import redis_client
from app.config import settings

router = APIRouter(tags=["health"])

@router.get("/health")
def health_check():
    """Return process status."""
    return {"status": "ok"}

@router.get("/livez")
def liveness_probe():
    """Process liveness probe."""
    return {"status": "ok"}

@router.get("/readyz")
async def readiness_probe():
    """Dependency readiness probe checking Redis and worker heartbeat."""
    client = redis_client.get_async_redis()

    # 1. Redis connectivity check
    try:
        ping_ok = await client.ping()
        if not ping_ok:
            logger.warning("Readiness probe failed: Redis ping returned False")
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "unhealthy", "redis": "unavailable"},
            )
    except Exception as exc:
        logger.warning(f"Readiness probe failed: Redis ping error: {exc}")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "redis": "unavailable"},
        )

    # 2. Worker heartbeat check
    try:
        heartbeat_found = False
        async for _ in client.scan_iter(
            match=f"{settings.worker_heartbeat_key_prefix}:*", count=10
        ):
            heartbeat_found = True
            break
        if not heartbeat_found:
            logger.warning("Readiness probe failed: No active worker heartbeat found")
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "unhealthy", "worker_heartbeat": "stale"},
            )
    except Exception as exc:
        logger.warning(f"Readiness probe failed during worker heartbeat check: {exc}")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "worker_heartbeat": "stale"},
        )

    return {"status": "ok", "redis": "connected", "worker_heartbeat": "healthy"}
