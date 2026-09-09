"""The command line, exercised through Typer's runner."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from test_hub.cli import app as cli_app
from test_hub.version import __version__
from tests.paths import junit


@pytest.fixture
def runner() -> CliRunner:
    from test_hub.cli import _register

    _register()
    return CliRunner()


@pytest.fixture
def cli_env(tmp_path: Path) -> dict[str, str]:
    """A clean environment pointed at a throwaway data directory."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("TEST_HUB_")}
    env["TEST_HUB_DATA_DIR"] = str(tmp_path / "data")
    env["TEST_HUB_STORE_RAW_REPORTS"] = "false"
    return env


def run(runner: CliRunner, env: dict[str, str], *args: str) -> Any:
    return runner.invoke(cli_app, list(args), env=env, catch_exceptions=False)


def test_version(runner: CliRunner, cli_env: dict[str, str]) -> None:
    result = run(runner, cli_env, "--version")
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_every_command(runner: CliRunner, cli_env: dict[str, str]) -> None:
    result = run(runner, cli_env, "--help")
    assert result.exit_code == 0
    for command in ("serve", "ingest", "publish", "export", "demo", "formats", "token"):
        assert command in result.stdout


def test_formats_lists_the_installed_adapters(runner: CliRunner, cli_env: dict[str, str]) -> None:
    result = run(runner, cli_env, "formats")
    assert result.exit_code == 0
    assert "junit-xml" in result.stdout
    assert "testhub-json" in result.stdout


def test_formats_json_is_machine_readable(runner: CliRunner, cli_env: dict[str, str]) -> None:
    result = run(runner, cli_env, "formats", "--json")
    rows = json.loads(result.stdout)
    assert {row["name"] for row in rows} >= {"junit-xml", "testhub-json"}


def test_ingest_imports_a_report(runner: CliRunner, cli_env: dict[str, str]) -> None:
    result = run(
        runner,
        cli_env,
        "ingest",
        str(junit("simple.xml")),
        "--project",
        "payments-api",
        "--team",
        "payments",
        "--type",
        "component",
        "--environment",
        "staging",
    )
    assert result.exit_code == 0
    assert "imported run" in result.stdout
    assert "junit-xml" in result.stdout
    assert "5 tests" in result.stdout


def test_ingest_json_output_is_the_import_result(
    runner: CliRunner, cli_env: dict[str, str]
) -> None:
    result = run(runner, cli_env, "ingest", str(junit("simple.xml")), "--project", "p", "--json")
    body = json.loads(result.stdout)
    assert body["adapter"] == "junit-xml"
    assert body["counts"]["total"] == 5
    assert body["schema_version"] == 1


def test_ingest_reports_parser_warnings(runner: CliRunner, cli_env: dict[str, str]) -> None:
    result = run(runner, cli_env, "ingest", str(junit("weird-statuses.xml")), "--project", "p")
    assert "parser warning" in result.stdout
    assert "unknown_status" in result.stdout


def test_ingest_refuses_a_file_it_cannot_parse(
    runner: CliRunner, cli_env: dict[str, str], tmp_path: Path
) -> None:
    junk = tmp_path / "notes.txt"
    junk.write_text("this is not a test report", encoding="utf-8")
    result = runner.invoke(cli_app, ["ingest", str(junk), "--project", "p"], env=cli_env)
    assert result.exit_code != 0


def test_ingest_rejects_a_missing_file(runner: CliRunner, cli_env: dict[str, str]) -> None:
    result = runner.invoke(cli_app, ["ingest", "/nope/missing.xml", "--project", "p"], env=cli_env)
    assert result.exit_code != 0


def test_demo_creates_sample_data(runner: CliRunner, cli_env: dict[str, str]) -> None:
    result = run(runner, cli_env, "demo", "--runs", "2", "--days", "3")
    assert result.exit_code == 0
    assert "sample data created" in result.stdout
    assert "projects" in result.stdout
    assert "one run is still open" in result.stdout


