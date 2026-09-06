import hashlib
import hmac
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.config import Settings, settings
from app.core.security import verify_signature
from app.core.target_validation import validate_delivery_target
from app.models import IncomingWebhook
from app.routes.webhooks import read_limited_body
from app.workers import delivery_worker


def test_verify_signature_valid():
    secret = "my_super_secret"
    payload = b'{"event": "test"}'
    mac = hmac.new(secret.encode("utf-8"), msg=payload, digestmod=hashlib.sha256)

    assert verify_signature(payload, f"sha256={mac.hexdigest()}", secret) is True


def test_verify_signature_invalid():
    secret = "my_super_secret"
    payload = b'{"event": "test"}'

    assert verify_signature(payload, "sha256=invalidhash", secret) is False
    assert verify_signature(payload, "invalidhash", secret) is False


def test_verify_signature_tampered_payload():
    secret = "my_super_secret"
    original_payload = b'{"event": "test"}'
    tampered_payload = b'{"event": "hacked"}'
    mac = hmac.new(
        secret.encode("utf-8"),
        msg=original_payload,
        digestmod=hashlib.sha256,
    )

    assert verify_signature(
        tampered_payload,
        f"sha256={mac.hexdigest()}",
        secret,
    ) is False


def test_verify_signature_empty_secret():
    payload = b'{"event": "test"}'
    assert verify_signature(payload, "sha256=somehash", "") is False
    assert verify_signature(payload, "sha256=somehash", None) is False


def test_verify_signature_no_prefix():
    secret = "my_super_secret"
    payload = b'{"event": "test"}'
    mac = hmac.new(secret.encode("utf-8"), msg=payload, digestmod=hashlib.sha256)

    assert verify_signature(payload, mac.hexdigest(), secret) is True


def test_api_invalid_signature_returns_401(client):
    payload = b'{"id": "123", "event_type": "test", "payload": {}}'
    response = client.post(
        "/webhooks",
        content=payload,
        headers={"x-signature": "sha256=wrong_sig"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid signature"}


def test_api_missing_signature_returns_401(client):
    payload = b'{"id": "123", "event_type": "test", "payload": {}}'
    response = client.post("/webhooks", content=payload)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid signature"}


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://127.0.0.1/webhook", "loopback address"),
        ("http://[::1]/webhook", "loopback address"),
        ("http://10.0.0.1/webhook", "private-network address"),
        ("http://172.16.0.1/webhook", "private-network address"),
        ("http://192.168.1.1/webhook", "private-network address"),
        ("http://169.254.169.254/latest/meta-data", "cloud metadata address"),
        ("http://100.100.100.200/latest/meta-data", "cloud metadata address"),
        ("http://[fd00:ec2::254]/latest/meta-data", "cloud metadata address"),
        ("http://0.0.0.0/webhook", "unspecified address"),
        ("http://224.0.0.1/webhook", "multicast address"),
    ],
)
def test_reject_non_public_ip_literals(url, reason):
    with pytest.raises(ValueError, match=reason):
        validate_delivery_target(url)


def test_reject_hostname_when_any_dns_answer_is_private():
    with pytest.raises(ValueError, match="private-network address"):
        validate_delivery_target(
            "https://receiver.example/webhook",
            resolver=lambda hostname, port: ["93.184.216.34", "10.0.0.8"],
        )


def test_unresolved_hostname_fails_closed():
    def unavailable_dns(hostname, port):
        raise ValueError("DNS unavailable")

    with pytest.raises(ValueError, match="DNS unavailable"):
        validate_delivery_target(
            "https://receiver.example/webhook",
            resolver=unavailable_dns,
        )


@pytest.mark.parametrize("scheme", ["file", "ftp", "gopher", "dict"])
def test_reject_non_http_schemes(scheme):
    with pytest.raises(ValueError, match="Unsupported URL scheme"):
        validate_delivery_target(f"{scheme}://example.com/resource")


@pytest.mark.parametrize(
    "url",
    [
        "http://user:password@example.com/webhook",
        "https://admin@example.com/webhook",
    ],
)
def test_reject_credentials_in_url(url):
    with pytest.raises(ValueError, match="must not contain credentials"):
        validate_delivery_target(url)


def test_require_https_in_production():
    with pytest.raises(ValueError, match="HTTPS is required in production"):
        validate_delivery_target(
            "http://example.com/webhook",
            environment="production",
            allowed_target_hosts=["example.com"],
        )


def test_production_requires_non_empty_allowlist():
    with pytest.raises(ValueError, match="non-empty target host allowlist"):
        validate_delivery_target(
            "https://example.com/webhook",
            environment="production",
        )


def test_accept_valid_allowlisted_https_destination():
    url = "https://example.com/webhooks"

    assert validate_delivery_target(
        url,
        environment="production",
        allowed_target_hosts=["EXAMPLE.COM."],
    ) == url

    with pytest.raises(ValueError, match="not allowlisted"):
        validate_delivery_target(
            url,
            environment="production",
            allowed_target_hosts=["other.example.com"],
        )


