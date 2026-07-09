import hmac
import hashlib
from loguru import logger

def verify_signature(payload_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify the HMAC signature of an incoming webhook.

    - Compute HMAC over the raw request body using `secret`.
    - Compare against `signature_header` using a constant-time check.
    - Return True iff valid; never short-circuit on length.
    """
    if not signature_header or not secret:
        logger.warning("Signature verification failed: Missing signature header or secret")
        return False
        
    secret_bytes = secret.encode('utf-8') if isinstance(secret, str) else secret
    
    expected_mac = hmac.new(
        key=secret_bytes,
        msg=payload_body,
        digestmod=hashlib.sha256
    )
    expected_hex = expected_mac.hexdigest()
    
    if signature_header.startswith("sha256="):
        signature_header = signature_header[len("sha256="):]
        
    is_valid = hmac.compare_digest(expected_hex, signature_header)
    
    if is_valid:
        logger.debug("Signature verification succeeded")
    else:
        logger.warning("Signature verification failed: Hash mismatch")
        
    return is_valid
