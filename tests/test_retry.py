from app.core.retry import compute_backoff, should_move_to_dlq

def test_compute_backoff_grows():
    b1 = compute_backoff(1)
    b2 = compute_backoff(2)
    b3 = compute_backoff(3)
    
    assert 2.0 <= b1 <= 3.0
    assert 4.0 <= b2 <= 5.0
    assert 8.0 <= b3 <= 9.0
    assert b1 < b2 < b3

def test_compute_backoff_capped():
    b10 = compute_backoff(10)
    assert b10 <= 60.0

def test_should_move_to_dlq():
    max_attempts = 5
    assert should_move_to_dlq(1, max_attempts) is False
    assert should_move_to_dlq(4, max_attempts) is False
    assert should_move_to_dlq(5, max_attempts) is True
    assert should_move_to_dlq(6, max_attempts) is True