@pytest.mark.asyncio
async def test_redirect_response_is_not_followed():
    requests = []

    async def redirect_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status_code=302,
            headers={"Location": "http://127.0.0.1/admin"},
        )

    transport = httpx.MockTransport(redirect_handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        webhook = IncomingWebhook(
            id="redirect-test",
            event_type="test",
            payload={"test": 1},
            target_url="https://example.com/webhook",
        )
        success = await delivery_worker.deliver_webhook(
            webhook,
            "https://example.com/webhook",
            client,
        )

    assert success is False
    assert len(requests) == 1
    assert requests[0].url.host == "example.com"


def production_settings(**overrides):
    values = {
        "environment": "production",
        "webhook_secret": "i" * 32,
        "outbound_webhook_secret": "o" * 32,
        "cli_admin_key": "a" * 32,
        "allowed_target_hosts": ["receiver.example"],
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"webhook_secret": ""}, "webhook_secret"),
        ({"webhook_secret": "change_me"}, "webhook_secret"),
        ({"outbound_webhook_secret": "your_outbound_secret_here"}, "outbound_webhook_secret"),
        ({"cli_admin_key": ""}, "cli_admin_key"),
        (
            {"webhook_secret": "s" * 32, "outbound_webhook_secret": "s" * 32},
            "must be separate",
        ),
        ({"allowed_target_hosts": []}, "must not be empty"),
    ],
)
def test_production_startup_fails_closed(overrides, message):
    with pytest.raises(ValidationError, match=message):
        production_settings(**overrides)


def test_valid_production_configuration_succeeds():
    configured = production_settings(
        default_target_url="https://receiver.example/webhooks"
    )

    assert configured.environment == "production"
    assert configured.allowed_target_hosts == ["receiver.example"]


def test_unknown_environment_cannot_bypass_production_rules():
    with pytest.raises(ValidationError, match="environment must be one of"):
        Settings(_env_file=None, environment="prodution")


def test_malformed_allowlist_entry_fails_closed():
    with pytest.raises(ValidationError, match="hostnames only"):
        Settings(_env_file=None, allowed_target_hosts=["https://receiver.example/path"])


def test_request_body_size_limit_exceeded(client, monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret", "test_secret")
    huge_body = b"a" * (settings.max_request_body_bytes + 1)
    mac = hmac.new(b"test_secret", msg=huge_body, digestmod=hashlib.sha256)

    response = client.post(
        "/webhooks",
        content=huge_body,
        headers={"x-signature": f"sha256={mac.hexdigest()}"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body exceeds maximum allowed size"}


@pytest.mark.asyncio
async def test_streamed_request_body_stops_at_limit():
    chunks = iter(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"56", "more_body": False},
        ]
    )

    async def receive():
        return next(chunks)

    request = Request(
        {"type": "http", "method": "POST", "path": "/", "headers": []},
        receive,
    )

    with pytest.raises(HTTPException) as exc_info:
        await read_limited_body(request, limit=5)

    assert exc_info.value.status_code == 413


def test_payload_size_limit_exceeded():
    large_payload = {"key": "x" * (settings.max_payload_bytes + 1)}

    with pytest.raises(ValidationError, match="Payload size exceeds"):
        IncomingWebhook(id="large-payload-id", event_type="test", payload=large_payload)


def test_event_type_length_and_whitespace_are_rejected():
    with pytest.raises(ValidationError, match="event_type exceeds"):
        IncomingWebhook(
            id="long-event-id",
            event_type="e" * (settings.max_event_type_length + 1),
            payload={},
        )
    with pytest.raises(ValidationError, match="non-empty"):
        IncomingWebhook(id="empty-event-id", event_type="   ", payload={})


def test_target_url_length_is_limited():
    with pytest.raises(ValidationError, match="Target URL exceeds maximum length"):
        IncomingWebhook(
            id="long-target-id",
            event_type="test",
            payload={},
            target_url="https://example.com/" + "x" * settings.max_target_url_length,
        )


def test_model_rejects_private_target_without_dns_lookup():
    with pytest.raises(ValidationError, match="loopback address"):
        IncomingWebhook(
            id="private-target-id",
            event_type="test",
            payload={},
            target_url="http://127.0.0.1/webhook",
        )


def test_model_rejects_empty_target_when_provided():
    with pytest.raises(ValidationError, match="must not be empty"):
        IncomingWebhook(
            id="empty-target-id",
            event_type="test",
            payload={},
            target_url="",
        )


@pytest.mark.asyncio
async def test_worker_quarantines_hostname_resolving_to_private_ip(
    fake_redis,
    monkeypatch,
):
    from app.core import target_validation
    from app.storage.redis_client import get_async_redis

    monkeypatch.setattr(
        target_validation,
        "resolve_hostname",
        lambda hostname, port: ["10.10.0.5"],
    )
    async_redis = get_async_redis()
    client = AsyncMock(spec=httpx.AsyncClient)
    webhook = IncomingWebhook(
        id="private-dns-target",
        event_type="test",
        payload={},
        target_url="https://receiver.example/webhook",
    )

    await delivery_worker.process_job(
        async_redis,
        webhook.model_dump_json(),
        client,
    )

    assert await async_redis.llen(settings.quarantine_queue) == 1
    assert await async_redis.zcard(settings.delay_queue_key) == 0
    client.post.assert_not_awaited()
