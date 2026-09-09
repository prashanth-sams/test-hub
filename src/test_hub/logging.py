"""Logging, and the secret redaction that every log line and stored trace passes through.

Test output is one of the great accidental secret stores: a failing HTTP client
prints the ``Authorization`` header it sent, a failing database test prints the
connection URL with the password in it. Test Hub stores that text and shows it
in a browser, so it scrubs it on the way in — both in log records and in the
failure text an adapter hands to the ingest service.

The patterns are configurable (``TEST_HUB_REDACTION_PATTERNS``) because every
organisation has one more secret shape than the defaults know about. Each pattern
must have exactly one capturing group: the group is what gets replaced, so
``password=hunter2`` becomes ``password=***REDACTED***`` and stays readable.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable
from typing import Any

from test_hub.config import Settings, get_settings

__all__ = ["Redactor", "configure_logging", "get_logger", "get_redactor"]


class Redactor:
    """Applies the configured redaction patterns to arbitrary text.

    Compiled once and reused; a redactor is cheap to call and safe to share
    across threads.
    """

    def __init__(self, patterns: Iterable[str], placeholder: str = "***REDACTED***") -> None:
        self._patterns: list[re.Pattern[str]] = []
        self.placeholder = placeholder
        for pattern in patterns:
            try:
                compiled = re.compile(pattern)
            except re.error:
                continue  # a bad pattern must not take the process down
            if compiled.groups >= 1:
                self._patterns.append(compiled)

    def __bool__(self) -> bool:
        return bool(self._patterns)

    def redact(self, text: str | None) -> str | None:
        """Replace every captured secret in ``text``.

        >>> Redactor([r"(?i)\\bpassword=([^\\s]+)"]).redact("password=hunter2 ok")
        'password=***REDACTED*** ok'
        """
        if not text or not self._patterns:
            return text
        result = text
        for pattern in self._patterns:
            result = pattern.sub(self._replace, result)
        return result

    def _replace(self, match: re.Match[str]) -> str:
        """Rebuild the match with group 1 swapped for the placeholder."""
        whole = match.group(0)
        secret = match.group(1)
        if not secret:
            return whole
        # Replace only the captured span, keeping the surrounding context intact.
        start = match.start(1) - match.start(0)
        end = match.end(1) - match.start(0)
        return whole[:start] + self.placeholder + whole[end:]

    def redact_mapping(self, data: dict[str, Any]) -> dict[str, Any]:
        """Redact every string value in a (possibly nested) mapping."""
        out: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, str):
                out[key] = self.redact(value)
            elif isinstance(value, dict):
                out[key] = self.redact_mapping(value)
            elif isinstance(value, list):
                out[key] = [self.redact(v) if isinstance(v, str) else v for v in value]
            else:
                out[key] = value
        return out


_NULL_REDACTOR = Redactor([])
_redactor: Redactor | None = None


def get_redactor(settings: Settings | None = None) -> Redactor:
    """The process-wide redactor, built from settings on first use."""
    global _redactor
    if settings is not None:
        _redactor = (
            Redactor(settings.redaction_patterns, settings.redaction_placeholder)
            if settings.redaction_enabled
            else _NULL_REDACTOR
        )
        return _redactor
    if _redactor is None:
        _redactor = get_redactor(get_settings())
    return _redactor


class _RedactingFilter(logging.Filter):
    """Scrubs the rendered message of every record that passes through a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        redactor = get_redactor()
        if not redactor:
            return True
        if isinstance(record.msg, str):
            record.msg = redactor.redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = redactor.redact_mapping(record.args)
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redactor.redact(arg) if isinstance(arg, str) else arg for arg in record.args
                )
        return True


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, for shipping into a log system."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        return json.dumps(payload, default=str)


_TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"


def configure_logging(settings: Settings | None = None) -> None:
    """Install Test Hub's handler on the root logger. Idempotent."""
    settings = settings or get_settings()
    get_redactor(settings)

    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_RedactingFilter())
    if settings.log_format == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT, datefmt="%H:%M:%S"))

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_test_hub", False):
            root.removeHandler(existing)
    handler._test_hub = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.log_level.upper()))

    # Uvicorn's access log duplicates our own request logging and is noisy in a
    # terminal that is also showing test output; keep errors, drop the rest.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """A logger under the ``test_hub`` namespace."""
    return logging.getLogger(name if name.startswith("test_hub") else f"test_hub.{name}")
