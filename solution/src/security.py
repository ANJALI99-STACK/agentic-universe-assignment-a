"""PII protection: authenticated encryption (Fernet) + keyed HMAC lookup hash.

* ``encrypt_json`` / ``decrypt_json`` protect the bundle of ``pii: true`` fields.
  Fernet = AES-128-CBC + HMAC-SHA256 (authenticated); MultiFernet allows key rotation.
* ``phone_hash`` is a deterministic HMAC-SHA256 over ``workflow_id`` + canonical phone, so
  the same number in two workflows yields different hashes and the DB never sees the phone.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from src.config import ConfigError, Settings


class PiiDecryptionError(RuntimeError):
    """Stored PII could not be decrypted (wrong key or corrupted data). Contains no PII."""


class PiiProtector:
    def __init__(self, encryption_keys: tuple[str, ...], hmac_key: str) -> None:
        try:
            self._fernet = MultiFernet([Fernet(k.encode()) for k in encryption_keys])
        except (ValueError, TypeError) as exc:
            raise ConfigError("PII_ENCRYPTION_KEY is not a valid Fernet key") from exc
        self._hmac_key = hmac_key.encode("utf-8")

    @classmethod
    def from_settings(cls, settings: Settings) -> "PiiProtector":
        return cls(settings.encryption_keys, settings.hmac_key)

    def encrypt_json(self, data: dict[str, Any]) -> str:
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return self._fernet.encrypt(payload).decode("ascii")

    def decrypt_json(self, token: str) -> dict[str, Any]:
        try:
            return json.loads(self._fernet.decrypt(token.encode("ascii")))
        except InvalidToken as exc:
            raise PiiDecryptionError("could not decrypt stored PII (wrong key or corrupted token)") from exc

    def phone_hash(self, workflow_id: str, canonical_phone: str) -> str:
        message = f"{workflow_id}\x00{canonical_phone}".encode("utf-8")
        return hmac.new(self._hmac_key, message, hashlib.sha256).hexdigest()
