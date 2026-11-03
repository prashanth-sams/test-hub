"""Choosing the database: SQLite unless a PostgreSQL URL says otherwise.

None of these need a database server. They pin how the choice is made — the
URL forms accepted, what is refused and how loudly, and that a password never
reaches a banner, a log line or a command line.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from test_hub.cli import app as cli_app
from test_hub.config import DEFAULT_REDACTION_PATTERNS, Settings, normalize_database_url
from test_hub.errors import ConfigurationError
from test_hub.logging import Redactor
from test_hub.storage import create_database
from test_hub.storage.db import describe_database_url

# ------------------------------------------------------------------ URLs


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgres://hub:pw@db:5432/hub", "postgresql+psycopg://hub:pw@db:5432/hub"),
        ("postgresql://hub@db/hub", "postgresql+psycopg://hub@db/hub"),
        ("PostgreSQL://hub@db/hub", "postgresql+psycopg://hub@db/hub"),
        ("  postgresql://hub@db/hub \n", "postgresql+psycopg://hub@db/hub"),
        # A driver someone chose on purpose is theirs to keep.
        ("postgresql+psycopg2://hub@db/hub", "postgresql+psycopg2://hub@db/hub"),
        ("sqlite:////var/lib/test-hub/test-hub.db", "sqlite:////var/lib/test-hub/test-hub.db"),
    ],
)
def test_urls_gain_the_shipped_driver(given: str, expected: str) -> None:
    assert normalize_database_url(given) == expected


def test_sqlite_is_the_default(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    assert settings.database_backend == "sqlite"
    assert settings.resolved_database_url.endswith("test-hub.db")


def test_a_blank_url_still_means_sqlite(tmp_path: Path) -> None:
    """Compose passes `TEST_HUB_DATABASE_URL: ""` when an interpolated variable is unset."""
    assert Settings(data_dir=tmp_path, database_url="  ").database_backend == "sqlite"


def test_a_postgresql_url_selects_postgresql(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, database_url="postgres://hub:pw@db/hub")
    assert settings.database_backend == "postgresql"
    assert settings.resolved_database_url.startswith("postgresql+psycopg://")


def test_the_choice_is_read_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_HUB_DATABASE_URL", "postgresql://hub@db/hub")
    assert Settings(data_dir=tmp_path).database_backend == "postgresql"


# --------------------------------------------------------------- refusals


@pytest.mark.parametrize("url", ["mysql://hub:pw@db/hub", "mssql+pyodbc://hub@db/hub"])
def test_unsupported_backends_are_refused_by_name(tmp_path: Path, url: str) -> None:
    with pytest.raises(ConfigurationError, match="SQLite or PostgreSQL") as raised:
        create_database(Settings(data_dir=tmp_path, database_url=url))
    assert raised.value.exit_code == 78
    assert ":pw@" not in str(raised.value.detail), "the password leaked into the error"


def test_a_url_that_is_not_a_url_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="not a database URL"):
        create_database(Settings(data_dir=tmp_path, database_url="localhost:5432"))


def test_a_missing_driver_says_what_to_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stands in for `pip install test-hub` without the `postgres` extra."""
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(ConfigurationError, match=r"pip install 'test-hub\[postgres\]'"):
        create_database(Settings(data_dir=tmp_path, database_url="postgresql://hub@db/hub"))


# --------------------------------------------------------------- secrecy


def test_passwords_are_masked_wherever_the_url_is_shown() -> None:
    shown = describe_database_url("postgresql+psycopg://hub:s3cret@db:5432/hub")
    assert shown == "postgresql+psycopg://hub:***@db:5432/hub"


@pytest.mark.parametrize(
    "line",
    [
        "could not connect: postgresql+psycopg://hub:s3cret@db/hub",
        "DSN postgres://hub:s3cret@db.internal:5432/hub",
    ],
)
def test_driver_qualified_urls_are_redacted_in_logs(line: str) -> None:
    redacted = Redactor(DEFAULT_REDACTION_PATTERNS).redact(line)
    assert redacted is not None
    assert "s3cret" not in redacted


# ------------------------------------------------------------------- CLI


@pytest.fixture
def runner() -> CliRunner:
    from test_hub.cli import _register

    _register()
    return CliRunner()


@pytest.fixture
def cli_env(tmp_path: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("TEST_HUB_")}
    env["TEST_HUB_DATA_DIR"] = str(tmp_path / "data")
    env["NO_COLOR"] = "1"
    return env


def invoke(runner: CliRunner, env: dict[str, str], *args: str) -> Any:
    return runner.invoke(cli_app, list(args), env=env)


def test_a_password_on_the_command_line_is_refused(
    runner: CliRunner, cli_env: dict[str, str]
) -> None:
    """Same rule as tokens: `ps` and shell history would keep it."""
    for command in (["db", "check"], ["serve"], ["demo"], ["prune", "--older-than", "1d"]):
        result = invoke(
            runner, cli_env, *command, "--database-url", "postgresql://hub:s3cret@db/hub"
        )
        assert isinstance(result.exception, ConfigurationError), command
        assert result.exception.exit_code == 78
        assert "TEST_HUB_DATABASE_URL" in result.exception.message
        assert "s3cret" not in result.output


def test_db_check_and_upgrade_report_the_sqlite_schema(
    runner: CliRunner, cli_env: dict[str, str]
) -> None:
    fresh = invoke(runner, cli_env, "db", "check")
    assert fresh.exit_code == 1, fresh.output
    assert "sqlite" in fresh.output and "empty" in fresh.output

    upgraded = invoke(runner, cli_env, "db", "upgrade")
    assert upgraded.exit_code == 0, upgraded.output
    assert "empty → 0003_postgresql_bigint" in upgraded.output

    ready = invoke(runner, cli_env, "db", "check")
    assert ready.exit_code == 0, ready.output
    assert "0003_postgresql_bigint (up to date)" in ready.output
    assert "SQLite" in ready.output

    again = invoke(runner, cli_env, "db", "upgrade")
    assert "already at 0003_postgresql_bigint" in again.output


def test_db_check_on_an_unreachable_server_exits_78(
    runner: CliRunner, cli_env: dict[str, str]
) -> None:
    """Port 1 refuses instantly, so this needs no server and no timeout."""
    result = invoke(
        runner, cli_env, "db", "check", "--database-url", "postgresql://hub@127.0.0.1:1/x"
    )
    assert isinstance(result.exception, ConfigurationError)
    assert result.exception.exit_code == 78
    assert "cannot reach the PostgreSQL database" in result.exception.message
