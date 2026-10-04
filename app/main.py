"""FastAPI application entry point with request logging middleware."""

import time
import uuid
import re
from fastapi import FastAPI, Request
from loguru import logger
from app.routes import webhooks, health, deliveries, website

app = FastAPI(title="Webhook Delivery Service")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def get_request_id(request: Request) -> str:
    """Accept only log-safe request IDs, otherwise generate one."""
    supplied = request.headers.get("x-request-id", "")
    if REQUEST_ID_PATTERN.fullmatch(supplied):
        return supplied
    return f"req_{uuid.uuid4().hex[:12]}"

@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log all incoming requests with structured request IDs and timing information."""
    start_time = time.time()
    request_id = get_request_id(request)
    client_ip = request.client.host if request.client else "unknown"

    with logger.contextualize(request_id=request_id):
        logger.info(
            f"[{request_id}] Incoming request: {request.method} {request.url.path} from {client_ip}"
        )

        response = await call_next(request)

        process_time = (time.time() - start_time) * 1000
        formatted_process_time = f"{process_time:.2f}ms"

        if response.status_code >= 400:
            logger.error(
                f"[{request_id}] Failed request: {request.method} {request.url.path} - Status: {response.status_code} in {formatted_process_time}"
            )
        else:
            logger.success(
                f"[{request_id}] Completed request: {request.method} {request.url.path} - Status: {response.status_code} in {formatted_process_time}"
            )

        response.headers["X-Request-ID"] = request_id
        return response

app.include_router(health.router)
app.include_router(webhooks.router)
app.include_router(deliveries.router)
app.include_router(website.router)
