"""The command line, exercised through Typer's runner."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from test_hub.cli import app as cli_app
from test_hub.config import reset_settings_cache
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
    for command in ("serve", "ingest", "publish", "export", "demo", "prune", "formats", "token"):
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
    # The evidence pages need something to draw; see tests/test_demo_evidence.py.
    assert "files attached" in result.stdout


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


# ------------------------------------------------------------------- prune


def seed_old_run(runner: CliRunner, env: dict[str, str], project: str = "p") -> None:
    """Import a report. The fixture carries its own timestamp, so it is already
    old enough for any cutoff these tests use."""
    run(runner, env, "ingest", str(junit("simple.xml")), "--project", project)
    # `get_settings` is process-wide and cached, and these tests make several
    # invocations inside one process where the real CLI would make one per
    # process. Without this, the second invocation reuses the first one's
    # environment.
    reset_settings_cache()


def everything_said(result: Any) -> str:
    """All the ways a Typer run can report a failure, in one string.

    A `TestHubError` reaches the console script through `main()`, which the
    runner does not call, so it arrives here as `result.exception`; a usage
    error goes to stderr; ordinary output goes to stdout.
    """
    parts = [result.stdout or ""]
    with contextlib.suppress(ValueError):  # capture mode dependent
        parts.append(result.stderr or "")
    if result.exception is not None:
        parts.append(str(result.exception))
    return "\n".join(parts)


def test_prune_dry_run_deletes_nothing(runner: CliRunner, cli_env: dict[str, str]) -> None:
    seed_old_run(runner, cli_env)
    plan = run(runner, cli_env, "prune", "--before", "2030-01-01", "--keep-min", "0", "--dry-run")
    assert plan.exit_code == 0
    assert "would be removed" in plan.stdout

    listing = run(
        runner, cli_env, "prune", "--before", "2030-01-01", "--keep-min", "0", "--dry-run"
    )
    assert "1 run(s) would be removed" in listing.stdout, "the dry run must not have consumed it"


def test_prune_deletes_with_yes(runner: CliRunner, cli_env: dict[str, str]) -> None:
    seed_old_run(runner, cli_env)
    result = run(runner, cli_env, "prune", "--before", "2030-01-01", "--keep-min", "0", "--yes")
    assert result.exit_code == 0
    assert "pruned 1 run(s)" in result.stdout

    after = run(runner, cli_env, "prune", "--before", "2030-01-01", "--keep-min", "0", "--dry-run")
    assert "nothing to prune" in after.stdout


def test_prune_without_a_cutoff_explains_itself(runner: CliRunner, cli_env: dict[str, str]) -> None:
    result = runner.invoke(cli_app, ["prune"], env=cli_env)
    assert result.exit_code != 0
    assert "TEST_HUB_RETENTION_DAYS" in everything_said(result)


def test_prune_reads_the_default_cutoff_from_the_setting(
    runner: CliRunner, cli_env: dict[str, str]
) -> None:
    seed_old_run(runner, cli_env)
    env = {**cli_env, "TEST_HUB_RETENTION_DAYS": "1"}
    result = run(runner, env, "prune", "--keep-min", "0", "--dry-run")
    assert result.exit_code == 0
    assert "1 run(s) would be removed" in result.stdout


def test_prune_refuses_to_prompt_when_there_is_no_terminal(
    runner: CliRunner, cli_env: dict[str, str]
) -> None:
    """A cron entry that forgot --yes must fail loudly, not block on a prompt
    nobody will ever answer."""
    seed_old_run(runner, cli_env)
    result = runner.invoke(
        cli_app, ["prune", "--before", "2030-01-01", "--keep-min", "0"], env=cli_env
    )
    assert result.exit_code != 0
    assert "--yes" in everything_said(result)

    still_there = run(
        runner, cli_env, "prune", "--before", "2030-01-01", "--keep-min", "0", "--dry-run"
    )
    assert "1 run(s) would be removed" in still_there.stdout


def test_prune_json_is_machine_readable(runner: CliRunner, cli_env: dict[str, str]) -> None:
    seed_old_run(runner, cli_env)
    result = run(
        runner, cli_env, "prune", "--before", "2030-01-01", "--keep-min", "0", "--yes", "--json"
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["runs"] == 1
    assert payload["dry_run"] is False
    assert payload["deleted"]["events"] > 0
    assert payload["by_project"] == {"p": 1}


def test_prune_rejects_two_ways_of_saying_the_same_cutoff(
    runner: CliRunner, cli_env: dict[str, str]
) -> None:
    result = runner.invoke(
        cli_app, ["prune", "--older-than", "30d", "--before", "2030-01-01"], env=cli_env
    )
    assert result.exit_code != 0
    assert "pass one" in everything_said(result)


def test_prune_rejects_an_unreadable_duration(runner: CliRunner, cli_env: dict[str, str]) -> None:
    result = runner.invoke(cli_app, ["prune", "--older-than", "3 months"], env=cli_env)
    assert result.exit_code != 0
    assert "90d" in everything_said(result)
