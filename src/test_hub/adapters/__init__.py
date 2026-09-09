"""The adapter registry: how Test Hub finds out which parser can read a file.

An adapter is discovered, never hard-wired. Every adapter — including the two
Test Hub ships — is published through the ``test_hub.adapters`` entry-point
group, so a third-party package that adds TRX or Allure support is exactly as
privileged as ``junit-xml`` is. This module is the only place that knows how to
find them, score them and hand the winner to the import service.

Discovery
---------

``importlib.metadata.entry_points(group="test_hub.adapters")`` is the primary
path. It only works once the distribution has been installed, and Test Hub is
very often run straight from a checkout with ``PYTHONPATH=src`` before anyone
has typed ``pip install -e .`` — in that state the group is empty and a naive
registry would report "no adapters installed" for a perfectly good JUnit file.
So when the group yields nothing (or yields something that does not include a
built-in), the built-ins are imported by module path instead. Which path was
taken is logged at ``debug`` level, because "why did it not see my adapter"
is the question this module exists to answer.

A third-party entry point that raises on import is *skipped with a warning*.
One broken plugin must not stop the process from parsing a JUnit file, and it
must not be silent either.

Detection
---------

:meth:`AdapterRegistry.detect` asks every adapter to score the input, rewinding
the stream in between (an adapter is allowed to read the head of the stream and
sloppy about putting it back). The winner is the highest score at or above
:data:`~test_hub.adapters.base.MIN_CONFIDENCE`; ties break on ``priority`` and
then on ``name``, so two adapters that both say 0.9 always resolve the same way
on every machine and in every process.

When nothing scores high enough the resulting
:class:`~test_hub.errors.UnknownFormatError` carries the full installed-adapter
table — name, description, extensions. That error message *is* the
documentation for "what can I upload"; it is deliberately not a bare
"unsupported format".
"""

from __future__ import annotations

import contextlib
import importlib
import threading
from importlib.metadata import EntryPoint, entry_points
from typing import IO, Any

from test_hub.adapters.base import MIN_CONFIDENCE, ReportAdapter
from test_hub.errors import UnknownFormatError
from test_hub.logging import get_logger

__all__ = [
    "ADAPTER_ENTRY_POINT_GROUP",
    "BUILTIN_ADAPTERS",
    "AdapterRegistry",
    "get_registry",
    "reset_registry",
    "select_adapter",
]

logger = get_logger(__name__)

#: The entry-point group every adapter publishes itself in.
ADAPTER_ENTRY_POINT_GROUP = "test_hub.adapters"

#: ``name -> "module:attribute"`` for the adapters that live in this package.
#: Used only as the fallback when the entry-point group cannot see them, which
#: is the normal state of a source checkout.
BUILTIN_ADAPTERS: dict[str, str] = {
    "testhub-json": "test_hub.adapters.canonical_json:CanonicalJsonAdapter",
    "junit-xml": "test_hub.adapters.junit_xml:JUnitXmlAdapter",
}


def _instantiate(loaded: Any, label: str) -> ReportAdapter:
    """Turn whatever an entry point pointed at into an adapter instance.

    Entry points are allowed to name a class, a zero-argument factory or a
    ready-made singleton; all three appear in the wild and all three are cheap
    to support here rather than in every adapter package.
    """
    candidate = loaded() if isinstance(loaded, type) else loaded
    looks_like_adapter = callable(getattr(candidate, "parse", None)) and callable(
        getattr(candidate, "detect", None)
    )
    if not looks_like_adapter and callable(candidate):
        candidate = candidate()
    for attribute in ("name", "detect", "parse"):
        if not hasattr(candidate, attribute):
            raise TypeError(f"{label} is not a ReportAdapter: it has no {attribute!r}")
    return candidate  # type: ignore[no-any-return]


