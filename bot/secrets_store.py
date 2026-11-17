"""Secret storage: dashboard password hash, session key, and exchange API keys.

Stored in data/secrets.json with permissions 600 (only your user can read it).
This file is git-ignored and must never be committed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading

from . import config
from .config import atomic_write

_lock = threading.Lock()

# scrypt parameters (memory-hard password hashing from the standard library)
_N, _R, _P = 2**14, 8, 1


def _path():
    return config.DATA_DIR / "secrets.json"


def _load() -> dict:
    path = _path()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _save(data: dict) -> None:
    atomic_write(_path(), json.dumps(data, indent=2), mode=0o600)


def get_session_secret() -> str:
    with _lock:
        data = _load()
        if not data.get("session_secret"):
            data["session_secret"] = secrets.token_hex(32)
            _save(data)
        return data["session_secret"]


# ---------------- Dashboard password ----------------

def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)


def password_is_set() -> bool:
    with _lock:
        return bool(_load().get("password_hash"))


def set_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    with _lock:
        data = _load()
        data["password_salt"] = salt.hex()
        data["password_hash"] = _hash_password(password, salt).hex()
        # Bumping the version logs out every existing session.
        data["password_version"] = int(data.get("password_version", 0)) + 1
        _save(data)


def verify_password(password: str) -> bool:
    with _lock:
        data = _load()
    if not data.get("password_hash"):
        return False
    expected = bytes.fromhex(data["password_hash"])
    actual = _hash_password(password, bytes.fromhex(data["password_salt"]))
    return hmac.compare_digest(expected, actual)


def password_version() -> int:
    with _lock:
        return int(_load().get("password_version", 0))


# ---------------- Exchange API keys ----------------

def get_api_keys() -> dict:
    """Returns {'api_key', 'api_secret', 'api_password'} (empty strings if unset)."""
    with _lock:
        keys = _load().get("api_keys", {})
    return {
        "api_key": keys.get("api_key", ""),
        "api_secret": keys.get("api_secret", ""),
        "api_password": keys.get("api_password", ""),
    }


def set_api_keys(api_key: str, api_secret: str, api_password: str = "") -> None:
    with _lock:
        data = _load()
        data["api_keys"] = {
            "api_key": api_key.strip(),
            # Coinbase secrets are multi-line PEM keys: keep inner newlines.
            "api_secret": api_secret.strip(),
            "api_password": api_password.strip(),
        }
        _save(data)


def clear_api_keys() -> None:
    with _lock:
        data = _load()
        data.pop("api_keys", None)
        _save(data)


def api_keys_set() -> bool:
    keys = get_api_keys()
    return bool(keys["api_key"] and keys["api_secret"])


def mask(value: str) -> str:
    if not value:
        return ""
    tail = value.strip()[-4:]
    return "••••" + tail if len(value) > 8 else "••••"
