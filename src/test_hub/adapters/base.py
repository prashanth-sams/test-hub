"""The adapter protocol: how a report format becomes Test Hub events.

An adapter's whole job is to turn bytes into a stream of
:class:`~test_hub.domain.events.DomainEvent`. It does not touch the database, it
does not know about HTTP, and it does not decide what a run is called — that
comes in on the :class:`ParseContext`. Two consequences: an adapter is trivially
unit-testable against a fixture file, and the file-import path and the live
publishing path share every line of code after the adapter returns.

Writing one
-----------

Implement :class:`ReportAdapter` (a ``Protocol`` — no base class to inherit,
though :class:`BaseAdapter` is there if you want the helpers), then register it::

    [project.entry-points."test_hub.adapters"]
    my-format = "my_package.adapter:MyAdapter"

Test Hub's own two adapters are registered exactly this way. There is no
privileged built-in path — see ``docs/adapters.md`` for the full guide.

Detection
---------

:meth:`ReportAdapter.detect` returns a confidence in ``[0.0, 1.0]``:

======  ==========================================================
 0.0    definitely not mine
 0.3    the extension or content type fits, nothing else checked
 0.6    the content sniffs right (root element, required keys)
 0.9    the content is unmistakably mine (namespace, generator tag)
 1.0    the content declares itself as mine (``"kind": "test-hub-..."``)
======  ==========================================================

The registry picks the highest score above :data:`MIN_CONFIDENCE`; ties are
broken by ``priority`` and then by name, so the choice is deterministic. An
explicit ``--format`` skips detection entirely.

``detect`` must not consume the stream: it is handed a seekable stream and the
registry rewinds between adapters, but an adapter that reads gigabytes to decide
will make every import slow. Read a few kilobytes.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import IO, Any, Protocol, runtime_checkable

from test_hub.domain.events import DomainEvent
from test_hub.domain.models import (
    ParserWarning,
    RunContext,
    TestType,
    TriggerType,
    WarningSeverity,
)

__all__ = [
    "MIN_CONFIDENCE",
    "SNIFF_BYTES",
    "BaseAdapter",
    "ParseContext",
    "ReportAdapter",
    "WarningCollector",
]

#: A format must score at least this to be selected by auto-detection.
MIN_CONFIDENCE = 0.3

#: How much of the head of a file an adapter should look at in ``detect``.
SNIFF_BYTES = 8192


@dataclass(slots=True)
class ParseContext:
    """Everything an adapter needs that is not in the file.

    The metadata a user supplies on the command line or in the REST request —
    which project this is, which team owns it, what environment it ran in. An
    adapter merges what the file knows with what the context says; where they
    conflict, the context wins, because the human typing it has more information
    than the file does.
    """

    workspace_id: uuid.UUID
    project_key: str
    run_id: uuid.UUID = field(default_factory=uuid.uuid4)
    name: str | None = None
    team: str | None = None
    component: str | None = None
    test_type: TestType = TestType.OTHER
    trigger: TriggerType = TriggerType.UNKNOWN
    context: RunContext = field(default_factory=RunContext)
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    source: str = "urn:test-hub:import"
    filename: str | None = None
    #: Longest text kept per field before truncation; mirrors
    #: ``Settings.max_text_field_bytes`` so adapters never build a 40 MB string.
    max_text_bytes: int = 256 * 1024
    #: Collects everything the adapter could not represent exactly.
    warnings: WarningCollector = field(default_factory=lambda: WarningCollector())
    #: Per-run sequence counter. Private; use :meth:`next_sequence`.
    _sequence: int = field(default=0, repr=False)

    def next_sequence(self) -> int:
        """Hand out the next per-run sequence number.

        Adapters call this instead of counting themselves, so events emitted by
        nested generators still come out strictly increasing, and a sub-parser
        handed the same context cannot restart the numbering.
        """
        self._sequence += 1
        return self._sequence


class WarningCollector:
    """Accumulates :class:`ParserWarning` s during a parse.

    Bounded, because a pathological file should produce a bounded response: past
    the cap it counts what it drops and says so in a final warning.
    """

    MAX = 500

    def __init__(self) -> None:
        self._warnings: list[ParserWarning] = []
        self._dropped = 0

    def add(
        self,
        code: str,
        message: str,
        *,
        location: str | None = None,
        severity: WarningSeverity = WarningSeverity.WARNING,
        **detail: Any,
    ) -> None:
        if len(self._warnings) >= self.MAX:
            self._dropped += 1
            return
        self._warnings.append(
            ParserWarning(
                code=code,
                message=message[:512],
                severity=severity,
                location=location,
                detail=detail,
            )
        )

    def info(self, code: str, message: str, **kwargs: Any) -> None:
        self.add(code, message, severity=WarningSeverity.INFO, **kwargs)

    def to_list(self) -> list[ParserWarning]:
        out = list(self._warnings)
        if self._dropped:
            out.append(
                ParserWarning(
                    code="warnings_truncated",
                    message=f"{self._dropped} further warnings were not recorded.",
                    severity=WarningSeverity.INFO,
                )
            )
        return out

    def __len__(self) -> int:
        return len(self._warnings) + self._dropped

    def __bool__(self) -> bool:
        return bool(self._warnings) or bool(self._dropped)


@runtime_checkable
class ReportAdapter(Protocol):
    """What every report adapter implements.

    ``name`` must be unique across installed adapters and stable — it is stored
    on the run and shown to the user, and ``--format <name>`` selects it.
    """

    name: str
    """Stable identifier, e.g. ``junit-xml``."""

    def detect(self, stream: IO[bytes], filename: str | None, content_type: str | None) -> float:
        """Score how likely it is that this adapter can parse the input.

        Must leave the stream position where it found it or accept that the
        registry will rewind. Must not raise: return ``0.0`` instead.
        """
        ...

    def parse(self, stream: IO[bytes], context: ParseContext) -> Iterable[DomainEvent]:
        """Turn the input into domain events.

        May raise :class:`~test_hub.errors.AdapterError` for input it recognised
        but cannot parse. Anything it can parse but not represent exactly should
        be a warning on ``context.warnings``, not an exception.
        """
        ...


class BaseAdapter:
    """Optional convenience base with the boring parts already written.

    Provides the extension/content-type half of detection and a safe stream
    sniffer. Inheriting is not required — :class:`ReportAdapter` is structural.
    """

    name: str = "base"
    display_name: str = "Base"
    #: File extensions this adapter recognises, lowercase and dotted.
    extensions: tuple[str, ...] = ()
    #: MIME types this adapter recognises.
    content_types: tuple[str, ...] = ()
    #: Tie-break when two adapters score the same. Higher wins.
    priority: int = 0
    #: Shown by ``test-hub formats`` and in the unknown-format error.
    description: str = ""

    def sniff(self, stream: IO[bytes], size: int = SNIFF_BYTES) -> bytes:
        """Read the head of the stream and rewind."""
        try:
            position = stream.tell()
        except (OSError, AttributeError):  # pragma: no cover - non-seekable
            position = 0
        head = stream.read(size) or b""
        with contextlib.suppress(OSError, AttributeError):  # pragma: no cover - non-seekable
            stream.seek(position)
        return head

    def hint_score(self, filename: str | None, content_type: str | None) -> float:
        """The extension/content-type half of a detection score."""
        score = 0.0
        if filename:
            lowered = filename.lower()
            if any(lowered.endswith(extension) for extension in self.extensions):
                score = 0.3
        if content_type:
            base = content_type.split(";", 1)[0].strip().lower()
            if base in self.content_types:
                score = max(score, 0.3)
        return score

    def detect(self, stream: IO[bytes], filename: str | None, content_type: str | None) -> float:
        """Hint-only detection. Subclasses override to sniff the content."""
        return self.hint_score(filename, content_type)

    def parse(self, stream: IO[bytes], context: ParseContext) -> Iterator[DomainEvent]:
        raise NotImplementedError  # pragma: no cover - abstract

    def describe(self) -> dict[str, Any]:
        """The row this adapter contributes to ``test-hub formats``."""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "extensions": list(self.extensions),
            "content_types": list(self.content_types),
        }
