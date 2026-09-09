"""The CloudEvents envelope and the payload contract."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from test_hub.domain.events import (
    DomainEvent,
    EventBatch,
    EventType,
    RunStartedData,
    TestFinishedData,
    make_event,
)
from test_hub.domain.models import utcnow
from test_hub.domain.status import TestStatus
from test_hub.version import CLOUDEVENTS_SPEC_VERSION, SCHEMA_VERSION


def build(event_type: EventType = EventType.RUN_STARTED, **kwargs: object) -> DomainEvent:
    defaults: dict[str, object] = {
        "run_id": uuid.uuid4(),
        "workspace_id": uuid.uuid4(),
        "sequence": 1,
        "data": RunStartedData(started_at=utcnow()),
    }
    defaults.update(kwargs)
    return make_event(event_type, **defaults)  # type: ignore[arg-type]


def test_envelope_carries_every_required_attribute() -> None:
    event = build()
    assert event.specversion == CLOUDEVENTS_SPEC_VERSION
    assert event.schema_version == SCHEMA_VERSION
    assert isinstance(event.id, uuid.UUID)
    assert event.source
    assert event.type is EventType.RUN_STARTED
    assert event.time.tzinfo is not None
    assert event.sequence == 1


def test_cloudevents_extension_names_are_lowercase_on_the_wire() -> None:
    """CloudEvents requires extension attribute names to be lowercase alphanumerics."""
    payload = build().model_dump(mode="json", by_alias=True)
    assert "workspaceid" in payload
    assert "runid" in payload
    assert "schemaversion" in payload
    assert "workspace_id" not in payload


def test_python_side_aliases_are_readable() -> None:
    event = build()
    assert event.workspace_id == event.workspaceid
    assert event.run_id == event.runid


def test_payload_is_validated_against_its_event_type() -> None:
    with pytest.raises(ValidationError):
        DomainEvent(
            type=EventType.TEST_FINISHED,
            workspace_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            sequence=1,
            data={"not": "a test result"},
        )


def test_typed_payload_round_trips() -> None:
    event = build(
        EventType.TEST_FINISHED,
        data=TestFinishedData(
            suite_id=uuid.uuid4(),
            name="test_x",
            suite_path="tests/t.py",
            status=TestStatus.PASSED,
        ),
    )
    payload = event.payload()
    assert isinstance(payload, TestFinishedData)
    assert payload.status is TestStatus.PASSED


def test_a_future_schema_version_is_refused_not_guessed_at() -> None:
    with pytest.raises(ValidationError, match="unsupported schema_version"):
        DomainEvent(
            type=EventType.RUN_STARTED,
            schema_version=SCHEMA_VERSION + 1,
            workspace_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            sequence=1,
            data={"started_at": utcnow().isoformat()},
        )


def test_dedupe_key_is_source_plus_id() -> None:
    event = build(source="urn:a", event_id=uuid.UUID(int=7))
    assert event.dedupe_key() == f"urn:a\x1f{uuid.UUID(int=7)}"
    assert build(source="urn:b", event_id=uuid.UUID(int=7)).dedupe_key() != event.dedupe_key()


def test_batch_must_belong_to_one_run() -> None:
    first = build()
    second = build()
    with pytest.raises(ValidationError, match="same run"):
        EventBatch(events=[first, second])


def test_batch_accepts_events_from_one_run() -> None:
    run_id = uuid.uuid4()
    batch = EventBatch(events=[build(run_id=run_id), build(run_id=run_id, sequence=2)])
    assert len(batch.events) == 2


def test_naive_timestamps_are_rejected() -> None:
    """Every timestamp is UTC and aware. A naive one is a bug waiting to happen."""
    from datetime import datetime

    with pytest.raises(ValidationError):
        DomainEvent(
            type=EventType.RUN_STARTED,
            workspace_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            sequence=1,
            time=datetime(2026, 1, 1, 12, 0, 0),
            data={"started_at": utcnow().isoformat()},
        )


def test_unknown_envelope_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        DomainEvent(
            type=EventType.RUN_STARTED,
            workspace_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            sequence=1,
            data={"started_at": utcnow().isoformat()},
            surprise="value",
        )
