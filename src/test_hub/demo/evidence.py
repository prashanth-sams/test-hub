"""The files a demo run leaves behind: screenshots, logs, coverage, steps.

The dashboard has three pages — Screenshots, API Logs, Test Coverage — whose
whole subject is what a run attached to itself. A demo store with no attachments
renders all three as "nothing was captured", which is a true sentence about the
sample data and a misleading one about the product: it looks like the feature is
missing rather than like the sample suite never took a screenshot.

So the seeder publishes evidence, and everything in this module *generates* it:

* the screenshots are drawn here, pixel by pixel, by the small PNG encoder
  below — they are stylised pictures of a browser, not a capture of any real
  page and not a copied image;
* the logs, coverage reports and traces are formatted here from the run's own
  names and numbers.

Nothing depends on Pillow or any other image library. A PNG is a zlib stream in
a four-chunk envelope, which is thirty lines, and a demo seeder is not worth a
dependency that has to be installed, pinned and audited.

Everything is derived from the test and project names rather than from a random
generator. Two reasons: the seeder's own generator drives the shape of the whole
demo dataset, and drawing from it here would shift every number in the sample
data; and content-addressed storage means the same picture published by five
runs costs one stored object, which only holds while the picture is stable.
"""

from __future__ import annotations

import io
import json
import struct
import uuid
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from test_hub.domain.events import DomainEvent, EventType
from test_hub.domain.models import ArtifactKind
from test_hub.domain.status import TestStatus
from test_hub.service.runs import ArtifactUpload, RunService

__all__ = ["attach_evidence", "steps_for"]

Colour = tuple[int, int, int]

#: The frameworks whose tests run in a browser, and therefore the only ones that
#: have anything to take a picture of.
BROWSER_FRAMEWORKS = frozenset({"Playwright"})
#: Framework -> the coverage report that toolchain actually writes.
COVERAGE_REPORTS = {"pytest": "coverage.xml", "JUnit 5": "jacoco.xml"}
#: Frameworks whose demo projects talk HTTP to a double, and so have a request
#: log worth publishing.
LOGGING_FRAMEWORKS = frozenset({"pytest"})
#: Screenshots published per run. A real UI suite captures one per failure and
#: the demo should look like one, but twenty-five runs of unbounded captures is
#: a data directory nobody asked for.
MAX_SCREENSHOTS = 3
#: The statuses a run takes a picture of.
_FAILED = frozenset({TestStatus.FAILED.value, TestStatus.ERROR.value})


# --------------------------------------------------------------------- PNG


class _Canvas:
    """A flat RGB canvas that can draw filled rectangles and encode itself.

    Deliberately the smallest thing that can produce a recognisable screenshot:
    rectangles are enough for a browser frame, a header, a sidebar and lines of
    text seen at gallery size.
    """

    def __init__(self, width: int, height: int, background: Colour) -> None:
        self.width = width
        self.height = height
        self._rows = [bytearray(bytes(background) * width) for _ in range(height)]

    def rect(self, x: int, y: int, width: int, height: int, colour: Colour) -> None:
        """Fill a rectangle, clipped to the canvas."""
        left = max(0, x)
        right = min(self.width, x + width)
        if right <= left:
            return
        run = bytes(colour) * (right - left)
        for row in range(max(0, y), min(self.height, y + height)):
            self._rows[row][left * 3 : right * 3] = run

    def to_png(self) -> bytes:
        """Encode as a PNG: signature, IHDR, one IDAT, IEND."""
        raw = bytearray()
        for row in self._rows:
            raw.append(0)  # filter type 0 — flat colour compresses fine without one
            raw.extend(row)

        def chunk(tag: bytes, payload: bytes) -> bytes:
            body = tag + payload
            return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

        header = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        return b"".join(
            (
                b"\x89PNG\r\n\x1a\n",
                chunk(b"IHDR", header),
                chunk(b"IDAT", zlib.compress(bytes(raw), 9)),
                chunk(b"IEND", b""),
            )
        )


