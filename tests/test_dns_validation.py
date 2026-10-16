"""Tests for bounded DNS validation and transient failure retries.

Verifies Section 9 of COMMIT_ROADMAP.md:
- Strict deadline per validation lookup (default 2.0s)
- Bounded/cancellable resolver execution without thread starvation or accumulating tasks
- Transient DNS failures retried with backoff and eventual DLQ exhaustion (never quarantined)
- Forbidden/invalid targets fail closed and quarantine
- Outbound HTTP POST prevented when validation fails
"""

import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from app.config import Settings, settings
from app.core.target_validation import (
    DEFAULT_DNS_LOOKUP_TIMEOUT_SECONDS,
    InvalidTargetError,
    TargetValidationError,
    TransientDNSError,
    resolve_hostname_async,
    validate_delivery_target,
    validate_delivery_target_async,
)
from app.models import DeliveryState, DeliveryStatus, IncomingWebhook
from app.workers.delivery_worker import (
    deliver_webhook,
    process_job,
    promote_delayed_jobs,
)


def test_transient_dns_error_hierarchy():
    """Verify TransientDNSError and InvalidTargetError inherit from TargetValidationError and ValueError."""
    assert issubclass(TransientDNSError, TargetValidationError)
    assert issubclass(TransientDNSError, ValueError)
    assert issubclass(InvalidTargetError, TargetValidationError)
    assert issubclass(InvalidTargetError, ValueError)


@pytest.mark.asyncio
async def test_controlled_stalled_resolver_async_reaches_deadline_and_cancels():
    """An async resolver that hangs beyond deadline must be cancelled and release capacity."""
    cancelled = False

    async def hanging_resolver(hostname: str, port: int):
        nonlocal cancelled
        try:
            await asyncio.sleep(10.0)
            return ["93.184.216.34"]
        except asyncio.CancelledError:
            cancelled = True
            raise

    start = time.perf_counter()
    with pytest.raises(TransientDNSError) as exc_info:
        await resolve_hostname_async(
            "hung.example.com",
            443,
            dns_timeout=0.05,
            resolver=hanging_resolver,
        )
    elapsed = time.perf_counter() - start

    assert elapsed < 0.5, f"Lookup took {elapsed}s, expected deadline ~0.05s"
    assert "timed out" in str(exc_info.value)
    assert cancelled, "Async resolver coroutine was not cancelled upon timeout"


@pytest.mark.asyncio
async def test_controlled_stalled_resolver_sync_signals_cancellation():
    """A sync resolver that supports cancel_event must receive cancellation signal on timeout."""
    cancel_received = threading.Event()

    def sync_hanging_resolver(hostname: str, port: int, cancel_event: threading.Event = None, timeout: float = None):
        if cancel_event:
            # Wait for cancellation signal up to 2 seconds
            if cancel_event.wait(timeout=2.0):
                cancel_received.set()
                return []
        return ["93.184.216.34"]

    start = time.perf_counter()
    with pytest.raises(TransientDNSError) as exc_info:
        await resolve_hostname_async(
            "sync-hung.example.com",
            443,
            dns_timeout=0.05,
            resolver=sync_hanging_resolver,
        )
    elapsed = time.perf_counter() - start

    assert elapsed < 0.5
    assert "timed out" in str(exc_info.value)
    # Ensure cancel signal was sent to the sync thread
    assert cancel_received.wait(timeout=1.0), "Sync resolver did not receive cancellation event"


@pytest.mark.asyncio
async def test_unrelated_jobs_continue_after_stalled_resolver():
    """Stalled DNS lookups must not exhaust the bounded executor or block subsequent lookups."""
    def hung_resolver(hostname: str, port: int, cancel_event: threading.Event = None):
        if cancel_event:
            cancel_event.wait(timeout=0.2)
        return []

    # Run several stalled lookups
    for _ in range(5):
        with pytest.raises(TransientDNSError):
            await resolve_hostname_async(
                "slow.example.com",
                443,
                dns_timeout=0.02,
                resolver=hung_resolver,
            )

    # Subsequent valid lookup immediately succeeds
    fast_resolver = lambda h, p: ["93.184.216.34"]
    result = await resolve_hostname_async(
        "fast.example.com",
        443,
        dns_timeout=1.0,
        resolver=fast_resolver,
    )
    assert result == ["93.184.216.34"]


