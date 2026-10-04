"""Шифрование значений меток и их слепой отпечаток.

Шифруем в приложении (AES-256-GCM), а не в Postgres через pgcrypto: там
ключ уходит в текст SQL-запроса и может осесть в журнале запросов базы и
в `pg_stat_statements`. Здесь ключ живёт только в памяти контейнера.

Из одного секрета `PII_ENCRYPTION_KEY` выводятся два ключа (HKDF): один
шифрует, другой считает отпечаток значения. Отпечаток — HMAC, а не голый
хеш: телефонов всего 10¹⁰, и хеш без секрета перебирается за минуты.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import settings

logger = logging.getLogger(__name__)

_keys: tuple[str, bytes, bytes] | None = None


class KeyMissing(RuntimeError):
    pass


def _derive(secret: bytes, purpose: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"teashop-pii:" + purpose).derive(secret)


def _load() -> tuple[bytes, bytes] | None:
    global _keys
    raw = settings.pii_encryption_key or ""
    if _keys is not None and _keys[0] == raw:
        return _keys[1], _keys[2]
    if not raw:
        return None
    try:
        secret = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError):
        logger.error("PII_ENCRYPTION_KEY не base64 — метки выключены")
        return None
    if len(secret) < 32:
        logger.error("PII_ENCRYPTION_KEY короче 32 байт — метки выключены")
        return None
    _keys = (raw, _derive(secret, b"encrypt"), _derive(secret, b"blind-index"))
    return _keys[1], _keys[2]


def available() -> bool:
    return _load() is not None


def _require() -> tuple[bytes, bytes]:
    keys = _load()
    if keys is None:
        raise KeyMissing("PII_ENCRYPTION_KEY не задан")
    return keys


def _aad(client_key: str, label: str) -> bytes:
    return f"{client_key}|{label}".encode()


def encrypt(value: str, *, client_key: str, label: str) -> str:
    key, _ = _require()
    nonce = os.urandom(12)
    sealed = AESGCM(key).encrypt(nonce, value.encode(), _aad(client_key, label))
    return base64.b64encode(nonce + sealed).decode()


def decrypt(blob: str, *, client_key: str, label: str) -> str:
    key, _ = _require()
    raw = base64.b64decode(blob)
    return AESGCM(key).decrypt(raw[:12], raw[12:], _aad(client_key, label)).decode()


def fingerprint(kind: str, value: str, *, client_key: str) -> str:
    """Отпечаток значения в пределах клиента: одинаковый у одинаковых значений."""
    _, key = _require()
    message = f"{client_key}|{kind}|{value.casefold().replace('ё', 'е')}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()