# The palette is written here rather than read from the dashboard's CSS tokens:
# these are pixels baked into a file at seed time, and a screenshot taken last
# week does not restyle itself when the product's theme changes.
_PAGE = (247, 248, 251)
_CHROME = (228, 232, 240)
_DOT = (198, 205, 220)
_INK = (203, 209, 221)
_INK_STRONG = (166, 175, 192)
_SIDEBAR = (236, 239, 246)
_FAIL = (226, 106, 98)
_FAIL_SOFT = (250, 226, 224)
_ACCENTS: tuple[Colour, ...] = (
    (79, 108, 216),
    (35, 148, 122),
    (168, 92, 186),
    (198, 122, 46),
)


def _spread(label: str) -> int:
    """A stable number for a name — the same test always draws the same page."""
    return zlib.crc32(label.encode("utf-8"))


def screenshot_png(label: str, *, failed: bool) -> bytes:
    """A stylised browser window, 480 by 300, drawn from the test's name.

    A failure gets a red banner where the assertion would have been reported, so
    the gallery reads at a glance even before the captions do.
    """
    seed = _spread(label)
    canvas = _Canvas(480, 300, _PAGE)
    accent = _ACCENTS[seed % len(_ACCENTS)]

    # Browser chrome: a bar, three dots and an address field.
    canvas.rect(0, 0, 480, 26, _CHROME)
    for index in range(3):
        canvas.rect(12 + index * 14, 10, 8, 7, _DOT)
    canvas.rect(60, 8, 300, 11, _PAGE)

    # The application under test: a coloured header, then a sidebar and content.
    canvas.rect(0, 26, 480, 34, accent)
    canvas.rect(16, 38, 96, 10, _PAGE)
    canvas.rect(0, 60, 104, 240, _SIDEBAR)
    for index in range(6):
        canvas.rect(14, 78 + index * 26, 60 + (seed >> index) % 24, 8, _INK)

    top = 78
    if failed:
        canvas.rect(124, top, 336, 54, _FAIL_SOFT)
        canvas.rect(124, top, 4, 54, _FAIL)
        canvas.rect(140, top + 14, 190, 9, _FAIL)
        canvas.rect(140, top + 32, 260, 7, _INK_STRONG)
        top += 74

    # Lines of "text" — varied widths, or it reads as a barcode rather than a page.
    for index in range(6):
        width = 150 + (seed >> (index * 3)) % 300
        canvas.rect(124, top + index * 22, min(width, 336), 9, _INK if index else _INK_STRONG)

    return canvas.to_png()


# ------------------------------------------------------------------- text


def request_log(project_key: str, started: datetime, lines: int = 24) -> bytes:
    """An HTTP client log of the sort a test double writes beside a suite.

    This is what the API Logs page has to show, and it is honest about being a
    log file rather than a modelled request: Test Hub stores the bytes, and the
    page says so.
    """
    routes = (
        ("POST", "/v1/authorizations", 201, 84),
        ("GET", "/v1/authorizations/{id}", 200, 22),
        ("POST", "/v1/captures", 201, 96),
        ("GET", "/v1/settlements?page=2", 200, 41),
        ("POST", "/v1/refunds", 422, 18),
        ("POST", "/v1/tokens", 200, 33),
        ("GET", "/healthz", 200, 3),
    )
    out: list[str] = [f"# {project_key} — test double request log"]
    clock = started
    for index in range(lines):
        method, path, status, millis = routes[index % len(routes)]
        clock += timedelta(milliseconds=millis * 3 + index * 7)
        stamp = clock.strftime("%H:%M:%S.%f")[:-3]
        level = "WARN " if status >= 400 else "INFO "
        out.append(f"{stamp} {level} {method:5} {path:34} -> {status} in {millis}ms")
    out.append(f"{clock.strftime('%H:%M:%S.%f')[:-3]} INFO  connection pool drained")
    return ("\n".join(out) + "\n").encode("utf-8")


