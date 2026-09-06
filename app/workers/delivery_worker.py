"""Async delivery worker that processes webhooks from Redis Streams.

Consumes jobs using Redis Stream consumer groups, attempts HTTP delivery
with exponential backoff retries, acknowledges only after delivery/persistence,
quarantines malformed jobs, and reclaims stale pending entries after worker crashes.
"""

import hmac
import hashlib
import time
import json
import uuid
import signal
import httpx
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Set, List, Tuple
from loguru import logger
from redis.exceptions import ResponseError
from app.storage import redis_client
from app.core import retry
from app.config import settings
from app.models import IncomingWebhook, DeliveryState, DeliveryStatus
from app.core.target_validation import validate_delivery_target
from app.core.delivery_state import save_delivery_state

import inspect

async def _maybe_await(res):
    if inspect.isawaitable(res):
        return await res
    return res

async def record_heartbeat(redis_conn, worker_id: str) -> None:
    """Write a worker heartbeat key to Redis with configured TTL."""
    key = f"{settings.worker_heartbeat_key_prefix}:{worker_id}"
    await _maybe_await(redis_conn.set(key, str(time.time()), ex=settings.worker_heartbeat_ttl_seconds))

async def heartbeat_loop(
    redis_conn,
    worker_id: str,
    shutdown_event: asyncio.Event,
):
    """Periodically publish worker heartbeat while the worker process is alive."""
    key = f"{settings.worker_heartbeat_key_prefix}:{worker_id}"
    while not shutdown_event.is_set():
        try:
            await record_heartbeat(redis_conn, worker_id)
        except Exception as e:
            logger.warning(f"Failed to record worker heartbeat for {worker_id}: {e}")
        try:
            await asyncio.sleep(settings.worker_heartbeat_interval_seconds)
        except asyncio.CancelledError:
            break
    try:
        await _maybe_await(redis_conn.delete(key))
    except Exception:
        pass

# Lua script for atomic promotion of delayed jobs from ZSET to Redis Stream
PROMOTE_DELAYED_LUA = """
local delay_queue = KEYS[1]
local stream_name = KEYS[2]
local max_score = ARGV[1]
local limit = ARGV[2] or '100'

local due_jobs = redis.call('ZRANGEBYSCORE', delay_queue, '-inf', max_score, 'LIMIT', 0, tonumber(limit))
local moved = 0
for _, job_json in ipairs(due_jobs) do
    -- Write the destination first. Redis scripts do not roll back earlier
    -- writes after a runtime error, so removing first could lose the job if
    -- XADD fails. A later ZREM failure can only create a duplicate, which is
    -- compatible with the service's at-least-once guarantee.
    redis.call('XADD', stream_name, '*', 'payload', job_json)
    redis.call('ZREM', delay_queue, job_json)
    moved = moved + 1
end
return moved
"""

# Lua script for atomic acknowledgement and deletion of completed stream messages
ACK_AND_DELETE_LUA = """
local stream_name = KEYS[1]
local consumer_group = ARGV[1]
local msg_id = ARGV[2]

local acked = redis.call('XACK', stream_name, consumer_group, msg_id)
if acked == 1 then
    return redis.call('XDEL', stream_name, msg_id)
end
return 0
"""

async def acknowledge_and_delete(
    redis_conn,
    stream_name: str,
    consumer_group: str,
    msg_id: str,
) -> int:
    """Atomically acknowledge and delete a stream message if acknowledged."""
    if not msg_id:
        return 0
    res = await _maybe_await(
        redis_conn.eval(ACK_AND_DELETE_LUA, 1, stream_name, consumer_group, str(msg_id))
    )
    return int(res) if res is not None else 0

def generate_worker_id() -> str:
    """Generate a stable unique worker identifier."""
    return f"worker-{uuid.uuid4().hex[:8]}"

