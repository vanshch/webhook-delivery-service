"""Async delivery worker that processes webhooks from the Redis queue.

Dequeues jobs, attempts HTTP delivery with exponential backoff retries,
and moves exhausted deliveries to the dead-letter queue.
"""

import hmac
import hashlib
import time
import json
import httpx
import asyncio
from loguru import logger
from app.storage import redis_client
from app.core import retry
from app.config import settings
from app.models import IncomingWebhook

async def deliver_webhook(
    webhook: IncomingWebhook,
    target_url: str,
    client: httpx.AsyncClient,
) -> bool:
    """Attempt HTTP POST delivery of the webhook to target_url.
    Returns True if successful, False otherwise.
    """
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

        response = await client.post(target_url, content=body, headers=headers)
        if 200 <= response.status_code < 300:
            logger.info(f"Webhook {webhook.id} successfully delivered to {target_url}")
            return True
        else:
            logger.warning(f"Webhook {webhook.id} delivery to {target_url} returned status {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"Webhook {webhook.id} delivery failed: {e}")
        return False

async def process_job(
    redis_conn,
    job_json: str,
    client: httpx.AsyncClient,
):
    """Parse job JSON, increment attempt count, deliver, and handle success/retry/DLQ."""
    try:
        data = json.loads(job_json)
        webhook = IncomingWebhook(**data)
    except Exception as e:
        logger.error(f"Failed to parse job JSON: {e}")
        return

    target_url = webhook.target_url or settings.default_target_url
    if not target_url:
        logger.error(f"No target URL specified for webhook {webhook.id}")
        return

    attempt_key = f"attempt:{webhook.id}"
    attempt_val = await redis_conn.get(attempt_key)
    attempt_number = int(attempt_val) if attempt_val else 0
    attempt_number += 1

    logger.info(f"Processing webhook {webhook.id}, attempt #{attempt_number} to {target_url}")
    success = await deliver_webhook(webhook, target_url, client)

    if success:
        await redis_conn.delete(attempt_key)
        logger.success(f"Webhook {webhook.id} delivered successfully")
    else:
        if retry.should_move_to_dlq(attempt_number, settings.max_retry_attempts):
            logger.error(f"Webhook {webhook.id} exhausted all {settings.max_retry_attempts} retries. Moving to DLQ.")
            dlq_item = {
                "webhook": webhook.model_dump(mode="json"),
                "last_attempt": attempt_number,
                "failed_at": time.time(),
                "target_url": target_url
            }
            await redis_conn.lpush("webhook_dlq", json.dumps(dlq_item))
            await redis_conn.delete(attempt_key)
        else:
            backoff_delay = retry.compute_backoff(attempt_number)
            await redis_conn.set(attempt_key, str(attempt_number))
            retry_time = time.time() + backoff_delay
            await redis_conn.zadd("webhook_delay_queue", {job_json: retry_time})
            logger.info(f"Webhook {webhook.id} scheduled for retry in {backoff_delay:.2f}s (at {retry_time})")

async def run_process_job(
    redis_conn,
    job_json: str,
    semaphore: asyncio.Semaphore,
    client: httpx.AsyncClient,
):
    """Helper wrapper to ensure the semaphore is released when job processing finishes."""
    try:
        await process_job(redis_conn, job_json, client)
    except Exception as e:
        logger.error(f"Error processing job: {e}")
    finally:
        semaphore.release()

async def poll_delay_queue(redis):
    """Periodically check the delay queue and move due jobs back to the main queue."""
    while True:
        try:
            now = time.time()
            due_jobs = await redis.zrangebyscore("webhook_delay_queue", 0, now)
            for job_json in due_jobs:
                if await redis.zrem("webhook_delay_queue", job_json) > 0:
                    await redis.lpush("webhook_queue", job_json)
                    logger.info("Moved due retry webhook back to main queue")
        except Exception as e:
            logger.error(f"Error polling delay queue: {e}")
        await asyncio.sleep(0.5)

async def main():
    redis = redis_client.get_async_redis()
    logger.info("Worker started, listening to queue...")
    
    # Start the background task to poll the delayed retry queue
    asyncio.create_task(poll_delay_queue(redis))
    
    semaphore = asyncio.Semaphore(50)
    
    async with httpx.AsyncClient(timeout=5.0) as client:
        while True:
            try:
                # Block and pop new jobs from the queue
                job = await redis.blpop("webhook_queue", timeout=1)
                if job:
                    _, job_json = job
                    await semaphore.acquire()
                    asyncio.create_task(
                        run_process_job(redis, job_json, semaphore, client)
                    )
            except Exception as e:
                logger.error(f"Worker main loop error: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Worker stopped by user")
