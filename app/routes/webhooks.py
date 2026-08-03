"""Webhook ingestion route — validates, deduplicates, and enqueues incoming events."""

from fastapi import APIRouter, Request, HTTPException, status, Response
from loguru import logger
from app.models import IncomingWebhook
from app.config import settings
from app.core import security, idempotency

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
    except ValueError:
        logger.warning("Invalid webhook payload")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook payload",
        )
    except Exception:
        logger.warning("Invalid JSON received in webhook payload")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON")

    accepted = await idempotency.deduplicate_and_enqueue(
        event_id=webhook_data.id,
        payload_json=webhook_data.model_dump_json(),
        stream_name=settings.stream_name,
        ttl_seconds=settings.idempotency_ttl_seconds,
    )

    if not accepted:
        logger.info(f"Duplicate webhook ignored: {webhook_data.id}")
        response.status_code = status.HTTP_200_OK
        return {"status": "duplicate ignored"}

    logger.info(f"Enqueued webhook {webhook_data.id} to stream {settings.stream_name}")
    return {"status": "accepted"}