def test_demo_is_deterministic(runner: CliRunner, tmp_path: Path) -> None:
    """A fixed seed keeps documentation screenshots matching the product."""
    outputs = []
    for index in range(2):
        env = {k: v for k, v in os.environ.items() if not k.startswith("TEST_HUB_")}
        env["TEST_HUB_DATA_DIR"] = str(tmp_path / f"run{index}")
        from test_hub.cli import _register

        _register()
        result = CliRunner().invoke(
            cli_app, ["demo", "--runs", "2", "--days", "3", "--no-live"], env=env
        )
        outputs.append([line for line in result.stdout.splitlines() if "projects," in line])
    assert outputs[0] == outputs[1]


def test_export_writes_one_self_contained_file(
    runner: CliRunner, cli_env: dict[str, str], tmp_path: Path
) -> None:
    run(runner, cli_env, "ingest", str(junit("simple.xml")), "--project", "p")
    output = tmp_path / "report.html"
    result = run(runner, cli_env, "export", "--output", str(output), "--days", "3650")
    assert result.exit_code == 0
    assert output.is_file()

    html = output.read_text(encoding="utf-8")
    assert 'src="assets/' not in html
    assert "<style>" in html
    assert "self-contained" in result.stdout


def test_export_from_a_directory_of_reports(
    runner: CliRunner, cli_env: dict[str, str], tmp_path: Path
) -> None:
    """The documented `--input ./results` flow, which never touches the real store."""
    results = tmp_path / "results"
    results.mkdir()
    for name in ("simple.xml", "nested-suites.xml"):
        (results / name).write_bytes(junit(name).read_bytes())

    output = tmp_path / "from-files.html"
    result = run(
        runner,
        cli_env,
        "export",
        "--input",
        str(results),
        "--output",
        str(output),
        "--project",
        "payments-api",
        "--days",
        "3650",
    )
    assert result.exit_code == 0
    assert output.is_file()
    assert output.stat().st_size > 50_000


def test_export_from_input_requires_a_project(
    runner: CliRunner, cli_env: dict[str, str], tmp_path: Path
) -> None:
    results = tmp_path / "results"
    results.mkdir()
    (results / "simple.xml").write_bytes(junit("simple.xml").read_bytes())
    result = runner.invoke(
        cli_app,
        ["export", "--input", str(results), "--output", str(tmp_path / "x.html")],
        env=cli_env,
    )
    assert result.exit_code != 0


def test_export_from_an_empty_directory_says_so(
    runner: CliRunner, cli_env: dict[str, str], tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = runner.invoke(
        cli_app,
        ["export", "--input", str(empty), "--output", str(tmp_path / "x.html"), "-p", "p"],
        env=cli_env,
    )
    assert result.exit_code != 0


def test_token_create_prints_a_token_and_its_hash(
    runner: CliRunner, cli_env: dict[str, str]
) -> None:
    result = run(runner, cli_env, "token", "create")
    assert result.exit_code == 0
    assert "ths_" in result.stdout
    assert "pbkdf2_sha256$" in result.stdout
    assert "TEST_HUB_API_TOKEN_HASHES" in result.stdout


def test_token_hash_reads_the_environment_never_an_argument(
    runner: CliRunner, cli_env: dict[str, str]
) -> None:
    """A token in argv is world-readable in `ps` and lands in shell history."""
    from test_hub.api.security import verify_token_hash

    cli_env["TEST_HUB_TOKEN"] = "a-secret-token"
    result = run(runner, cli_env, "token", "hash")
    assert result.exit_code == 0
    digest = result.stdout.strip()
    assert verify_token_hash("a-secret-token", digest)

    help_text = run(runner, cli_env, "token", "hash", "--help").stdout
    assert "--token-env" in help_text
    assert "--token " not in help_text


def test_token_hash_without_the_variable_set_fails_clearly(
    runner: CliRunner, cli_env: dict[str, str]
) -> None:
    cli_env.pop("TEST_HUB_TOKEN", None)
    result = runner.invoke(cli_app, ["token", "hash"], env=cli_env)
    assert result.exit_code == 2


def test_publish_help_offers_only_safe_token_options(
    runner: CliRunner, cli_env: dict[str, str]
) -> None:
    help_text = run(runner, cli_env, "publish", "--help").stdout
    assert "--token-env" in help_text
    assert "--token-file" in help_text
    assert "--token " not in help_text
