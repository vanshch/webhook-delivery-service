"""Webhook ingestion route — validates, deduplicates, and enqueues incoming events."""

import json

from fastapi import APIRouter, Request, HTTPException, status, Response
from loguru import logger
from app.models import IncomingWebhook
from app.config import settings
from app.core import security, idempotency

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def read_limited_body(request: Request, limit: int) -> bytes:
    """Read a request body without buffering more than the configured limit."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > limit:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="Request body exceeds maximum allowed size",
                )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Content-Length header",
            ) from exc

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Request body exceeds maximum allowed size",
            )
        body.extend(chunk)
    return bytes(body)


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(request: Request, response: Response):
    """Receive an incoming webhook, verify its HMAC signature, deduplicate, and enqueue for delivery."""
    raw_body = await read_limited_body(request, settings.max_request_body_bytes)

    signature_header = request.headers.get("x-signature", "")
    
    if not security.verify_signature(raw_body, signature_header, settings.webhook_secret):
        logger.warning("Webhook verification failed: Invalid signature")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    try:
        body_json = json.loads(raw_body)
        webhook_data = IncomingWebhook(**body_json)
        logger.debug(f"Successfully parsed webhook payload: {webhook_data.id}")
    except (ValueError, TypeError):
        logger.warning("Invalid webhook payload")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook payload",
        )

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
