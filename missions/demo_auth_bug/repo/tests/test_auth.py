from app.auth import is_token_valid

def test_unexpired_token_is_valid():
    assert is_token_valid(20, 10)

def test_expired_token_is_invalid():
    assert not is_token_valid(10, 20)
