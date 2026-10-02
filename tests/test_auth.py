from auth import hash_password, verify_password

def test_password_hashing():
    pwd = "MySecurePass123"
    hashed = hash_password(pwd)
    assert hashed != pwd
    assert verify_password(pwd, hashed) is True
    assert verify_password("WrongPass", hashed) is False