async def ensure_consumer_group(
    redis_conn,
    stream_name: str = settings.stream_name,
    consumer_group: str = settings.consumer_group,
) -> None:
    """Ensure the Redis Stream consumer group exists."""
    try:
        await redis_conn.xgroup_create(stream_name, consumer_group, id="0", mkstream=True)
        logger.info(f"Created consumer group '{consumer_group}' on stream '{stream_name}'")
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            logger.error(f"Failed to create consumer group '{consumer_group}': {e}")
            raise

def extract_job_json(fields: dict) -> Optional[str]:
    """Extract job JSON string from stream message fields dictionary."""
    if not isinstance(fields, dict):
        return None
    if "payload" in fields:
        return fields["payload"]
    if "data" in fields:
        return fields["data"]
    if "job" in fields:
        return fields["job"]
    if len(fields) == 1:
        return next(iter(fields.values()))
    return None

async def deliver_webhook(
    webhook: IncomingWebhook,
    target_url: str,
    client: httpx.AsyncClient,
) -> bool:
    """Attempt HTTP POST delivery of the webhook to target_url.
    Returns True if successful, False otherwise.
    """
    try:
        validated_url = await asyncio.to_thread(
            validate_delivery_target,
            target_url,
            environment=settings.environment,
            allowed_target_hosts=settings.allowed_target_hosts,
            max_url_length=settings.max_target_url_length,
        )
    except ValueError as e:
        logger.error(f"Target URL validation failed for webhook {webhook.id}: {e}")
        return False

    try:
        payload = {
            "id": webhook.id,
            "event_type": webhook.event_type,
            "payload": webhook.payload,
            "timestamp": webhook.timestamp.isoformat() if webhook.timestamp else None
        }
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        if settings.outbound_webhook_secret:
            mac = hmac.new(
                settings.outbound_webhook_secret.encode("utf-8"),
                msg=body,
                digestmod=hashlib.sha256,
            )
            headers["x-signature"] = f"sha256={mac.hexdigest()}"

        response = await client.post(validated_url, content=body, headers=headers, follow_redirects=False)
        if 200 <= response.status_code < 300:
            logger.info(f"Webhook {webhook.id} successfully delivered to {validated_url}")
            return True
        else:
            logger.warning(f"Webhook {webhook.id} delivery to {validated_url} returned status {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"Webhook {webhook.id} delivery failed: {e}")
        return False

async def process_job(
    redis_conn,
    job_json: str,
    client: httpx.AsyncClient,
    stream_msg_id: Optional[str] = None,
    stream_name: str = settings.stream_name,
    consumer_group: str = settings.consumer_group,
) -> None:
    """Parse job JSON, attempt delivery, and handle success/retry/DLQ/quarantine.

    Acknowledges stream message ONLY AFTER successful delivery, persisted retry state,
    DLQ persistence, or quarantine persistence.
    """
    # 1. Parse job payload or quarantine malformed job
    try:
        data = json.loads(job_json)
        webhook = IncomingWebhook(**data)
    except Exception as e:
        logger.error(f"Failed to parse job JSON: {e}. Moving job to quarantine.")
        quarantine_item = {
            "job": job_json,
            "error": str(e),
            "quarantined_at": time.time(),
        }
        await _maybe_await(redis_conn.lpush(settings.quarantine_queue, json.dumps(quarantine_item)))
        if stream_msg_id:
            await acknowledge_and_delete(redis_conn, stream_name, consumer_group, stream_msg_id)
        return

    # 2. Validate target URL
    raw_target_url = webhook.target_url or settings.default_target_url
    if not raw_target_url:
        logger.error(f"No target URL specified for webhook {webhook.id}. Moving to quarantine.")
        quarantine_item = {
            "webhook_id": webhook.id,
            "job": job_json,
            "error": "Missing target URL",
            "quarantined_at": time.time(),
        }
        await _maybe_await(redis_conn.lpush(settings.quarantine_queue, json.dumps(quarantine_item)))
        if stream_msg_id:
            await acknowledge_and_delete(redis_conn, stream_name, consumer_group, stream_msg_id)
        return

    try:
        target_url = await asyncio.to_thread(
            validate_delivery_target,
            raw_target_url,
            environment=settings.environment,
            allowed_target_hosts=settings.allowed_target_hosts,
            max_url_length=settings.max_target_url_length,
        )
    except ValueError as e:
        logger.error(f"Target URL validation failed for webhook {webhook.id}: {e}. Moving to quarantine.")
        quarantine_item = {
            "webhook_id": webhook.id,
            "job": job_json,
            "error": f"Invalid target URL: {e}",
            "quarantined_at": time.time(),
        }
        await _maybe_await(redis_conn.lpush(settings.quarantine_queue, json.dumps(quarantine_item)))
        if stream_msg_id:
            await acknowledge_and_delete(redis_conn, stream_name, consumer_group, stream_msg_id)
        return

    # 3. Track attempt count
    attempt_key = f"attempt:{webhook.id}"
    attempt_val = await _maybe_await(redis_conn.get(attempt_key))
    attempt_number = int(attempt_val) if attempt_val else 0
    attempt_number += 1

    with logger.contextualize(event_id=webhook.id):
        logger.info(f"Processing webhook {webhook.id}, attempt #{attempt_number} to {target_url}")
        success = await deliver_webhook(webhook, target_url, client)

        # 4. Handle delivery result & acknowledge stream message AFTER persistence
        now = datetime.now(timezone.utc)
        if success:
            state = DeliveryState(
                event_id=webhook.id,
                status=DeliveryStatus.DELIVERED,
                attempt_count=attempt_number,
                last_attempt_time=now,
                final_delivery_time=now,
            )
            await save_delivery_state(redis_conn, state)
            await _maybe_await(redis_conn.delete(attempt_key))
            if stream_msg_id:
                await acknowledge_and_delete(redis_conn, stream_name, consumer_group, stream_msg_id)
            logger.success(f"Webhook {webhook.id} delivered successfully")
        else:
            if retry.should_move_to_dlq(attempt_number, settings.max_retry_attempts):
                logger.error(f"Webhook {webhook.id} exhausted all {settings.max_retry_attempts} retries. Moving to DLQ.")
                err_msg = f"Delivery failed after {attempt_number} attempts"
                state = DeliveryState(
                    event_id=webhook.id,
                    status=DeliveryStatus.DEAD,
                    attempt_count=attempt_number,
                    last_attempt_time=now,
                    last_error=err_msg,
                )
                await save_delivery_state(redis_conn, state)
                dlq_item = {
                    "webhook": webhook.model_dump(mode="json"),
                    "last_attempt": attempt_number,
                    "failed_at": time.time(),
                    "target_url": target_url,
                    "last_error": err_msg,
                }
                await _maybe_await(redis_conn.lpush(settings.dlq_key, json.dumps(dlq_item)))
                await _maybe_await(redis_conn.delete(attempt_key))
                if stream_msg_id:
                    await acknowledge_and_delete(redis_conn, stream_name, consumer_group, stream_msg_id)
            else:
                backoff_delay = retry.compute_backoff(attempt_number)
                err_msg = f"Delivery attempt {attempt_number} failed"
                next_retry_dt = now + timedelta(seconds=backoff_delay)
                state = DeliveryState(
                    event_id=webhook.id,
                    status=DeliveryStatus.RETRYING,
                    attempt_count=attempt_number,
                    last_attempt_time=now,
                    last_error=err_msg,
                    next_retry_time=next_retry_dt,
                )
                await save_delivery_state(redis_conn, state)
                await _maybe_await(redis_conn.set(attempt_key, str(attempt_number)))
                retry_time = time.time() + backoff_delay
                await _maybe_await(redis_conn.zadd(settings.delay_queue_key, {job_json: retry_time}))
                if stream_msg_id:
                    await acknowledge_and_delete(redis_conn, stream_name, consumer_group, stream_msg_id)
                logger.info(f"Webhook {webhook.id} scheduled for retry in {backoff_delay:.2f}s (at {retry_time})")

async def promote_delayed_jobs(
    redis_conn,
    delay_queue_key: str = settings.delay_queue_key,
    stream_name: str = settings.stream_name,
    limit: int = 100,
) -> int:
    """Atomically move due retry jobs from ZSET delay queue to Redis Stream."""
    now = time.time()
    try:
        res = await redis_conn.eval(PROMOTE_DELAYED_LUA, 2, delay_queue_key, stream_name, str(now), str(limit))
        return int(res) if res else 0
    except ResponseError as e:
        logger.error(f"Error promoting delayed jobs via Lua: {e}")
        return 0
    except Exception as e:
        logger.error(f"Unexpected error promoting delayed jobs: {e}")
        return 0

async def poll_delay_queue(
    redis_conn,
    delay_queue_key: str = settings.delay_queue_key,
    stream_name: str = settings.stream_name,
    poll_interval: float = 0.5,
):
    """Periodically promote due delayed jobs back to the main Redis stream."""
    while True:
        try:
            moved = await promote_delayed_jobs(redis_conn, delay_queue_key, stream_name)
            if moved > 0:
                logger.info(f"Promoted {moved} due retry webhook(s) to stream {stream_name}")
        except Exception as e:
            logger.error(f"Error polling delay queue: {e}")
        await asyncio.sleep(poll_interval)

async def reclaim_stale_pending_jobs(
    redis_conn,
    stream_name: str = settings.stream_name,
    consumer_group: str = settings.consumer_group,
    worker_id: str = "worker-reclaim",
    min_idle_time_ms: int = settings.stream_claim_min_idle_ms,
    count: int = 10,
) -> List[Tuple[str, dict]]:
    """Reclaim stale unacknowledged pending stream entries from crashed workers."""
    claimed_messages = []
    try:
        res = await redis_conn.xautoclaim(
            stream_name,
            consumer_group,
            worker_id,
            min_idle_time=min_idle_time_ms,
            start_id="0-0",
            count=count,
        )
        if res and len(res) >= 2:
            for item in res[1]:
                if isinstance(item, (tuple, list)) and len(item) == 2:
                    msg_id, fields = item
                    claimed_messages.append((msg_id, fields))
    except Exception as e:
        logger.error(f"Error reclaiming stale pending jobs: {e}")
    return claimed_messages

class DeliveryWorker:
    """Delivery Worker managing stream consumption, concurrency, and graceful shutdown."""

    def __init__(
        self,
        worker_id: Optional[str] = None,
        concurrency: int = 50,
        stream_name: str = settings.stream_name,
        consumer_group: str = settings.consumer_group,
        min_idle_time_ms: int = settings.stream_claim_min_idle_ms,
        block_ms: int = 1000,
    ):
        self.worker_id = worker_id or generate_worker_id()
        self.semaphore = asyncio.Semaphore(concurrency)
        self.stream_name = stream_name
        self.consumer_group = consumer_group
        self.min_idle_time_ms = min_idle_time_ms
        self.block_ms = block_ms
        self.in_flight_tasks: Set[asyncio.Task] = set()
        self.shutdown_event = asyncio.Event()

    def signal_shutdown(self):
        """Trigger graceful shutdown signal."""
        logger.info(f"Worker {self.worker_id} received shutdown signal.")
        self.shutdown_event.set()

    async def _run_job_task(
        self,
        redis_conn,
        msg_id: str,
        job_json: str,
        client: httpx.AsyncClient,
    ):
        """Execute process_job within semaphore bounds and track active task."""
        task = asyncio.current_task()
        if task:
            self.in_flight_tasks.add(task)
        try:
            await process_job(
                redis_conn=redis_conn,
                job_json=job_json,
                client=client,
                stream_msg_id=msg_id,
                stream_name=self.stream_name,
                consumer_group=self.consumer_group,
            )
        except Exception as e:
            logger.error(f"Error processing stream job {msg_id}: {e}")
        finally:
            self.semaphore.release()
            if task:
                self.in_flight_tasks.discard(task)

    async def run(self, redis_conn=None):
        """Run worker main loop: consume stream, reclaim crashed jobs, and handle shutdown."""
        if redis_conn is None:
            redis_conn = redis_client.get_async_redis()

        await ensure_consumer_group(redis_conn, self.stream_name, self.consumer_group)
        logger.info(f"Worker {self.worker_id} started, listening on stream {self.stream_name}...")

        # Start background delay queue poller and worker heartbeat
        poller_task = asyncio.create_task(
            poll_delay_queue(redis_conn, settings.delay_queue_key, self.stream_name)
        )
        hb_task = asyncio.create_task(
            heartbeat_loop(redis_conn, self.worker_id, self.shutdown_event)
        )

        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            while not self.shutdown_event.is_set():
                try:
                    # 1. Reclaim stale pending jobs from crashed workers
                    reclaimed = await reclaim_stale_pending_jobs(
                        redis_conn,
                        stream_name=self.stream_name,
                        consumer_group=self.consumer_group,
                        worker_id=self.worker_id,
                        min_idle_time_ms=self.min_idle_time_ms,
                    )
                    for msg_id, fields in reclaimed:
                        job_json = extract_job_json(fields)
                        if job_json:
                            await self.semaphore.acquire()
                            asyncio.create_task(
                                self._run_job_task(redis_conn, msg_id, job_json, client)
                            )

                    # 2. Read new stream entries for consumer group
                    response = await redis_conn.xreadgroup(
                        self.consumer_group,
                        self.worker_id,
                        {self.stream_name: ">"},
                        count=10,
                        block=self.block_ms,
                    )
                    if response:
                        for stream, messages in response:
                            for msg_id, fields in messages:
                                job_json = extract_job_json(fields)
                                if job_json:
                                    await self.semaphore.acquire()
                                    asyncio.create_task(
                                        self._run_job_task(redis_conn, msg_id, job_json, client)
                                    )
                                else:
                                    # Malformed stream entry fields
                                    logger.error(f"Malformed stream entry {msg_id}: missing job json")
                                    await _maybe_await(
                                        redis_conn.lpush(
                                            settings.quarantine_queue,
                                            json.dumps({"msg_id": msg_id, "fields": fields, "error": "Missing payload"}),
                                        )
                                    )
                                    await acknowledge_and_delete(
                                        redis_conn,
                                        self.stream_name,
                                        self.consumer_group,
                                        msg_id,
                                    )
                    else:
                        await asyncio.sleep(0.01)

                except Exception as e:
                    if not self.shutdown_event.is_set():
                        logger.error(f"Worker main loop error: {e}")
                        await asyncio.sleep(0.01)

            # Graceful shutdown: wait for in-flight tasks to complete before closing HTTP client
            poller_task.cancel()
            hb_task.cancel()
            try:
                await poller_task
            except asyncio.CancelledError:
                pass
            try:
                await hb_task
            except asyncio.CancelledError:
                pass

            if self.in_flight_tasks:
                logger.info(f"Worker {self.worker_id} waiting for {len(self.in_flight_tasks)} in-flight tasks...")
                await asyncio.gather(*list(self.in_flight_tasks), return_exceptions=True)
            logger.info(f"Worker {self.worker_id} shutdown complete.")

async def run_process_job(
    redis_conn,
    job_json: str,
    semaphore: asyncio.Semaphore,
    client: httpx.AsyncClient,
    stream_msg_id: Optional[str] = None,
):
    """Backward-compatible helper wrapper for process_job."""
    try:
        await process_job(redis_conn, job_json, client, stream_msg_id=stream_msg_id)
    except Exception as e:
        logger.error(f"Error processing job: {e}")
    finally:
        semaphore.release()

async def main():
    redis_conn = redis_client.get_async_redis()
    worker = DeliveryWorker(concurrency=settings.worker_concurrency)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.signal_shutdown)
        except NotImplementedError:
            pass

    await worker.run(redis_conn)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Worker stopped by user")
