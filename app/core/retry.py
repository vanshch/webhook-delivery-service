import random

def compute_backoff(attempt: int) -> float:
    """Return delay in seconds before the next retry for a given attempt number.

    Exponential backoff with random jitter.
    """
    backoff = 2 ** attempt
    jitter = random.uniform(0, 1)
    return min(backoff + jitter, 60.0)

def should_move_to_dlq(attempt: int, max_attempts: int) -> bool:
    """Return True when a delivery has exhausted its retries and must go to the DLQ.

    Moves to DLQ when attempt count is greater than or equal to max_attempts.
    """
    return attempt >= max_attempts
