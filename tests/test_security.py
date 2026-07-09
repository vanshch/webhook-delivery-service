import pytest
import hmac
import hashlib
from app.core.security import verify_signature

def test_verify_signature_valid():
    secret = "my_super_secret"
    payload = b'{"event": "test"}'
    
    # Generate valid signature
    mac = hmac.new(secret.encode('utf-8'), msg=payload, digestmod=hashlib.sha256)
    signature = f"sha256={mac.hexdigest()}"
    
    assert verify_signature(payload, signature, secret) is True

def test_verify_signature_invalid():
    secret = "my_super_secret"
    payload = b'{"event": "test"}'
    
    assert verify_signature(payload, "sha256=invalidhash", secret) is False
    assert verify_signature(payload, "invalidhash", secret) is False
    
def test_verify_signature_tampered_payload():
    secret = "my_super_secret"
    original_payload = b'{"event": "test"}'
    tampered_payload = b'{"event": "hacked"}'
    
    mac = hmac.new(secret.encode('utf-8'), msg=original_payload, digestmod=hashlib.sha256)
    signature = f"sha256={mac.hexdigest()}"
    
    assert verify_signature(tampered_payload, signature, secret) is False

def test_verify_signature_empty_secret():
    payload = b'{"event": "test"}'
    assert verify_signature(payload, "sha256=somehash", "") is False
    assert verify_signature(payload, "sha256=somehash", None) is False

def test_verify_signature_no_prefix():
    secret = "my_super_secret"
    payload = b'{"event": "test"}'
    mac = hmac.new(secret.encode('utf-8'), msg=payload, digestmod=hashlib.sha256)
    raw_hex = mac.hexdigest()
    assert verify_signature(payload, raw_hex, secret) is True

def test_api_invalid_signature_returns_401(client):
    payload = b'{"id": "123", "event_type": "test", "payload": {}}'
    response = client.post(
        "/webhooks",
        content=payload,
        headers={"x-signature": "sha256=wrong_sig"}
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid signature"}

def test_api_missing_signature_returns_401(client):
    payload = b'{"id": "123", "event_type": "test", "payload": {}}'
    response = client.post(
        "/webhooks",
        content=payload
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid signature"}
