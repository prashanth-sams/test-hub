"""Retention: what the prune deletes, what it protects, and what it must not touch.

The first two tests here fail against the schema as shipped, which is the point
of writing them down. ``events``, ``artifacts`` and ``idempotency_keys`` carry a
``run_id`` with no foreign key, so nothing cascades into them and a run deleted
by hand leaves its whole event log behind — orphaned, unreachable, and invisible
to every query the product makes. The rest of this file is the surrounding
policy: a floor so a quiet project does not vanish, a live run left alone, and a
content-addressed blob that must survive exactly as long as somebody still points
at it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from test_hub.domain.models import RunContext, utcnow
from test_hub.domain.status import RunStatus
from test_hub.errors import ConfigurationError
from test_hub.service.imports import ImportRequest, ImportService
from test_hub.service.retention import (
    UNIT_DAYS,
    UNLINKED_TABLES,
    RetentionPolicy,
    RetentionService,
    parse_duration,
    resolve_cutoff,
)
from test_hub.storage.models import (
    ArtifactRow,
    AttemptRow,
    Base,
    EventRow,
    IdempotencyKeyRow,
    RunRow,
    SuiteRow,
    TestCaseRow,
)
from tests.paths import junit

# --------------------------------------------------------------------- helpers


def import_run(
    context: Any,
    *,
    project: str = "payments-api",
    fixture: str = "simple.xml",
    idempotency_key: str | None = None,
) -> uuid.UUID:
    """Import a report and return its run id."""
    with junit(fixture).open("rb") as handle:
        result = ImportService(context).import_stream(
            handle,
            ImportRequest(
                project_key=project,
                filename=fixture,
                context=RunContext(),
                idempotency_key=idempotency_key,
            ),
        )
    return result.run_id


def backdate(context: Any, run_id: uuid.UUID, *, days: int) -> None:
    """Move a run into the past, both timestamps, as if it had run then."""
    moment = utcnow() - timedelta(days=days)
    with context.uow_factory() as uow:
        row = uow.session.get(RunRow, run_id)
        row.started_at = moment
        row.finished_at = moment
        row.created_at = moment
        uow.commit()


def freshen(context: Any, run_id: uuid.UUID) -> None:
    """Move a run to now.

    The JUnit fixtures carry their own ``timestamp``, which the adapter honours
    — so a run "just imported" is dated whenever the fixture says, not today.
    That is D2 behaving correctly, and it makes an explicit reset the only
    honest way to say "this run is recent".
    """
    with context.uow_factory() as uow:
        row = uow.session.get(RunRow, run_id)
        row.started_at = utcnow()
        row.created_at = utcnow()
        uow.commit()


def count(context: Any, model: Any, run_id: uuid.UUID) -> int:
    with context.uow_factory(read_only=True) as uow:
        return int(
            uow.session.execute(
                sa.select(sa.func.count()).select_from(model).where(model.run_id == run_id)
            ).scalar_one()
        )


def policy(**overrides: Any) -> RetentionPolicy:
    base: dict[str, Any] = {
        "cutoff": utcnow() - timedelta(days=30),
        "keep_min": 0,
        "grace_seconds": 0.0,
    }
    base.update(overrides)
    return RetentionPolicy(**base)


def blobs(context: Any) -> list[Path]:
    root = context.settings.resolved_artifacts_dir
    return sorted(p for p in root.rglob("*") if p.is_file() and ".incoming" not in p.parts)


# ------------------------------------------------------- the cascade gap (D3)


def test_prune_removes_the_rows_no_foreign_key_cascades_into(context: Any) -> None:
    """The regression this whole module exists for.

    `suites`, `test_cases` and `attempts` go by ON DELETE CASCADE. `events`,
    `artifacts` and `idempotency_keys` do not, and a prune that only deleted the
    run would leave the event log behind with nothing pointing at it.
    """
    context.settings.store_raw_reports = True
    run_id = import_run(context, idempotency_key="batch-1")
    backdate(context, run_id, days=90)

    assert count(context, EventRow, run_id) > 0
    assert count(context, ArtifactRow, run_id) > 0
    assert count(context, IdempotencyKeyRow, run_id) == 1
    assert count(context, AttemptRow, run_id) > 0

    result = RetentionService(context).prune(policy())
    assert result.runs == 1

    for model in (
        RunRow,
        SuiteRow,
        TestCaseRow,
        AttemptRow,
        EventRow,
        ArtifactRow,
        IdempotencyKeyRow,
    ):
        column = RunRow.id if model is RunRow else model.run_id
        with context.uow_factory(read_only=True) as uow:
            remaining = uow.session.execute(
                sa.select(sa.func.count()).select_from(model).where(column == run_id)
            ).scalar_one()
        assert remaining == 0, f"{model.__tablename__} still has rows for the pruned run"


def test_every_table_with_a_run_id_either_cascades_or_is_pruned_by_hand() -> None:
    """The guard that keeps :data:`UNLINKED_TABLES` honest.

    A new table with a ``run_id`` is either given the foreign key or added to the
    prune's delete list. Forgetting both is the bug this file opened with, and
    it is silent — the rows simply stop being reachable.
    """
    orphans: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name == "runs" or "run_id" not in table.columns:
            continue
        cascades = any(
            fk.column.table.name == "runs" and fk.ondelete == "CASCADE"
            for fk in table.columns["run_id"].foreign_keys
        )
        if not cascades and table.name not in UNLINKED_TABLES:
            orphans.append(table.name)
    assert not orphans, (
        f"{orphans} carry a run_id that nothing cascades into and that the prune does not "
        f"delete. Add the foreign key, or add the table to UNLINKED_TABLES."
    )


# ------------------------------------------------------ content addressing (D4)


def test_a_blob_shared_with_another_run_survives(context: Any) -> None:
    """Deduplication means one file backs every row that uploaded those bytes."""
    context.settings.store_raw_reports = True
    doomed = import_run(context, project="alpha")
    kept = import_run(context, project="beta")  # same fixture, same digest
    backdate(context, doomed, days=90)

    assert len(blobs(context)) == 1, "the fixture should have been stored once, not twice"

    result = RetentionService(context).prune(policy(project_keys=("alpha",)))

    assert result.runs == 1
    assert result.artifacts == 1, "the doomed run's artifact row goes"
    assert result.blobs_unlinked == 0, "but the bytes are still beta's bytes"
    assert result.blobs_retained == 1
    assert len(blobs(context)) == 1
    with context.uow_factory(read_only=True) as uow:
        assert uow.artifacts.list_for_run(kept)[0].sha256


def test_the_last_reference_frees_the_blob(context: Any) -> None:
    context.settings.store_raw_reports = True
    first = import_run(context, project="alpha")
    import_run(context, project="beta")
    backdate(context, first, days=90)
    for row in _all_run_ids(context):
        backdate(context, row, days=90)

    stored = blobs(context)
    assert len(stored) == 1
    size = stored[0].stat().st_size

    result = RetentionService(context).prune(policy())

    assert result.blobs_unlinked == 1
    assert result.bytes_freed == size
    assert blobs(context) == []
    # The two shard directories go with it.
    assert not (context.settings.resolved_artifacts_dir / stored[0].parent.name).exists()


def test_the_grace_window_protects_a_freshly_uploaded_blob(context: Any) -> None:
    """The race `put()`'s utime exists to close.

    An importer that uploads content already on disk takes no write lock to do
    it and records its row afterwards, so a sweep in between would see no
    references and unlink bytes that are about to be claimed.
    """
    context.settings.store_raw_reports = True
    run_id = import_run(context)
    backdate(context, run_id, days=90)

    result = RetentionService(context).prune(policy(grace_seconds=3600.0))

    assert result.runs == 1
    assert result.blobs_unlinked == 0
    assert result.blobs_retained == 1
    assert len(blobs(context)) == 1


def test_put_touches_an_existing_blob_so_the_grace_window_can_see_it(context: Any) -> None:
    """Without the utime, mtime would mean *first* upload and the guard would lapse."""
    import io

    with context.uow_factory() as uow:
        first = uow.artifacts.put(io.BytesIO(b"shared bytes"), max_bytes=1024)
        uow.commit()
    path = Path(first.path)
    old = datetime(2020, 1, 1, tzinfo=UTC).timestamp()
    import os

    os.utime(path, (old, old))

    with context.uow_factory() as uow:
        again = uow.artifacts.put(io.BytesIO(b"shared bytes"), max_bytes=1024)
        uow.commit()

    assert again.created is False, "the same bytes must not be written twice"
    assert path.stat().st_mtime > old, "the dedup path must bump mtime"


# -------------------------------------------------------------- the floor (D6)


def test_the_floor_keeps_the_newest_runs_of_a_dormant_project(context: Any) -> None:
    """A project that went quiet must not be deleted out of the dashboard."""
    for index in range(8):
        run_id = import_run(context, project="dormant")
        backdate(context, run_id, days=90 + index)

    result = RetentionService(context).prune(policy(keep_min=5))

    assert result.runs == 3
    assert result.protected == 5
    with context.uow_factory(read_only=True) as uow:
        remaining = (
            uow.session.execute(sa.select(RunRow.started_at).order_by(RunRow.started_at.desc()))
            .scalars()
            .all()
        )
    assert len(remaining) == 5, "the floor keeps five, and they are the five newest"


def test_the_floor_is_per_project_not_per_workspace(context: Any) -> None:
    for project in ("alpha", "beta"):
        for index in range(4):
            backdate(context, import_run(context, project=project), days=90 + index)

    result = RetentionService(context).prune(policy(keep_min=3))

    assert result.protected == 6
    assert result.runs == 2
    assert result.by_project == {"alpha": 1, "beta": 1}


# --------------------------------------------------------- live runs (D7)


def test_a_live_run_past_the_cutoff_is_reported_not_deleted(context: Any) -> None:
    run_id = import_run(context)
    backdate(context, run_id, days=90)
    with context.uow_factory() as uow:
        uow.session.get(RunRow, run_id).status = RunStatus.RUNNING.value
        uow.commit()

    result = RetentionService(context).prune(policy())

    assert result.runs == 0
    assert result.skipped_live == 1
    assert count(context, AttemptRow, run_id) > 0


def test_include_unfinished_takes_it(context: Any) -> None:
    run_id = import_run(context)
    backdate(context, run_id, days=90)
    with context.uow_factory() as uow:
        uow.session.get(RunRow, run_id).status = RunStatus.RUNNING.value
        uow.commit()

    result = RetentionService(context).prune(policy(include_unfinished=True))

    assert result.runs == 1
    assert result.skipped_live == 0


# ------------------------------------------------------------ the cutoff (D2)


def test_the_cutoff_falls_back_to_created_at_when_a_run_never_started(context: Any) -> None:
    """`started_at` is nullable; `created_at` is not. The dashboard's filter
    makes the same substitution, so both must select the same runs."""
    old = import_run(context)
    recent = import_run(context, project="other")
    with context.uow_factory() as uow:
        for run_id, days in ((old, 90), (recent, 1)):
            row = uow.session.get(RunRow, run_id)
            row.started_at = None
            row.finished_at = None
            row.created_at = utcnow() - timedelta(days=days)
        uow.commit()

    result = RetentionService(context).prune(policy())

    assert result.runs == 1
    with context.uow_factory(read_only=True) as uow:
        assert uow.session.get(RunRow, recent) is not None
        assert uow.session.get(RunRow, old) is None


def test_the_boundary_is_inclusive_on_both_sides(context: Any) -> None:
    cutoff = utcnow() - timedelta(days=30)
    just_older = import_run(context, project="older")
    just_newer = import_run(context, project="newer")
    with context.uow_factory() as uow:
        uow.session.get(RunRow, just_older).started_at = cutoff - timedelta(seconds=1)
        uow.session.get(RunRow, just_newer).started_at = cutoff + timedelta(seconds=1)
        uow.commit()

    result = RetentionService(context).prune(policy(cutoff=cutoff))

    assert result.by_project == {"older": 1}


def test_by_import_date_cuts_on_created_at_instead(context: Any) -> None:
    """A back-dated import — last quarter's archive, uploaded today — is eligible
    under the default and protected under `--by-import-date`."""
    run_id = import_run(context)
    with context.uow_factory() as uow:
        row = uow.session.get(RunRow, run_id)
        row.started_at = utcnow() - timedelta(days=200)
        row.created_at = utcnow()
        uow.commit()

    assert RetentionService(context).plan(policy()).runs == 1
    assert RetentionService(context).plan(policy(by_import_date=True)).runs == 0


# ----------------------------------------------------------- dry run and batching


def test_a_dry_run_changes_nothing(context: Any) -> None:
    context.settings.store_raw_reports = True
    for index in range(4):
        backdate(context, import_run(context), days=90 + index)
    before = _snapshot(context)

    plan = RetentionService(context).plan(policy())

    assert plan.dry_run is True
    assert plan.runs == 4
    assert _snapshot(context) == before


def test_a_dry_run_predicts_what_the_prune_then_does(context: Any) -> None:
    """The plan an operator approves has to be the prune they get."""
    context.settings.store_raw_reports = True
    for index, project in enumerate(("alpha", "beta", "alpha")):
        backdate(context, import_run(context, project=project), days=90 + index)

    service = RetentionService(context)
    plan = service.plan(policy())
    result = service.prune(policy())

    assert plan.runs == result.runs
    assert plan.events == result.events
    assert plan.attempts == result.attempts
    assert plan.artifacts == result.artifacts
    assert plan.blobs_unlinked == result.blobs_unlinked
    assert plan.bytes_freed == result.bytes_freed
    assert plan.by_project == result.by_project


def test_batching_commits_as_it_goes_and_resumes_after_an_interruption(context: Any) -> None:
    """Not atomic, and deliberately so: an interrupted prune must not have to
    start over, because the reason it was interrupted may recur."""
    for index in range(7):
        backdate(context, import_run(context), days=90 + index)

    service = RetentionService(context)
    original = service._delete
    calls = {"n": 0}

    def fail_on_the_third_batch(session: Any, run_ids: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("interrupted")
        original(session, run_ids)

    service._delete = fail_on_the_third_batch  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        service.prune(policy(batch_size=2))

    assert len(_all_run_ids(context)) == 3, "two batches committed, the third rolled back"

    service._delete = original  # type: ignore[method-assign]
    assert RetentionService(context).prune(policy(batch_size=2)).runs == 3
    assert _all_run_ids(context) == []


def test_the_reported_counts_are_what_committed(context: Any) -> None:
    for index in range(5):
        backdate(context, import_run(context), days=90 + index)

    result = RetentionService(context).prune(policy(batch_size=2))

    assert result.runs == 5
    assert result.batches == 3
    assert result.rows == result.runs + result.suites + result.test_cases + result.attempts + (
        result.events + result.artifacts + result.idempotency_keys
    )
    assert _all_run_ids(context) == []


def test_nothing_eligible_is_not_an_error(context: Any) -> None:
    freshen(context, import_run(context))
    result = RetentionService(context).prune(policy())
    assert result.runs == 0
    assert result.batches == 0
    assert len(_all_run_ids(context)) == 1


# ----------------------------------------------------- the single writer (D5)


@pytest.mark.slow
def test_a_prune_in_batches_does_not_lock_a_publisher_out(context: Any) -> None:
    """The reason this is batched rather than one transaction.

    `BEGIN IMMEDIATE` means every transaction holds the write lock for its whole
    life, so a prune of a long window in one transaction blocks every publisher
    in CI for the duration. In batches the writer queues behind one batch, not
    behind the whole job.
    """
    import threading
    import time

    for index in range(40):
        backdate(context, import_run(context), days=90 + index)
    context.settings.sqlite_busy_timeout_ms = 5000

    elapsed: list[float | None] = []
    started = threading.Event()

    def publish() -> None:
        started.wait(timeout=5)
        begin = time.perf_counter()
        try:
            with context.uow_factory() as uow:
                uow.runs.ensure_project(context.workspace_id, key="probe", name="probe")
                uow.commit()
            elapsed.append(time.perf_counter() - begin)
        except Exception:
            elapsed.append(None)

    writer = threading.Thread(target=publish)
    writer.start()
    started.set()
    result = RetentionService(context).prune(policy(batch_size=2))
    writer.join(timeout=10)

    assert result.runs == 40
    assert result.batches == 20
    assert elapsed and elapsed[0] is not None, "the publisher was locked out entirely"
    assert elapsed[0] < 5.0, "the publisher waited for the whole prune, not for one batch"


# ------------------------------------------------------------ the grammar


@pytest.mark.parametrize(
    ("text", "days"),
    [
        ("90", 90),
        ("90d", 90),
        ("2w", 14),
        ("6m", 180),
        ("1y", 365),
        ("  30D  ", 30),
        ("1Y", 365),
    ],
)
def test_duration_grammar(text: str, days: int) -> None:
    assert parse_duration(text) == timedelta(days=days)


def test_a_month_is_thirty_days_and_says_so() -> None:
    """Fixed-length units, not calendar arithmetic — see UNIT_DAYS."""
    assert UNIT_DAYS["m"] == 30
    assert UNIT_DAYS["y"] == 365
    assert parse_duration("6m") == timedelta(days=180)


@pytest.mark.parametrize("text", ["", "soon", "-5d", "3 months", "90dd", "1.5d", "d", "9x"])
def test_a_duration_that_cannot_be_read_explains_the_grammar(text: str) -> None:
    """This is read off a cron entry nobody is watching, so the message has to
    carry the whole grammar rather than just refusing."""
    with pytest.raises(ConfigurationError) as excinfo:
        parse_duration(text)
    message = str(excinfo.value)
    assert "90d" in message
    assert all(unit in message for unit in ("d (day)", "w (7 days)", "m (30 days)"))


@pytest.mark.parametrize("text", ["0", "0d", "0m"])
def test_a_zero_cutoff_is_refused_as_its_own_mistake(text: str) -> None:
    """Readable, but it would mean "delete everything" — a different error from
    a typo, and it deserves a message that says which."""
    with pytest.raises(ConfigurationError, match="positive duration"):
        parse_duration(text)


def test_resolve_cutoff_prefers_an_explicit_date() -> None:
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    assert resolve_cutoff(older_than=None, before=moment, default_days=30) == moment


def test_resolve_cutoff_assumes_utc_for_a_naive_date() -> None:
    resolved = resolve_cutoff(older_than=None, before=datetime(2026, 1, 1), default_days=None)
    assert resolved == datetime(2026, 1, 1, tzinfo=UTC)


def test_resolve_cutoff_falls_back_to_the_setting() -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    resolved = resolve_cutoff(older_than=None, before=None, default_days=30, now=now)
    assert resolved == now - timedelta(days=30)


def test_resolve_cutoff_refuses_two_ways_of_saying_the_same_thing() -> None:
    with pytest.raises(ConfigurationError, match="pass one"):
        resolve_cutoff(older_than="30d", before=datetime(2026, 1, 1), default_days=None)


def test_resolve_cutoff_refuses_to_guess() -> None:
    with pytest.raises(ConfigurationError, match="TEST_HUB_RETENTION_DAYS"):
        resolve_cutoff(older_than=None, before=None, default_days=None)


# ------------------------------------------------------------------ internals


def _all_run_ids(context: Any) -> list[uuid.UUID]:
    with context.uow_factory(read_only=True) as uow:
        return list(uow.session.execute(sa.select(RunRow.id)).scalars().all())


def _snapshot(context: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    with context.uow_factory(read_only=True) as uow:
        for table in Base.metadata.sorted_tables:
            counts[table.name] = int(
                uow.session.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
            )
    counts["__blobs__"] = len(blobs(context))
    return counts
