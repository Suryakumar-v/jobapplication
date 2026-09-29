from __future__ import annotations

from app.utils.hashing import sha256_text, stable_fingerprint
from app.utils.redaction import REDACTED, redact_text, redact_url, redact_value
from app.utils.text_utils import normalize_text, slugify


def test_redacts_key_value_secrets() -> None:
    out = redact_text('password=hunter2 and "api_key": "abcd1234efgh" token: xyz')
    assert "hunter2" not in out
    assert "abcd1234efgh" not in out
    assert "xyz" not in out
    assert REDACTED in out


def test_redacts_bearer_email_phone() -> None:
    out = redact_text(
        "Authorization Bearer abcdefghijklmnop1234 mail a.b@corp.com call +1 555 010 0199"
    )
    assert "abcdefghijklmnop1234" not in out
    assert "a.b@corp.com" not in out
    assert "555 010 0199" not in out


def test_redacts_long_opaque_strings() -> None:
    secret = "A" * 40
    assert secret not in redact_text(f"value {secret}")


def test_plain_text_preserved() -> None:
    assert redact_text("Element not found: submit button") == "Element not found: submit button"


def test_redact_url_removes_credentials_query_fragment() -> None:
    url = "https://user:pw@jobs.example.com:8443/a/b?token=1&x=2#frag"
    assert redact_url(url) == "https://jobs.example.com:8443/a/b"


def test_redact_value_masks_sensitive_keys_recursively() -> None:
    data = {"name": "ok", "cookies": [1, 2], "nested": {"gender_answer": "x", "note": "hi"}}
    result = redact_value(data)
    assert result["cookies"] == REDACTED
    assert result["nested"]["gender_answer"] == REDACTED
    assert result["nested"]["note"] == "hi"
    assert result["name"] == "ok"


def test_hashing_is_deterministic_and_boundary_safe() -> None:
    assert sha256_text("a") == sha256_text("a")
    assert stable_fingerprint("ab", "c") != stable_fingerprint("a", "bc")
    assert len(stable_fingerprint("x")) == 32


def test_text_normalization() -> None:
    assert normalize_text("  Sr. Network-Security   Engineer! ") == "sr network security engineer"
    assert slugify("Acme, Inc. / Sr. Engineer") == "acme-inc-sr-engineer"
    assert slugify("!!!") == "untitled"
