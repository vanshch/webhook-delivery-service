"""Webhook ingestion route — validates, deduplicates, and enqueues incoming events."""

from fastapi import APIRouter, Request, HTTPException, status, Response
from loguru import logger
from app.models import IncomingWebhook
from app.config import settings
from app.core import security, idempotency
from app.storage import redis_client

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(request: Request, response: Response):
    """Receive an incoming webhook, verify its HMAC signature, deduplicate, and enqueue for delivery."""
    raw_body = await request.body()
    signature_header = request.headers.get("x-signature", "")
    
    if not security.verify_signature(raw_body, signature_header, settings.webhook_secret):
        logger.warning("Webhook verification failed: Invalid signature")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    try:
        body_json = await request.json()
        webhook_data = IncomingWebhook(**body_json)
        logger.debug(f"Successfully parsed webhook payload: {webhook_data.id}")
    except Exception:
        logger.warning("Invalid JSON received in webhook payload")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON")

    if await idempotency.is_duplicate(webhook_data.id):
        logger.info(f"Duplicate webhook ignored: {webhook_data.id}")
        response.status_code = status.HTTP_200_OK
        return {"status": "duplicate ignored"}

    redis = redis_client.get_async_redis()
    await redis.lpush("webhook_queue", webhook_data.model_dump_json())
    logger.info(f"Enqueued webhook {webhook_data.id} for processing")

    await idempotency.mark_processed(webhook_data.id, settings.idempotency_ttl_seconds)

    return {"status": "accepted"}
