import sys
import hmac
import hashlib
from fastapi.testclient import TestClient
from loguru import logger
from app.main import app

# Ensure logger outputs to stderr so we see it
logger.remove()
logger.add(sys.stderr, level="DEBUG")

client = TestClient(app)

secret = "your_secret_here"
payload = b'{"id": "123", "event_type": "test", "payload": {}}'

# Calculate signature
signature = hmac.new(secret.encode(), msg=payload, digestmod=hashlib.sha256).hexdigest()
signature_header = f"sha256={signature}"

print("--- Sending Valid Request ---")
response = client.post(
    "/webhooks",
    content=payload,
    headers={"x-signature": signature_header}
)
print("Response:", response.status_code, response.json())

print("\n--- Sending Invalid Signature Request ---")
response_invalid = client.post(
    "/webhooks",
    content=payload,
    headers={"x-signature": "sha256=invalid"}
)
print("Response:", response_invalid.status_code, response_invalid.json())
