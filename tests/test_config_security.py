"""Secure defaults, and the gate that refuses an unauthenticated public bind."""

from __future__ import annotations

from pathlib import Path

import pytest

from test_hub.config import DEFAULT_REDACTION_PATTERNS, Settings, default_data_dir
from test_hub.errors import ConfigurationError
from test_hub.logging import Redactor


def test_defaults_are_the_safe_ones() -> None:
    settings = Settings()
    assert settings.host == "127.0.0.1"
    assert settings.is_loopback
    assert settings.auth_enabled is False
    assert settings.cors_allow_origins == []
    assert settings.redaction_enabled is True
    assert "default-src 'none'" in settings.csp
    assert "frame-ancestors 'none'" in settings.csp


def test_loopback_needs_no_authentication() -> None:
    assert Settings(host="127.0.0.1").check_bind_safety() == []
    assert Settings(host="localhost").check_bind_safety() == []
    assert Settings(host="::1").check_bind_safety() == []


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "10.0.0.5", "::"])
def test_public_bind_without_auth_is_refused(host: str) -> None:
    """The single most important line of defence in the product."""
    with pytest.raises(ConfigurationError, match="refusing to bind"):
        Settings(host=host).check_bind_safety()


def test_public_bind_with_tokens_is_allowed_silently() -> None:
    settings = Settings(host="0.0.0.0", api_tokens=["a-token"])
    assert settings.auth_enabled is True
    assert settings.check_bind_safety() == []


def test_public_bind_with_explicit_opt_out_warns_loudly_every_time() -> None:
    warnings = Settings(host="0.0.0.0", insecure_no_auth=True).check_bind_safety()
    assert any("AUTHENTICATION IS DISABLED" in warning for warning in warnings)


def test_wildcard_cors_warns() -> None:
    warnings = Settings(
        host="0.0.0.0", insecure_no_auth=True, cors_allow_origins=["*"]
    ).check_bind_safety()
    assert any("CORS" in warning for warning in warnings)


def test_configuring_tokens_turns_auth_on_by_itself() -> None:
    """Nobody should have to set two variables to get one behaviour."""
    assert Settings(api_tokens=["x"]).auth_enabled
    assert Settings(api_token_hashes=["pbkdf2_sha256$1$aa$bb"]).auth_enabled
    assert not Settings().auth_enabled


def test_list_settings_accept_comma_separated_values_in_python() -> None:
    settings = Settings(api_tokens="one,two , three", cors_allow_origins="https://a,https://b")
    assert len(settings.api_tokens) == 3
    assert settings.cors_allow_origins == ["https://a", "https://b"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("alpha,beta", ["alpha", "beta"]),
        ("alpha, beta , gamma", ["alpha", "beta", "gamma"]),
        ("alpha", ["alpha"]),
        ('["alpha","beta"]', ["alpha", "beta"]),
        ("", []),
    ],
)
def test_list_settings_parse_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[str]
) -> None:
    """Regression: this path is not the same one as passing a Python keyword.

    pydantic-settings JSON-decodes a complex field's raw environment value
    before any field validator runs, so before ``CsvList`` carried ``NoDecode``,
    ``TEST_HUB_API_TOKENS=alpha,beta`` raised ``SettingsError`` and the process
    exited before it bound a port — while the identical string passed to
    ``Settings(...)`` worked perfectly. The container failed on first boot and
    the unit test stayed green.
    """
    monkeypatch.setenv("TEST_HUB_API_TOKENS", raw)
    assert Settings().api_tokens == expected