def coverage_report(project_key: str, filename: str) -> bytes:
    """A Cobertura or JaCoCo document, whichever that toolchain writes.

    The numbers inside are never read by Test Hub — it stores the report and
    hands it back, which is exactly what the coverage page says it does.
    """
    seed = _spread(project_key)
    covered = 2400 + seed % 900
    missed = 180 + seed % 260
    total = covered + missed
    rate = covered / total

    if filename.endswith("jacoco.xml"):
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<report name="{project_key}">\n'
            f'  <counter type="INSTRUCTION" missed="{missed * 4}" covered="{covered * 4}"/>\n'
            f'  <counter type="BRANCH" missed="{missed // 3}" covered="{covered // 3}"/>\n'
            f'  <counter type="LINE" missed="{missed}" covered="{covered}"/>\n'
            "</report>\n"
        ).encode()

    packages = "\n".join(
        f'    <package name="{project_key.replace("-", "_")}.module{index}" '
        f'line-rate="{rate - index / 100:.4f}" branch-rate="{rate - 0.06:.4f}"/>'
        for index in range(4)
    )
    return (
        '<?xml version="1.0" ?>\n'
        f'<coverage line-rate="{rate:.4f}" branch-rate="{rate - 0.05:.4f}" '
        f'lines-covered="{covered}" lines-valid="{total}" version="7.6.1">\n'
        "  <sources><source>.</source></sources>\n"
        f"  <packages>\n{packages}\n  </packages>\n"
        "</coverage>\n"
    ).encode()


def trace_json(test_name: str, suite_path: str) -> bytes:
    """A browser trace, in the shape a UI runner writes one."""
    steps = steps_for(suite_path, test_name)
    actions = [
        {
            "type": "action",
            "apiName": entry["name"].split(" ", 1)[0].lower(),
            "title": entry["name"],
            "startTime": round(120.0 + index * 310.5, 1),
            "duration": entry.get("duration_ms", 200),
            "status": entry.get("status", "passed"),
        }
        for index, entry in enumerate(steps)
    ]
    return json.dumps(
        {"version": 1, "title": f"{suite_path}::{test_name}", "actions": actions},
        indent=2,
    ).encode("utf-8")


# ------------------------------------------------------------------ steps

#: The step lists the browser suite publishes, per spec file.
#:
#: Test Hub has no step model — the finest grain it records is the attempt — so
#: these travel in `attempt.metadata` under "steps", which is the interim
#: convention the Test Steps page reads. Seeding them is what gives that page
#: something to render; when a real model arrives, this moves with it.
_STEPS: dict[str, tuple[str, ...]] = {
    "e2e/basket.spec.ts": (
        "Given a signed-in shopper",
        "When the catalogue page is open",
        "And an item is added to the basket",
        "Then the basket badge shows one item",
    ),
    "e2e/search.spec.ts": (
        "Given the storefront is loaded",
        "When a query is typed into the search box",
        "Then the results list is rendered",
        "And the query is echoed back safely",
    ),
    "e2e/checkout.spec.ts": (
        "Given a basket with one item",
        "When the delivery address is entered",
        "And a test card is submitted",
        "Then the order confirmation is shown",
    ),
}


def steps_for(suite_path: str, test_name: str) -> list[dict[str, Any]]:
    """The steps for one browser test, or an empty list for everything else.

    Only the browser suite publishes steps, which is the point: the Test Steps
    page has to be legible both when a run recorded them and when it did not.
    """
    template = _STEPS.get(suite_path)
    if not template:
        return []
    seed = _spread(test_name)
    return [
        {"name": name, "status": "passed", "duration_ms": 90 + (seed >> (index * 4)) % 1400}
        for index, name in enumerate(template)
    ]


