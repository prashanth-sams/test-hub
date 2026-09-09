"""Bearer-token authentication, and the token hashing behind it.

Test Hub stores **only hashes**. A stolen database gives an attacker a list of
PBKDF2 digests, not a list of working tokens. The plaintext exists exactly once,
in the terminal output of ``test-hub token create``, and it is the operator's job
to put it somewhere safe from there.

Why PBKDF2 rather than a bare SHA-256 of a random token: the tokens *we* generate
carry 256 bits of entropy and a bare hash would be fine for them, but nothing
stops an operator setting ``TEST_HUB_API_TOKENS=letmein``. PBKDF2 makes that
mistake expensive to exploit instead of free. To keep the per-request cost off
the hot path, a successful verification is memoized for the life of the process,
keyed on a SHA-256 of the presented token — so the KDF runs once per distinct
token, not once per request.

Everything here is timing-safe: :func:`hmac.compare_digest` for the comparison,
and a uniform "invalid token" response whether the token was unknown, malformed
or simply absent.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field

from test_hub.errors import AuthenticationError, ConfigurationError

__all__ = [
    "PBKDF2_ITERATIONS",
    "TokenVerifier",
    "generate_token",
    "hash_token",
    "parse_authorization",
    "verify_token_hash",
]

PBKDF2_ITERATIONS = 120_000
_ALGORITHM = "sha256"
_SALT_BYTES = 16
_TOKEN_BYTES = 32


def generate_token() -> str:
    """A fresh API token: 256 bits, URL-safe, prefixed so it is greppable.

    The ``ths_`` prefix means a leaked token can be found by secret scanners and
    recognised in a log by a human, which is worth more than the four characters
    it costs.
    """
    return f"ths_{secrets.token_urlsafe(_TOKEN_BYTES)}"


def hash_token(
    token: str, *, salt: bytes | None = None, iterations: int = PBKDF2_ITERATIONS
) -> str:
    """Hash a token into the storable ``pbkdf2_sha256$iterations$salt$hash`` form."""
    if not token:
        raise ConfigurationError("refusing to hash an empty token")
    salt = salt or secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(_ALGORITHM, token.encode("utf-8"), salt, iterations)
    return f"pbkdf2_{_ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def verify_token_hash(token: str, stored: str) -> bool:
    """Constant-time check of ``token`` against one stored hash.

    Accepts the legacy ``sha256$salt$hash`` form as well, so an operator who
    generated hashes with an older build is not locked out by an upgrade.
    """
    if not token or not stored:
        return False
    parts = stored.split("$")
    try:
        if len(parts) == 4 and parts[0] == f"pbkdf2_{_ALGORITHM}":
            iterations = int(parts[1])
            salt = bytes.fromhex(parts[2])
            expected = bytes.fromhex(parts[3])
            actual = hashlib.pbkdf2_hmac(_ALGORITHM, token.encode("utf-8"), salt, iterations)
        elif len(parts) == 3 and parts[0] == _ALGORITHM:
            salt = bytes.fromhex(parts[1])
            expected = bytes.fromhex(parts[2])
            actual = hashlib.sha256(salt + token.encode("utf-8")).digest()
        else:
            return False
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def parse_authorization(header: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer …`` header.

    Returns ``None`` for anything else — including ``Basic`` — rather than
    raising, so the caller decides whether a missing credential is an error.
    """
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None


@dataclass
class TokenVerifier:
    """Checks presented tokens against the configured hashes.

    Built once at startup from :class:`~test_hub.config.Settings`. Plaintext
    tokens from configuration are hashed immediately and the plaintext is not
    retained anywhere on the instance.
    """

    hashes: list[str] = field(default_factory=list)
    enabled: bool = True
    _accepted: set[str] = field(default_factory=set, repr=False)
    _rejected: set[str] = field(default_factory=set, repr=False)

    @classmethod
    def from_settings(cls, settings: object) -> TokenVerifier:
        hashes = list(getattr(settings, "api_token_hashes", []) or [])
        for plaintext in getattr(settings, "api_tokens", []) or []:
            hashes.append(hash_token(plaintext))
        return cls(hashes=hashes, enabled=bool(getattr(settings, "auth_enabled", False)))

    @classmethod
    def admin_from_settings(cls, settings: object) -> TokenVerifier:
        """The verifier for destructive maintenance.

        Always ``enabled``, whatever ``auth_enabled`` says, and deliberately so:
        this verifier answers "is this caller an administrator", which is not the
        same question as "is authentication switched on". An instance with no
        admin tokens configured therefore accepts no admin token at all, and
        :func:`test_hub.api.deps.require_admin` is the only thing that decides
        whether an unauthenticated loopback caller may proceed without one.
        """
        hashes = list(getattr(settings, "admin_token_hashes", []) or [])
        for plaintext in getattr(settings, "admin_tokens", []) or []:
            hashes.append(hash_token(plaintext))
        return cls(hashes=hashes, enabled=True)

    def __bool__(self) -> bool:
        return bool(self.hashes)

    def verify(self, token: str | None) -> bool:
        """True when ``token`` matches one configured hash."""
        if not self.enabled:
            return True
        if not token:
            return False
        fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if fingerprint in self._accepted:
            return True
        if fingerprint in self._rejected:
            return False
        for stored in self.hashes:
            if verify_token_hash(token, stored):
                self._accepted.add(fingerprint)
                return True
        # Bounded, so a flood of bad tokens cannot grow the process without limit.
        if len(self._rejected) < 4096:
            self._rejected.add(fingerprint)
        return False

    def require(self, header: str | None) -> None:
        """Raise :class:`AuthenticationError` unless the header carries a valid token.

        The message is identical for a missing, malformed and wrong token: telling
        a caller *which* of those it was is free information for an attacker.
        """
        if not self.enabled:
            return
        if not self.hashes:
            raise ConfigurationError(
                "authentication is enabled but no tokens are configured; "
                "create one with `test-hub token create`"
            )
        if not self.verify(parse_authorization(header)):
            raise AuthenticationError(
                "a valid bearer token is required; send it as `Authorization: Bearer <token>`"
            )
