import pytest
from cryptography.fernet import Fernet

from src.config import ConfigError
from src.security import PiiDecryptionError, PiiProtector


def make(key=None, hmac_key="k" * 40):
    return PiiProtector((key or Fernet.generate_key().decode(),), hmac_key)


def test_encrypt_decrypt_roundtrip_and_no_plaintext_in_token():
    p = make()
    token = p.encrypt_json({"customer_name": "Asha Rao", "phone": "+919876543210"})
    assert "Asha" not in token and "9876543210" not in token
    assert p.decrypt_json(token) == {"customer_name": "Asha Rao", "phone": "+919876543210"}


def test_encryption_is_randomised_but_hash_is_deterministic():
    p = make()
    assert p.encrypt_json({"a": "1"}) != p.encrypt_json({"a": "1"})
    assert p.phone_hash("wf", "+919876543210") == p.phone_hash("wf", "+919876543210")


def test_hash_is_scoped_per_workflow_and_per_key():
    p = make()
    assert p.phone_hash("wf_a", "+919876543210") != p.phone_hash("wf_b", "+919876543210")
    assert make(hmac_key="z" * 40).phone_hash("wf_a", "+919876543210") != p.phone_hash("wf_a", "+919876543210")
    assert len(p.phone_hash("wf", "+919876543210")) == 64


def test_tampered_or_wrong_key_fails_authentication():
    p = make()
    token = p.encrypt_json({"a": "1"})
    with pytest.raises(PiiDecryptionError):
        make().decrypt_json(token)
    with pytest.raises(PiiDecryptionError):
        p.decrypt_json(token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB"))


def test_key_rotation_old_tokens_still_decrypt():
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    token = PiiProtector((old,), "k" * 40).encrypt_json({"a": "1"})
    rotated = PiiProtector((new, old), "k" * 40)
    assert rotated.decrypt_json(token) == {"a": "1"}


def test_invalid_fernet_key_is_a_config_error():
    with pytest.raises(ConfigError):
        PiiProtector(("not-a-key",), "k" * 40)