# ------------------------------------------------------------- publishing


@dataclass(slots=True)
class _Result:
    """One finished test attempt, as read back out of the event stream."""

    test_id: uuid.UUID
    name: str
    suite_path: str
    failed: bool


def _results(events: Sequence[DomainEvent]) -> list[_Result]:
    """Fold the stream that was just applied into one entry per test.

    `DomainEvent.data` is a JSON mapping, not the typed payload it was built
    from — `make_event` dumps the model on the way in, because an event is a
    wire object first — so this reads keys.

    A retry emits two `test.finished` events and the LAST one wins here, which
    is the same rule the dashboard applies: a test that failed and then passed
    on retry is a passing test. Capturing the failed first attempt instead would
    put a screenshot with a red banner in it under a green "Passed" caption,
    because an artifact is attached to a test rather than to an attempt.
    """
    folded: dict[uuid.UUID, _Result] = {}
    for event in events:
        if event.type is not EventType.TEST_FINISHED:
            continue
        data = event.data
        raw_id = data.get("test_id")
        if not raw_id:
            continue
        test_id = uuid.UUID(str(raw_id))
        folded[test_id] = _Result(
            test_id=test_id,
            name=str(data.get("name") or "test"),
            suite_path=str(data.get("suite_path") or ""),
            failed=str(data.get("status")) in _FAILED,
        )
    return list(folded.values())


def _publish(
    service: RunService,
    run_id: uuid.UUID,
    *,
    name: str,
    payload: bytes,
    kind: ArtifactKind,
    content_type: str,
    test_id: uuid.UUID | None = None,
) -> None:
    service.add_artifact(
        run_id,
        ArtifactUpload(
            name=name,
            stream=io.BytesIO(payload),
            kind=kind,
            content_type=content_type,
            test_id=test_id,
            metadata={"source": "demo"},
        ),
    )


def attach_evidence(
    service: RunService,
    run_id: uuid.UUID,
    events: Sequence[DomainEvent],
    *,
    framework: str,
    project_key: str,
    started: datetime,
) -> int:
    """Publish the files this run would have produced. Returns how many.

    What a run attaches follows from what it *is*: a browser suite leaves
    screenshots and a trace, a Python suite leaves its HTTP log and a coverage
    report, and a Go unit suite leaves nothing at all — which is the honest case
    the evidence pages also have to render.
    """
    results = _results(events)
    failures: Iterable[_Result] = [result for result in results if result.failed]
    published = 0

    if framework in BROWSER_FRAMEWORKS:
        for result in list(failures)[:MAX_SCREENSHOTS]:
            _publish(
                service,
                run_id,
                name=f"{result.name.replace(' ', '-')[:48]}.png",
                payload=screenshot_png(f"{result.suite_path}::{result.name}", failed=True),
                kind=ArtifactKind.SCREENSHOT,
                content_type="image/png",
                test_id=result.test_id,
            )
            published += 1
        first_failure = next(iter(failures), None)
        if first_failure is not None:
            _publish(
                service,
                run_id,
                name="trace.json",
                payload=trace_json(first_failure.name, first_failure.suite_path),
                kind=ArtifactKind.TRACE,
                content_type="application/json",
                test_id=first_failure.test_id,
            )
            published += 1

    if framework in LOGGING_FRAMEWORKS:
        attached_to = next(iter(failures), None)
        _publish(
            service,
            run_id,
            name="requests.log",
            payload=request_log(project_key, started),
            kind=ArtifactKind.LOG,
            content_type="text/plain",
            test_id=attached_to.test_id if attached_to else None,
        )
        published += 1

    report = COVERAGE_REPORTS.get(framework)
    if report:
        _publish(
            service,
            run_id,
            name=report,
            payload=coverage_report(project_key, report),
            kind=ArtifactKind.COVERAGE,
            content_type="application/xml",
        )
        published += 1

    return published