class AdapterRegistry:
    """The set of adapters this process can use.

    Not a global by construction: tests build their own, register a fake and
    throw it away. :func:`get_registry` is the process-wide one that production
    code uses.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, ReportAdapter] = {}

    # ------------------------------------------------------------ mutation

    def register(self, adapter: ReportAdapter) -> None:
        """Add an adapter, replacing any earlier one with the same name.

        Replacement is deliberate and logged: it is how an operator overrides a
        built-in with a patched fork without uninstalling anything.
        """
        name = getattr(adapter, "name", "")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"adapter {adapter!r} has no usable .name")
        name = name.strip()
        if name in self._adapters:
            logger.warning(
                "adapter %r replaces the previously registered %r",
                type(adapter).__name__,
                type(self._adapters[name]).__name__,
            )
        self._adapters[name] = adapter

    # ------------------------------------------------------------- lookup

    def get(self, name: str) -> ReportAdapter:
        """The adapter registered under ``name``.

        Raises :class:`~test_hub.errors.UnknownFormatError` — not ``KeyError`` —
        because this is what ``--format nonsense`` reaches, and the caller
        deserves the list of formats that do exist.
        """
        key = (name or "").strip()
        adapter = self._adapters.get(key)
        if adapter is None:
            raise UnknownFormatError(
                f"unknown format {name!r}.\n{self._format_table()}",
                adapters=self.describe(),
            )
        return adapter

    def names(self) -> list[str]:
        return sorted(self._adapters)

    def describe(self) -> list[dict[str, Any]]:
        """One row per installed adapter, for ``test-hub formats`` and errors."""
        rows: list[dict[str, Any]] = []
        for name in self.names():
            adapter = self._adapters[name]
            describe = getattr(adapter, "describe", None)
            row: dict[str, Any]
            if callable(describe):
                row = dict(describe())
            else:  # a Protocol-only adapter that never inherited BaseAdapter
                row = {
                    "name": name,
                    "display_name": getattr(adapter, "display_name", name),
                    "description": getattr(adapter, "description", ""),
                    "extensions": list(getattr(adapter, "extensions", ())),
                    "content_types": list(getattr(adapter, "content_types", ())),
                }
            row.setdefault("name", name)
            row["priority"] = int(getattr(adapter, "priority", 0))
            rows.append(row)
        return rows

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._adapters

    def __len__(self) -> int:
        return len(self._adapters)

    # ---------------------------------------------------------- detection

    def detect(
        self, stream: IO[bytes], filename: str | None, content_type: str | None
    ) -> tuple[ReportAdapter, float]:
        """Score every adapter and return the best one above the threshold."""
        try:
            origin = stream.tell()
        except (OSError, AttributeError):  # pragma: no cover - non-seekable input
            origin = 0

        def rewind() -> None:
            with contextlib.suppress(OSError, AttributeError):  # non-seekable input
                stream.seek(origin)

        scored: list[tuple[float, int, str, ReportAdapter]] = []
        for name in self.names():
            adapter = self._adapters[name]
            rewind()
            try:
                score = float(adapter.detect(stream, filename, content_type))
            except Exception:  # a broken detector must not fail the whole import
                logger.warning("adapter %r raised during detection; scoring it 0.0", name)
                score = 0.0
            if score != score:  # NaN
                score = 0.0
            score = max(0.0, min(1.0, score))
            logger.debug("adapter %s scored %.2f for %s", name, score, filename or "<stream>")
            if score >= MIN_CONFIDENCE:
                scored.append((score, int(getattr(adapter, "priority", 0)), name, adapter))
        rewind()

        if not scored:
            raise UnknownFormatError(
                "no installed adapter recognised this input"
                f" ({_describe_input(filename, content_type)}).\n{self._format_table()}",
                adapters=self.describe(),
            )
        # Highest score, then highest priority, then lowest name: total and stable.
        score, _priority, name, adapter = min(scored, key=lambda row: (-row[0], -row[1], row[2]))
        logger.debug("selected adapter %s with confidence %.2f", name, score)
        return adapter, score

    # ------------------------------------------------------------ helpers

    def _format_table(self) -> str:
        """The human half of every "I don't know this file" message."""
        if not self._adapters:
            return (
                "No adapters are installed at all, which means the Test Hub package is "
                "broken or was imported from a partial checkout."
            )
        lines = ["Installed adapters:"]
        for row in self.describe():
            extensions = ", ".join(row.get("extensions") or ()) or "no extension hint"
            description = row.get("description") or row.get("display_name") or ""
            lines.append(f"  {row['name']:<16} {extensions:<22} {description}")
        lines.append("Pass --format <name> to skip detection and force one of these.")
        return "\n".join(lines)


def _describe_input(filename: str | None, content_type: str | None) -> str:
    parts = []
    if filename:
        parts.append(f"filename={filename!r}")
    if content_type:
        parts.append(f"content_type={content_type!r}")
    return ", ".join(parts) or "no filename or content type given"


def _load_entry_points(registry: AdapterRegistry) -> list[str]:
    """Register everything the ``test_hub.adapters`` group advertises."""
    try:
        points: list[EntryPoint] = list(entry_points(group=ADAPTER_ENTRY_POINT_GROUP))
    except Exception:  # pragma: no cover - a corrupt metadata dir must not be fatal
        logger.warning("could not read the %r entry-point group", ADAPTER_ENTRY_POINT_GROUP)
        return []

    loaded: list[str] = []
    for point in points:
        try:
            adapter = _instantiate(point.load(), f"entry point {point.name!r} ({point.value})")
            registry.register(adapter)
        except Exception as exc:
            # A third-party plugin with a bad import must cost that plugin, not
            # the process. The name and the reason are both in the log.
            logger.warning(
                "skipping adapter entry point %r (%s): %s: %s",
                point.name,
                point.value,
                type(exc).__name__,
                exc,
            )
            continue
        loaded.append(point.name)
    return loaded


def _load_builtin(registry: AdapterRegistry, spec: str) -> bool:
    module_name, _, attribute = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
        registry.register(_instantiate(getattr(module, attribute), spec))
    except Exception:
        # Losing a built-in is a packaging bug, not a user error; log it loudly
        # and let the unknown-format message show which adapters survived.
        logger.exception("built-in adapter %s failed to load", spec)
        return False
    return True


def _build_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    discovered = _load_entry_points(registry)
    if discovered:
        logger.debug(
            "loaded %d adapter(s) from the %r entry-point group: %s",
            len(discovered),
            ADAPTER_ENTRY_POINT_GROUP,
            ", ".join(sorted(discovered)),
        )
    else:
        logger.debug(
            "the %r entry-point group is empty (the package is probably running from "
            "source without `pip install -e .`); importing the built-in adapters directly",
            ADAPTER_ENTRY_POINT_GROUP,
        )
    missing = [name for name in BUILTIN_ADAPTERS if name not in registry]
    if missing and discovered:
        logger.debug("entry points did not provide built-in adapter(s): %s", ", ".join(missing))
    for name in missing:
        _load_builtin(registry, BUILTIN_ADAPTERS[name])
    return registry


_registry: AdapterRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> AdapterRegistry:
    """The process-wide registry, built once on first use."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = _build_registry()
    return _registry


def reset_registry() -> None:
    """Forget the process-wide registry so the next call rebuilds it.

    Exists for tests that install a fake adapter or manipulate the entry-point
    environment; nothing in production should need it.
    """
    global _registry
    with _registry_lock:
        _registry = None


def select_adapter(
    stream: IO[bytes],
    *,
    filename: str | None = None,
    content_type: str | None = None,
    explicit_format: str | None = None,
) -> tuple[ReportAdapter, float]:
    """Pick the adapter for one input, honouring ``--format`` above all else.

    An explicit format always wins, even when the content plainly looks like
    something else: the user asked for a specific parser and is entitled to the
    specific parser's error message rather than a silent substitution. It is
    reported with confidence ``1.0`` because there was nothing to guess.
    """
    registry = get_registry()
    if explicit_format and explicit_format.strip():
        return registry.get(explicit_format), 1.0
    return registry.detect(stream, filename, content_type)
