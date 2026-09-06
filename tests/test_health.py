"""Tests for health, liveness, and readiness endpoints."""

import pytest
from unittest.mock import AsyncMock
from fastapi import status
from app.config import settings
from app.storage import redis_client
from app.workers.delivery_worker import record_heartbeat

def test_health(client):
    response = client.get("/health")
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}

def test_livez(client):
    response = client.get("/livez")
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}

@pytest.mark.asyncio
async def test_readyz_fails_when_redis_unavailable(client, monkeypatch):
    from app.storage import redis_client
    mock_async_redis = AsyncMock()
    mock_async_redis.ping.side_effect = ConnectionError("Redis connection refused")
    monkeypatch.setattr(redis_client, "get_async_redis", lambda: mock_async_redis)

    response = client.get("/readyz")
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json()["status"] == "unhealthy"
    assert response.json()["redis"] == "unavailable"

@pytest.mark.asyncio
async def test_readyz_fails_when_worker_heartbeat_stale(client, fake_redis):
    # fake_redis is empty, so no worker heartbeat exists
    response = client.get("/readyz")
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    res_data = response.json()
    assert res_data["status"] == "unhealthy"
    assert res_data["worker_heartbeat"] == "stale"

@pytest.mark.asyncio
async def test_readyz_succeeds_when_healthy(client, fake_redis):
    # Set active worker heartbeat
    fake_redis.set(f"{settings.worker_heartbeat_key_prefix}:w1", "1700000000", ex=10)
    response = client.get("/readyz")
    assert response.status_code == status.HTTP_200_OK
    res_data = response.json()
    assert res_data["status"] == "ok"
    assert res_data["redis"] == "connected"
    assert res_data["worker_heartbeat"] == "healthy"


@pytest.mark.asyncio
async def test_worker_heartbeat_has_expiry(fake_redis):
    async_redis = redis_client.get_async_redis()
    worker_id = "worker-heartbeat-test"

    await record_heartbeat(async_redis, worker_id)

    ttl = await async_redis.ttl(
        f"{settings.worker_heartbeat_key_prefix}:{worker_id}"
    )
    assert 0 < ttl <= settings.worker_heartbeat_ttl_seconds
