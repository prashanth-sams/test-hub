"""Stable identity for tests, suites and runs.

A test's identity has to survive being re-run, re-ordered, re-parameterized in a
different order, moved to a different line, and executed on a different machine
by a different framework version. If it does not, history is worthless: every
run invents a new set of tests and nothing can be compared or called flaky.

The identity is therefore derived from *what the test is*, never from *when or
where it ran*:

===================  =====================================================
Part of the identity  Deliberately excluded
===================  =====================================================
project key           timestamps
suite path            line numbers, file offsets
class name            durations
test name             worker / shard id, hostname
normalized params     run id, build number, branch
===================  =====================================================

The result is a UUIDv5 in a fixed namespace, so it is stable across processes,
machines and releases of Test Hub, and can be computed independently by a
publisher that has never talked to the server.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "TEST_HUB_NAMESPACE",
    "identity_source",
    "normalize_params",
    "suite_id",
    "test_identity",
    "workspace_id",
]

#: Fixed UUIDv5 namespace for everything Test Hub identifies. Generated once from
#: ``uuid.uuid5(uuid.NAMESPACE_URL, "https://test-hub.dev/ns/v1")`` and then
#: hard-coded: it must never move, or every historical identity changes.
TEST_HUB_NAMESPACE = uuid.UUID("4c8f4c4d-3ecb-5f78-a1e4-c1a24e07b1e6")

_WS_RE = re.compile(r"\s+")
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]{4,}\b")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_MEMADDR_RE = re.compile(r"\bobject at 0x[0-9a-fA-F]+\b")


def _clean(value: str) -> str:
    """Normalize a free-text identity component.

    Unicode NFC so that a name typed with a combining accent matches the same
    name typed with a precomposed one, and whitespace collapsed so that a
    reformatted parameter list is still the same test.
    """
    text = unicodedata.normalize("NFC", value).strip()
    return _WS_RE.sub(" ", text)


def _scrub_volatile(value: str) -> str:
    """Remove the parts of a rendered value that change every run.

    ``<Foo object at 0x7f3c...>`` and bare UUIDs appear inside parametrized ids
    produced by frameworks that render objects into the test name. Left alone,
    every run would invent a new test. They are replaced with a stable token
    rather than dropped, so two genuinely different parameters do not collide.
    """
    value = _MEMADDR_RE.sub("object at 0x?", value)
    value = _UUID_RE.sub("<uuid>", value)
    return _HEX_RE.sub("0x?", value)


def _render(value: Any) -> str:
    """Render a parameter value into a comparable string."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _scrub_volatile(_clean(value))
    if isinstance(value, Mapping):
        inner = ",".join(f"{_render(k)}={_render(v)}" for k, v in sorted(value.items(), key=str))
        return "{" + inner + "}"
    if isinstance(value, Sequence):
        return "[" + ",".join(_render(v) for v in value) + "]"
    return _scrub_volatile(_clean(str(value)))


def normalize_params(params: Mapping[str, Any] | Sequence[Any] | str | None) -> str:
    """Render test parameters into a canonical, order-independent string.

    Named parameters are sorted by name, so ``(b=2, a=1)`` and ``(a=1, b=2)`` are
    one test. Positional parameters keep their order, because position *is* the
    name. A pre-rendered id string (``"[chrome-1024x768]"``) is accepted as-is
    after cleaning, for adapters that only ever see the rendered form.

    >>> normalize_params({"b": 2, "a": 1})
    'a=1|b=2'
    >>> normalize_params(["chrome", 1024])
    '0=chrome|1=1024'
    """
    if params is None:
        return ""
    if isinstance(params, str):
        return _scrub_volatile(_clean(params)).strip("[]")
    if isinstance(params, Mapping):
        return "|".join(
            f"{_clean(str(key))}={_render(value)}" for key, value in sorted(params.items(), key=str)
        )
    return "|".join(f"{index}={_render(value)}" for index, value in enumerate(params))


def identity_source(
    *,
    project: str,
    suite_path: str,
    class_name: str | None,
    test_name: str,
    params: Mapping[str, Any] | Sequence[Any] | str | None = None,
) -> str:
    """Build the exact string that is hashed into a test identity.

    Exposed, documented and tested on purpose: a publisher in another language
    can reproduce it byte for byte and compute the same UUID.

    The form is five ``\\x1f``-separated fields (unit separator — a character
    that cannot appear in any of them)::

        project \\x1f suite_path \\x1f class_name \\x1f test_name \\x1f params
    """
    fields = (
        _clean(project),
        _clean(suite_path).replace("\\", "/").lstrip("./"),
        _clean(class_name or ""),
        _clean(test_name),
        normalize_params(params),
    )
    return "\x1f".join(fields)


def test_identity(
    *,
    project: str,
    suite_path: str,
    test_name: str,
    class_name: str | None = None,
    params: Mapping[str, Any] | Sequence[Any] | str | None = None,
) -> uuid.UUID:
    """The stable identity of a test, as a UUIDv5.

    >>> a = test_identity(project="p", suite_path="tests/t.py", test_name="x")
    >>> b = test_identity(project="p", suite_path="./tests/t.py", test_name=" x ")
    >>> a == b
    True
    """
    return uuid.uuid5(
        TEST_HUB_NAMESPACE,
        identity_source(
            project=project,
            suite_path=suite_path,
            class_name=class_name,
            test_name=test_name,
            params=params,
        ),
    )


def suite_id(*, project: str, suite_path: str) -> uuid.UUID:
    """The stable identity of a suite within a project."""
    source = "\x1f".join(
        ("suite", _clean(project), _clean(suite_path).replace("\\", "/").lstrip("./"))
    )
    return uuid.uuid5(TEST_HUB_NAMESPACE, source)


def workspace_id(slug: str) -> uuid.UUID:
    """The stable identity of a workspace, so ``default`` is the same id everywhere."""
    return uuid.uuid5(TEST_HUB_NAMESPACE, "\x1f".join(("workspace", _clean(slug).lower())))


def content_digest(data: bytes) -> str:
    """SHA-256 hex digest, used for content-addressed artifact storage."""
    return hashlib.sha256(data).hexdigest()
