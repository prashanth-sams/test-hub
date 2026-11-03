"""The canonical model, the event contract, and the rules that define both.

Nothing in this package imports from anywhere else in ``test_hub`` except
:mod:`test_hub.version`. That is deliberate: it means an adapter, a publisher or
a test can depend on the contract without pulling in FastAPI, SQLAlchemy or a
database. If you find yourself wanting to import ``test_hub.storage`` here, the
logic belongs in :mod:`test_hub.service` instead.
"""

from __future__ import annotations

from test_hub.domain.events import DomainEvent, EventBatch, EventType, make_event
from test_hub.domain.identity import suite_id, test_identity, workspace_id
from test_hub.domain.models import (
    Artifact,
    ImportResult,
    OverviewSnapshot,
    ParserWarning,
    Project,
    RunCounts,
    RunDetail,
    RunSummary,
    TestAttempt,
    TestCase,
    TestDetail,
    TestRun,
    TestSuite,
    TestType,
    TriggerType,
    Workspace,
)
from test_hub.domain.status import RunStatus, TestStatus, normalize_status

__all__ = [
    "Artifact",
    "DomainEvent",
    "EventBatch",
    "EventType",
    "ImportResult",
    "OverviewSnapshot",
    "ParserWarning",
    "Project",
    "RunCounts",
    "RunDetail",
    "RunStatus",
    "RunSummary",
    "TestAttempt",
    "TestCase",
    "TestDetail",
    "TestRun",
    "TestStatus",
    "TestSuite",
    "TestType",
    "TriggerType",
    "Workspace",
    "make_event",
    "normalize_status",
    "suite_id",
    "test_identity",
    "workspace_id",
]
