"""Configuration, with secure defaults that have to be opted out of loudly.

Everything is settable from the environment with a ``TEST_HUB_`` prefix, from a
``.env`` file, or from the CLI. The defaults are the ones you want on a laptop:
loopback only, no auth needed because nothing else can reach it, no CORS, modest
size limits, data under the platform's user-data directory.

The one rule worth knowing: **binding to a non-loopback interface requires
authentication**. Either configure tokens or pass ``--insecure-no-auth``, which
logs a warning on every start and puts a banner in the dashboard. There is no
silent path to an open, unauthenticated Test Hub on a network.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from test_hub.errors import ConfigurationError

__all__ = ["CsvList", "Settings", "default_data_dir", "get_settings", "reset_settings_cache"]

#: A list that may be written as ``a,b,c`` in the environment.
#:
#: ``NoDecode`` is load-bearing. Without it, pydantic-settings JSON-decodes a
#: complex field's raw environment value *before* any field validator runs, so
#: ``TEST_HUB_API_TOKENS=alpha,beta`` raises ``SettingsError`` and the process
#: exits before it can reach the ``mode="before"`` validator that was supposed
#: to split it. That failure happens only through the environment — passing the
#: same string to ``Settings(...)`` in Python works — which is exactly the kind
#: of bug that survives a unit test and breaks a container on first boot.
CsvList = Annotated[list[str], NoDecode]

ENV_PREFIX = "TEST_HUB_"

#: Redaction patterns applied to log records and to stored failure text. Each is
#: matched case-insensitively; the capturing group is what gets replaced, so the
#: key stays readable and only the value disappears.
DEFAULT_REDACTION_PATTERNS: tuple[str, ...] = (
    # Order matters. The scheme-specific patterns run first so that
    # `Authorization: Bearer <token>` loses the token rather than the word
    # "Bearer" — the generic key=value rule below would otherwise capture the
    # scheme and stop.
    r"(?i)\bbearer\s+([A-Za-z0-9._\-]{8,})",
    r"(?i)\bbasic\s+([A-Za-z0-9+/=]{8,})",
    r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----([\s\S]+?)-----END",
    r"(?i)\b(?:postgres|postgresql|mysql|mongodb|redis|amqp)://[^:/\s]+:([^@\s]+)@",
    r"(?i)\bAKIA([0-9A-Z]{16})",
    r"(?i)\bghp_([A-Za-z0-9]{20,})",
    r"(?i)\bxox[baprs]-([A-Za-z0-9-]{10,})",
    r"(?i)\beyJ[A-Za-z0-9_\-]{8,}\.([A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,})",
    r"(?i)\b(?:api[_-]?key|apikey|access[_-]?key|token|secret|password|passwd|pwd|"
    r"authorization|auth[_-]?token|private[_-]?key|client[_-]?secret)\b[\"']?\s*[=:]\s*"
    r"[\"']?([^\s\"',;&]{4,})",
)


def default_data_dir() -> Path:
    """Where Test Hub keeps its database and artifacts when nobody says otherwise.

    Follows the platform convention rather than dropping a directory in the
    working tree, so ``test-hub serve`` run from two different folders is one
    Test Hub, not two.
    """
    override = os.environ.get(f"{ENV_PREFIX}DATA_DIR")
    if override:
        return Path(override).expanduser()
    # Read through a variable: a type checker narrows the `sys.platform` literal
    # to whatever platform it happens to be running on and then calls the other
    # two branches unreachable, which is true for the checker and false for the
    # program.
    platform = str(sys.platform)
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "test-hub"
    if platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "test-hub"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "test-hub"


def _is_loopback(host: str) -> bool:
    """True when binding to ``host`` cannot be reached from another machine."""
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Settings(BaseSettings):
    """Every knob, in one place."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------- server
    host: str = Field(default="127.0.0.1", description="Interface to bind. Loopback by default.")
    port: Annotated[int, Field(ge=1, le=65535)] = 8000
    root_path: str = Field(default="", description="Mount prefix when behind a reverse proxy.")

    # --------------------------------------------------------------- data
    data_dir: Path = Field(default_factory=default_data_dir)
    database_url: str | None = Field(
        default=None,
        description="SQLAlchemy URL. Defaults to SQLite inside data_dir. "
        "The interfaces support other backends; only SQLite is shipped.",
    )
    artifacts_dir: Path | None = Field(default=None, description="Defaults to data_dir/artifacts.")
    sqlite_busy_timeout_ms: int = Field(
        default=10_000, description="How long a writer waits behind another writer."
    )

    # --------------------------------------------------------- ingestion
    max_report_bytes: Annotated[int, Field(ge=1024)] = Field(
        default=64 * 1024 * 1024, description="Largest report file accepted, before decompression."
    )
    max_artifact_bytes: Annotated[int, Field(ge=1024)] = Field(default=128 * 1024 * 1024)
    max_decompressed_bytes: Annotated[int, Field(ge=1024)] = Field(
        default=512 * 1024 * 1024,
        description="Ceiling on what a compressed upload may expand to — zip-bomb guard.",
    )
    max_text_field_bytes: Annotated[int, Field(ge=256)] = Field(
        default=256 * 1024,
        description="Longest stack trace / log stored per attempt; longer text is truncated "
        "and the attempt is flagged, never silently cut.",
    )
    max_events_per_batch: Annotated[int, Field(ge=1)] = 5000
    store_raw_reports: bool = Field(
        default=True, description="Keep the original uploaded report as an artifact."
    )

    # ------------------------------------------------------------ security
    auth_enabled: bool = Field(
        default=False, description="Require a bearer token on writes (and on reads if configured)."
    )
    require_auth_for_reads: bool = False
    api_tokens: CsvList = Field(
        default_factory=list,
        description="Bearer tokens in plaintext. Prefer api_token_hashes or the token store; "
        "these are hashed at startup and never logged.",
    )
    api_token_hashes: CsvList = Field(
        default_factory=list,
        description="Pre-hashed tokens, `sha256$<salt-hex>$<hash-hex>`. The preferred form.",
    )
    insecure_no_auth: bool = Field(
        default=False,
        description="Explicitly allow a non-loopback bind with no authentication. Development "
        "only; warns on every start.",
    )
    cors_allow_origins: CsvList = Field(
        default_factory=list, description="Empty means no CORS headers at all."
    )
    trusted_hosts: CsvList = Field(default_factory=list)
    csp: str = Field(
        default=(
            "default-src 'none'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "form-action 'none'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'"
        ),
        description="Content-Security-Policy for the dashboard and API.",
    )
    redaction_patterns: CsvList = Field(default_factory=lambda: list(DEFAULT_REDACTION_PATTERNS))
    redaction_enabled: bool = True
    redaction_placeholder: str = "***REDACTED***"

    # --------------------------------------------------------------- live
    sse_heartbeat_seconds: Annotated[float, Field(gt=0)] = 15.0
    sse_replay_buffer: Annotated[int, Field(ge=0)] = Field(
        default=1000, description="Events kept per workspace for Last-Event-ID replay."
    )
    sse_max_clients: Annotated[int, Field(ge=1)] = 200

    # ------------------------------------------------------------ behaviour
    workspace_slug: str = "default"
    workspace_name: str = "Default workspace"
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_format: Literal["text", "json"] = "text"
    history_window_days: Annotated[int, Field(ge=1)] = 30
    flaky_window_runs: Annotated[int, Field(ge=2)] = 20
    open_browser: bool = False

    # ------------------------------------------------------------ retention
    #
    # These bound how long data is *kept*, which is a different question from
    # `history_window_days` above — that one bounds what the overview *shows*
    # and deletes nothing. Nothing here sweeps on its own either: the settings
    # are defaults for `test-hub prune`, which an operator schedules (ADR-0002
    # D1). A setting that silently deleted data on a timer would take SQLite's
    # single write lock at an hour nobody chose.
    retention_days: Annotated[int | None, Field(ge=1)] = Field(
        default=None,
        description=(
            "Default cutoff for `test-hub prune`, in days. Nothing is deleted until "
            "that command runs; schedule it."
        ),
    )
    retention_min_runs: Annotated[int, Field(ge=0)] = Field(
        default=5,
        description=(
            "Runs always kept per project, however old. 0 disables the floor. Stops a "
            "cutoff from deleting a dormant project out of existence."
        ),
    )
    artifact_gc_grace_hours: Annotated[float, Field(ge=0)] = Field(
        default=24.0,
        description=(
            "A content-addressed blob touched more recently than this is never "
            "unlinked, even with no rows referencing it."
        ),
    )

    # ------------------------------------------------------------ validators

    @field_validator(
        "api_tokens",
        "api_token_hashes",
        "cors_allow_origins",
        "trusted_hosts",
        "redaction_patterns",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        """Accept ``a,b,c`` from the environment as well as a JSON array.

        Both forms are supported because both are natural: a JSON array is what
        a configuration-management tool emits, and a comma-separated string is
        what a person types into a Compose file.

        The decoding happens *here* rather than in pydantic-settings because
        these fields are annotated :data:`CsvList` — see the note there for why
        letting it decode them first made the comma form impossible.
        """
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"value starts with '[' so it was read as a JSON array, "
                    f"but it does not parse: {exc}"
                ) from exc
        return [part.strip() for part in stripped.split(",") if part.strip()]

    @field_validator("redaction_patterns")
    @classmethod
    def _compilable(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:  # pragma: no cover - defensive
                raise ValueError(f"invalid redaction pattern {pattern!r}: {exc}") from exc
        return value

    @field_validator("data_dir", "artifacts_dir")
    @classmethod
    def _expand(cls, value: Path | None) -> Path | None:
        return value.expanduser().resolve() if value is not None else None

    @model_validator(mode="after")
    def _auth_follows_exposure(self) -> Settings:
        """Turn auth on by itself when tokens exist, and refuse an open bind."""
        if (self.api_tokens or self.api_token_hashes) and not self.auth_enabled:
            object.__setattr__(self, "auth_enabled", True)
        return self

    # -------------------------------------------------------------- derived

    @property
    def is_loopback(self) -> bool:
        return _is_loopback(self.host)

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+pysqlite:///{self.data_dir / 'test-hub.db'}"

    @property
    def resolved_artifacts_dir(self) -> Path:
        return self.artifacts_dir or (self.data_dir / "artifacts")

    def ensure_directories(self) -> None:
        """Create the data directories, with permissions that keep them private."""
        for path in (self.data_dir, self.resolved_artifacts_dir):
            path.mkdir(parents=True, exist_ok=True)
            # Windows and some network filesystems have no concept of this mode;
            # the directory is still created, which is what matters.
            with contextlib.suppress(OSError):
                path.chmod(0o700)

    def check_bind_safety(self) -> list[str]:
        """Validate the host/auth combination.

        Returns warnings to log; raises :class:`ConfigurationError` when the
        combination is unsafe and has not been explicitly opted into. This is the
        single gate described in ADR-0001 D8, called by ``serve`` before binding.
        """
        warnings: list[str] = []
        if self.is_loopback:
            if self.insecure_no_auth:
                warnings.append(
                    "--insecure-no-auth is redundant on a loopback bind and can be dropped."
                )
            return warnings

        if not self.auth_enabled and not self.insecure_no_auth:
            raise ConfigurationError(
                f"refusing to bind {self.host}:{self.port} with no authentication. "
                "Create a token with `test-hub token create`, set TEST_HUB_API_TOKEN_HASHES, "
                "or pass --insecure-no-auth if this really is a throwaway development host.",
                detail={"host": self.host, "port": self.port},
            )
        if self.insecure_no_auth:
            warnings.append(
                f"AUTHENTICATION IS DISABLED and Test Hub is bound to {self.host}:{self.port}. "
                "Anyone who can reach this port can read and write every test result."
            )
        if "*" in self.cors_allow_origins:
            warnings.append(
                "CORS is set to '*'. Any website a user visits can read this API in their browser."
            )
        return warnings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings, read once."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings. Used by tests and by the CLI after it applies flags."""
    get_settings.cache_clear()
