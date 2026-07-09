from fastapi import FastAPI, Response
import asyncio

app = FastAPI()

@app.post("/success-endpoint")
async def success_endpoint():
    await asyncio.sleep(0.05)  # simulate 50ms latency
    return {"status": "delivered"}

@app.post("/fail-endpoint")
async def fail_endpoint():
    await asyncio.sleep(0.05)  # simulate 50ms latency
    return Response(
        content='{"error": "Internal Server Error"}',
        media_type="application/json",
        status_code=500
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8081)
