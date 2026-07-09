import pytest
import fakeredis
from fastapi.testclient import TestClient
import app.storage.redis_client
from app.main import app as fastapi_app

@pytest.fixture
def fake_redis(monkeypatch):
    """Fixture to mock Redis using fakeredis (both sync and async)."""
    # Reset globally cached clients to avoid leaking real connections
    app.storage.redis_client._redis_client = None
    app.storage.redis_client._async_redis_client = None
    
    server = fakeredis.FakeServer()
    sync_client = fakeredis.FakeRedis(server=server, decode_responses=True)
    async_client = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    monkeypatch.setattr(app.storage.redis_client, "get_redis", lambda: sync_client)
    monkeypatch.setattr(app.storage.redis_client, "get_async_redis", lambda: async_client)
    return sync_client

@pytest.fixture
def client(fake_redis):
    """Fixture for API test client, using the fake_redis fixture."""
    return TestClient(fastapi_app)
