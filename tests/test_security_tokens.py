"""Token generation, hashing, verification, and the rules around them."""

from __future__ import annotations

import time

import pytest

from test_hub.api.security import (
    TokenVerifier,
    generate_token,
    hash_token,
    parse_authorization,
    verify_token_hash,
)
from test_hub.config import Settings
from test_hub.errors import AuthenticationError, ConfigurationError


def test_generated_tokens_are_unique_prefixed_and_long() -> None:
    tokens = {generate_token() for _ in range(200)}
    assert len(tokens) == 200
    for token in tokens:
        assert token.startswith("ths_")
        assert len(token) > 40


def test_a_hash_never_contains_the_token() -> None:
    """The whole point: a stolen database must not yield working credentials."""
    token = generate_token()
    digest = hash_token(token)
    assert token not in digest
    assert token.removeprefix("ths_") not in digest


def test_hashing_is_salted_so_two_hashes_of_one_token_differ() -> None:
    token = generate_token()
    assert hash_token(token) != hash_token(token)
    assert verify_token_hash(token, hash_token(token))


def test_verification_rejects_wrong_tokens_and_malformed_hashes() -> None:
    digest = hash_token("correct-horse")
    assert verify_token_hash("correct-horse", digest)
    assert not verify_token_hash("correct-horse ", digest)
    assert not verify_token_hash("wrong", digest)
    assert not verify_token_hash("", digest)
    assert not verify_token_hash("x", "")
    assert not verify_token_hash("x", "garbage")
    assert not verify_token_hash("x", "pbkdf2_sha256$notanumber$aa$bb")


def test_empty_token_cannot_be_hashed() -> None:
    with pytest.raises(ConfigurationError):
        hash_token("")


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("BEARER  abc  ", "abc"),
        ("Basic abc", None),
        ("abc", None),
        ("Bearer", None),
        ("Bearer   ", None),
        ("", None),
        (None, None),
    ],
)
def test_authorization_header_parsing(header: str | None, expected: str | None) -> None:
    assert parse_authorization(header) == expected


def test_verifier_accepts_a_configured_token_and_rejects_others() -> None:
    token = generate_token()
    settings = Settings(api_token_hashes=[hash_token(token)])
    verifier = TokenVerifier.from_settings(settings)
    assert verifier.enabled
    assert verifier.verify(token)
    assert not verifier.verify(generate_token())


def test_plaintext_configured_tokens_are_hashed_and_not_retained() -> None:
    settings = Settings(api_tokens=["plain-secret-token"])
    verifier = TokenVerifier.from_settings(settings)
    assert verifier.verify("plain-secret-token")
    assert all("plain-secret-token" not in digest for digest in verifier.hashes)


def test_require_raises_the_same_error_for_absent_and_wrong_tokens() -> None:
    """Distinguishing the two would tell an attacker which half to work on."""
    verifier = TokenVerifier.from_settings(Settings(api_tokens=["good"]))
    with pytest.raises(AuthenticationError) as absent:
        verifier.require(None)
    with pytest.raises(AuthenticationError) as wrong:
        verifier.require("Bearer bad")
    assert str(absent.value) == str(wrong.value)


def test_auth_enabled_with_no_tokens_is_a_configuration_error_not_an_open_door() -> None:
    verifier = TokenVerifier(hashes=[], enabled=True)
    with pytest.raises(ConfigurationError):
        verifier.require("Bearer anything")


def test_disabled_verifier_lets_everything_through() -> None:
    verifier = TokenVerifier(hashes=[], enabled=False)
    verifier.require(None)
    assert verifier.verify(None)


def test_repeated_verification_is_cached_and_therefore_fast() -> None:
    """PBKDF2 is deliberately slow; a request path that pays for it every time is not."""
    token = generate_token()
    verifier = TokenVerifier.from_settings(Settings(api_token_hashes=[hash_token(token)]))
    verifier.verify(token)

    start = time.perf_counter()
    for _ in range(300):
        assert verifier.verify(token)
    assert time.perf_counter() - start < 0.25


def test_rejected_token_cache_is_bounded() -> None:
    """A flood of bad tokens must not grow the process without limit."""
    verifier = TokenVerifier(hashes=[hash_token("good")], enabled=True)
    for index in range(5000):
        verifier.verify(f"bad-{index}")
    assert len(verifier._rejected) <= 4096


def test_legacy_sha256_hashes_still_verify() -> None:
    """An operator who generated hashes with an older build is not locked out."""
    import hashlib
    import secrets

    token = "legacy-token"
    salt = secrets.token_bytes(16)
    stored = f"sha256${salt.hex()}${hashlib.sha256(salt + token.encode()).hexdigest()}"
    assert verify_token_hash(token, stored)
    assert not verify_token_hash("other", stored)
