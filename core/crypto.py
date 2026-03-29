"""
Key encryption utilities using PBKDF2 + Fernet.
Adapted from discountry/polymarket-trading-bot/src/crypto.py
"""

import base64
import os

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


def derive_key(password: str, salt: bytes) -> bytes:
    """Derive a Fernet-compatible key from a password and salt."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def encrypt_key(private_key: str, password: str) -> tuple[bytes, bytes]:
    """Encrypt a private key with a password. Returns (encrypted_data, salt)."""
    salt = os.urandom(16)
    key = derive_key(password, salt)
    f = Fernet(key)
    encrypted = f.encrypt(private_key.encode())
    return encrypted, salt


def decrypt_key(encrypted_data: bytes, password: str, salt: bytes) -> str:
    """Decrypt a private key with a password and salt."""
    key = derive_key(password, salt)
    f = Fernet(key)
    return f.decrypt(encrypted_data).decode()
