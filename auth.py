"""Local account validation and password hashing, using only the standard library."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets


PASSWORD_ROUNDS = 600_000
SESSION_SECONDS = 30 * 24 * 60 * 60
SESSION_COOKIE = "clipshelf_session"
SESSION_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)
# Missing accounts still perform the same password derivation as existing ones.
DUMMY_PASSWORD_HASH = f"pbkdf2_sha256${PASSWORD_ROUNDS}${'00' * 16}${'00' * 32}"


def valid_text(value: object, maximum: int) -> bool:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def normalize_email(value: object) -> str:
    if not valid_text(value, 254):
        raise ValueError("Enter a valid email address of at most 254 characters.")
    email = value.strip().casefold()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("Enter a valid email address.")
    return email


def validate_password(value: object) -> str:
    if not valid_text(value, 1024) or len(value) < 6:
        raise ValueError("Password must contain 6 to 1,024 characters.")
    return value


def validate_signup(payload: object) -> tuple[str, str, str]:
    if not isinstance(payload, dict):
        raise ValueError("Account details must be a JSON object.")
    name = payload.get("name")
    if not valid_text(name, 80) or not name.strip():
        raise ValueError("Name is required and must be at most 80 characters.")
    return name.strip(), normalize_email(payload.get("email")), validate_password(payload.get("password"))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ROUNDS)
    return f"pbkdf2_sha256${PASSWORD_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt, expected = stored.split("$")
        iterations = int(rounds)
        if algorithm != "pbkdf2_sha256" or not 100_000 <= iterations <= 2_000_000:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
        return hmac.compare_digest(digest, bytes.fromhex(expected))
    except (ValueError, UnicodeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()