def test_mixed_public_and_forbidden_addresses_fails_closed():
    """If DNS returns both a public IP and a private/forbidden IP, fail closed with InvalidTargetError."""
    mixed_resolver = lambda h, p: ["93.184.216.34", "10.0.0.1"]

    with pytest.raises(InvalidTargetError) as exc_info:
        validate_delivery_target(
            "http://dual-homed.example.com/webhook",
            resolver=mixed_resolver,
        )
    assert "forbidden private-network address" in str(exc_info.value)


@pytest.mark.asyncio
async def test_transient_dns_failure_retries_and_succeeds_on_next_attempt():
    """Transient DNS failure consumes attempt budget and retries; succeeds on second attempt."""
    redis = FakeAsyncRedis()
    webhook_id = "dns-transient-retry-test"
    job_dict = {
        "id": webhook_id,
        "event_type": "payment.success",
        "payload": {"amount": 100},
        "target_url": "https://api.external.com/webhook",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    job_json = json.dumps(job_dict)

    mock_client = AsyncMock(spec=httpx.AsyncClient)

    with patch("app.workers.delivery_worker.validate_delivery_target_async", side_effect=TransientDNSError("Temporary DNS lookup failure")):
        await process_job(
            redis,
            job_json,
            mock_client,
            stream_msg_id="1-0",
        )

    # Verify attempt 1 failure handled as retryable
    attempt_val = await redis.get(f"attempt:{webhook_id}")
    assert attempt_val == b"1" or attempt_val == "1"

    # State should be RETRYING
    state_json = await redis.get(f"delivery:{webhook_id}")
    assert state_json is not None
    state_data = json.loads(state_json)
    assert state_data["status"] == DeliveryStatus.RETRYING.value
    assert state_data["attempt_count"] == 1
    assert "DNS resolution failed" in state_data["last_error"]

    # Quarantine queue must be empty
    quarantine_len = await redis.llen(settings.quarantine_queue)
    assert quarantine_len == 0

    # Delay queue should have 1 item
    delay_count = await redis.zcard(settings.delay_queue_key)
    assert delay_count == 1

    # HTTP client was never called
    assert mock_client.post.call_count == 0

    # Second attempt: DNS resolves successfully and HTTP delivery succeeds
    mock_response = MagicMock(status_code=200)
    mock_client.post.return_value = mock_response

    # Promote delayed job
    # Override timestamp score to ensure immediate promotion
    await redis.zadd(settings.delay_queue_key, {job_json: 0})
    promoted = await promote_delayed_jobs(redis)
    assert promoted == 1

    # Deliver attempt 2 with successful target validation
    with patch("app.workers.delivery_worker.validate_delivery_target_async", return_value="https://api.external.com/webhook"):
        await process_job(
            redis,
            job_json,
            mock_client,
            stream_msg_id="2-0",
        )

    # Verify successful delivery state
    state_json = await redis.get(f"delivery:{webhook_id}")
    state_data = json.loads(state_json)
    assert state_data["status"] == DeliveryStatus.DELIVERED.value
    assert state_data["attempt_count"] == 2

    # Attempt key is deleted
    assert await redis.get(f"attempt:{webhook_id}") is None
    # HTTP post was called exactly once on successful validation
    assert mock_client.post.call_count == 1


@pytest.mark.asyncio
async def test_repeated_transient_dns_failures_exhaust_budget_to_dlq():
    """Repeated transient DNS failures exhaust max_retry_attempts into DLQ, not quarantine."""
    redis = FakeAsyncRedis()
    webhook_id = "dns-exhaust-dlq-test"
    job_dict = {
        "id": webhook_id,
        "event_type": "order.completed",
        "payload": {"order_id": 42},
        "target_url": "https://flaky-dns.example.com/hooks",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    job_json = json.dumps(job_dict)
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    # Execute attempts 1 through 5 (where max_retry_attempts == 5)
    for attempt in range(1, settings.max_retry_attempts + 1):
        with patch("app.workers.delivery_worker.validate_delivery_target_async", side_effect=TransientDNSError("Name or service not known")):
            await process_job(
                redis,
                job_json,
                mock_client,
                stream_msg_id=f"{attempt}-0",
            )

    # Verify DLQ received the exhausted job
    dlq_len = await redis.llen(settings.dlq_key)
    assert dlq_len == 1
    dlq_raw = await redis.lpop(settings.dlq_key)
    dlq_item = json.loads(dlq_raw)
    assert dlq_item["last_attempt"] == settings.max_retry_attempts
    assert "DNS resolution failed" in dlq_item["last_error"]

    # Delivery state is DEAD
    state_json = await redis.get(f"delivery:{webhook_id}")
    state_data = json.loads(state_json)
    assert state_data["status"] == DeliveryStatus.DEAD.value
    assert state_data["attempt_count"] == settings.max_retry_attempts

    # Quarantine queue must remain empty
    quarantine_len = await redis.llen(settings.quarantine_queue)
    assert quarantine_len == 0

    # No HTTP requests sent
    assert mock_client.post.call_count == 0


@pytest.mark.asyncio
async def test_delivery_time_rebinding_to_private_ip_is_quarantined():
    """If target hostname rebinds to a private IP at delivery time, job is quarantined without HTTP send."""
    redis = FakeAsyncRedis()
    webhook_id = "dns-rebinding-test"
    job_dict = {
        "id": webhook_id,
        "event_type": "user.created",
        "payload": {"user_id": "u123"},
        "target_url": "https://rebind.attacker.com/webhook",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    job_json = json.dumps(job_dict)
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    # Initial validation in process_job succeeds (returns URL),
    # but delivery-time validation in deliver_webhook detects private IP!
    async def mock_validate(url, **kwargs):
        # Inspect call stack: if called from deliver_webhook, raise InvalidTargetError
        import inspect
        stack = inspect.stack()
        callers = [f.function for f in stack]
        if "deliver_webhook" in callers:
            raise InvalidTargetError("Target URL points to a forbidden private-network address: 127.0.0.1")
        return url

    with patch("app.workers.delivery_worker.validate_delivery_target_async", side_effect=mock_validate):
        await process_job(
            redis,
            job_json,
            mock_client,
            stream_msg_id="1-0",
        )

    # HTTP client was never invoked
    assert mock_client.post.call_count == 0

    # Job is quarantined
    quarantine_len = await redis.llen(settings.quarantine_queue)
    assert quarantine_len == 1
    item = json.loads(await redis.lpop(settings.quarantine_queue))
    assert "Invalid target URL" in item["error"]
    assert "private-network address" in item["error"]

    # DLQ and delay queue remain empty
    assert await redis.llen(settings.dlq_key) == 0
    assert await redis.zcard(settings.delay_queue_key) == 0


def test_validate_delivery_target_sync_wraps_arbitrary_exceptions():
    """Verify synchronous validate_delivery_target wraps arbitrary resolver exceptions as TransientDNSError."""
    def buggy_resolver(hostname: str, port: int):
        raise RuntimeError("Unexpected resolver failure")

    with pytest.raises(TransientDNSError) as exc_info:
        validate_delivery_target(
            "http://example.com/webhook",
            resolver=buggy_resolver,
        )
    assert "DNS resolution failed" in str(exc_info.value)
    assert "Unexpected resolver failure" in str(exc_info.value)


def test_dns_lookup_timeout_configuration():
    """Verify dns_lookup_timeout_seconds setting defaults to 2.0s and validates bounds."""
    default_cfg = Settings(_env_file=None)
    assert default_cfg.dns_lookup_timeout_seconds == 2.0

    custom_cfg = Settings(_env_file=None, dns_lookup_timeout_seconds=5.0)
    assert custom_cfg.dns_lookup_timeout_seconds == 5.0
