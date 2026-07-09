import time
from fastapi import FastAPI, Request
from loguru import logger
from app.routes import webhooks, health

app = FastAPI(title="Webhook Delivery Service")

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    client_ip = request.client.host if request.client else "unknown"
    logger.info(f"Incoming request: {request.method} {request.url.path} from {client_ip}")
    
    response = await call_next(request)
    
    process_time = (time.time() - start_time) * 1000
    formatted_process_time = f"{process_time:.2f}ms"
    
    if response.status_code >= 400:
        logger.error(f"Failed request: {request.method} {request.url.path} - Status: {response.status_code} in {formatted_process_time}")
    else:
        logger.success(f"Completed request: {request.method} {request.url.path} - Status: {response.status_code} in {formatted_process_time}")
        
    return response

app.include_router(health.router)
app.include_router(webhooks.router)
