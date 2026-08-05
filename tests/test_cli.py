"""Tests for protected DLQ CLI tool commands and authentication."""

import pytest
import json
import argparse
from app.config import settings
from app.storage import redis_client
from app.models import IncomingWebhook
from scripts.dlq_cli import async_main, verify_admin_key

@pytest.mark.asyncio
async def test_cli_admin_key_verification(monkeypatch):
    monkeypatch.setattr(settings, "cli_admin_key", "secret123")
    assert verify_admin_key("secret123") is True
    assert verify_admin_key("wrongkey") is False
    assert verify_admin_key(None) is False

    monkeypatch.setattr(settings, "cli_admin_key", "")
    assert verify_admin_key(None) is False
    assert verify_admin_key("anything") is False

@pytest.mark.asyncio
async def test_cli_list_inspect_and_replay(fake_redis, monkeypatch, capsys):
    async_redis = redis_client.get_async_redis()
    monkeypatch.setattr(settings, "cli_admin_key", "test-admin-key")

    event_id = "evt_cli_1"
    webhook_data = IncomingWebhook(
        id=event_id,
        event_type="user.created",
        payload={"id": 123},
        target_url="https://example.com/target",
    )
    dlq_item = {
        "webhook": webhook_data.model_dump(mode="json"),
        "last_attempt": 5,
        "failed_at": 1700000000.0,
        "target_url": "https://example.com/target",
        "last_error": "Connection timeout",
    }
    await async_redis.lpush(settings.dlq_key, json.dumps(dlq_item))

    # Test list command
    args_list = argparse.Namespace(command="list", admin_key="test-admin-key", event_id=None, event_ids=None)
    code_list = await async_main(args_list)
    assert code_list == 0
    captured_list = capsys.readouterr().out
    assert "Total DLQ events: 1" in captured_list
    assert event_id in captured_list

    # Test inspect command
    args_inspect = argparse.Namespace(command="inspect", admin_key="test-admin-key", event_id=event_id, event_ids=None)
    code_inspect = await async_main(args_inspect)
    assert code_inspect == 0
    captured_inspect = capsys.readouterr().out
    assert event_id in captured_inspect

    # Test replay command
    args_replay = argparse.Namespace(command="replay", admin_key="test-admin-key", event_id=event_id, event_ids=None)
    code_replay = await async_main(args_replay)
    assert code_replay == 0
    captured_replay = capsys.readouterr().out
    assert f"Successfully replayed DLQ event '{event_id}'." in captured_replay

    # Confirm DLQ is now empty and stream has 1 item
    assert await async_redis.llen(settings.dlq_key) == 0
    assert await async_redis.xlen(settings.stream_name) == 1