def test_token_hashes_survive_the_environment_with_their_dollar_signs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PBKDF2 hash is four ``$``-separated fields; splitting must not touch them."""
    digest = "pbkdf2_sha256$120000$0011aabb$ccdd2233"
    monkeypatch.setenv("TEST_HUB_API_TOKEN_HASHES", f"{digest},{digest}")
    settings = Settings()
    assert settings.api_token_hashes == [digest, digest]
    assert settings.auth_enabled is True


def test_a_malformed_json_array_is_rejected_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_HUB_CORS_ALLOW_ORIGINS", "[not json")
    with pytest.raises(ValueError, match="JSON array"):
        Settings()


def test_every_list_setting_accepts_the_comma_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """All five, because the bug was per-field and only one was covered."""
    monkeypatch.setenv("TEST_HUB_API_TOKENS", "a,b")
    monkeypatch.setenv("TEST_HUB_API_TOKEN_HASHES", "x,y")
    monkeypatch.setenv("TEST_HUB_CORS_ALLOW_ORIGINS", "https://a,https://b")
    monkeypatch.setenv("TEST_HUB_TRUSTED_HOSTS", "a.example,b.example")
    monkeypatch.setenv("TEST_HUB_REDACTION_PATTERNS", r"secret=(\w+),token=(\w+)")
    settings = Settings()
    assert settings.api_tokens == ["a", "b"]
    assert settings.api_token_hashes == ["x", "y"]
    assert settings.cors_allow_origins == ["https://a", "https://b"]
    assert settings.trusted_hosts == ["a.example", "b.example"]
    assert len(settings.redaction_patterns) == 2


def test_invalid_redaction_pattern_is_rejected_at_startup() -> None:
    with pytest.raises(ValueError, match="invalid redaction pattern"):
        Settings(redaction_patterns=["(unclosed"])


def test_data_directory_is_outside_the_working_tree(tmp_path: Path) -> None:
    """Two `test-hub serve` runs from different folders must be one Test Hub."""
    assert default_data_dir().is_absolute()
    assert Settings(data_dir=tmp_path).resolved_artifacts_dir == tmp_path / "artifacts"


def test_ensure_directories_creates_private_directories(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "nested" / "data")
    settings.ensure_directories()
    assert settings.data_dir.is_dir()
    assert settings.resolved_artifacts_dir.is_dir()


def test_sqlite_url_is_derived_from_the_data_directory(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    assert settings.resolved_database_url.startswith("sqlite+pysqlite:///")
    assert settings.resolved_database_url.endswith("test-hub.db")
    assert Settings(database_url="sqlite://").resolved_database_url == "sqlite://"


@pytest.mark.parametrize(
    ("text", "must_not_contain"),
    [
        ("Authorization: Bearer sk-live-1234567890abcdef", "sk-live-1234567890abcdef"),
        ("postgresql://user:hunter2@db.internal/app", "hunter2"),
        ('{"api_key": "AKIAIOSFODNN7EXAMPLE"}', "AKIAIOSFODNN7EXAMPLE"),
        ("password=correcthorsebatterystaple", "correcthorsebatterystaple"),
        ("X-API-Key: ghp_abcdefghijklmnopqrstuvwxyz01", "ghp_abcdefghijklmnopqrstuvwxyz01"),
        ("token: xoxb-1234567890-abcdefghijkl", "xoxb-1234567890-abcdefghijkl"),
    ],
)
def test_default_redaction_patterns_remove_real_secret_shapes(
    text: str, must_not_contain: str
) -> None:
    """Test output leaks credentials constantly. This is where they stop."""
    redacted = Redactor(DEFAULT_REDACTION_PATTERNS).redact(text)
    assert redacted is not None
    assert must_not_contain not in redacted
    assert "***REDACTED***" in redacted


def test_redaction_leaves_innocent_text_alone() -> None:
    text = "AssertionError: expected 200 but got 502 at checkout.py:84"
    assert Redactor(DEFAULT_REDACTION_PATTERNS).redact(text) == text


def test_redaction_keeps_the_key_readable() -> None:
    """`password=***` is useful; a blanked-out line is not."""
    redacted = Redactor(DEFAULT_REDACTION_PATTERNS).redact("db password=s3cret going in")
    assert redacted is not None
    assert redacted.startswith("db password=")
    assert redacted.endswith("going in")


def test_redactor_handles_none_and_empty() -> None:
    redactor = Redactor(DEFAULT_REDACTION_PATTERNS)
    assert redactor.redact(None) is None
    assert redactor.redact("") == ""


def test_redactor_walks_nested_mappings() -> None:
    redactor = Redactor(DEFAULT_REDACTION_PATTERNS)
    result = redactor.redact_mapping(
        {"env": {"DATABASE_URL": "postgres://u:pw123456@h/d"}, "n": 5, "l": ["token=abcdef"]}
    )
    assert "pw123456" not in str(result)
    assert "abcdef" not in str(result)
    assert result["n"] == 5


def test_a_broken_custom_pattern_is_skipped_not_fatal() -> None:
    """A bad pattern in configuration must not take the process down."""
    redactor = Redactor(["(unclosed", r"password=(\w+)"])
    assert redactor.redact("password=abc") == "password=***REDACTED***"


def test_patterns_without_a_capture_group_are_ignored() -> None:
    """Replacing group 1 is the contract; a pattern with no group cannot honour it."""
    assert Redactor([r"secret"]).redact("secret") == "secret"
