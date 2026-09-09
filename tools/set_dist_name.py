#!/usr/bin/env python3
"""Rewrite the distribution name in ``pyproject.toml``, and nothing else.

Why this exists
---------------

``test-hub`` is a good name and a risky one. PyPI rejects an upload whose name is
"too similar to an existing project" (PEP 541 confusability: it folds runs of
``-``, ``_`` and ``.`` together and lowercases, so ``test_hub`` and ``testhub``
are the same name to it). That rejection arrives at the worst possible moment —
mid-release, with a tag already pushed. This script is the pre-agreed escape
hatch: rename the *distribution*, keep everything else.

Three names are involved and only the first one moves:

=====================  =========================  =============================
 Distribution name      ``pip install test-hub``   this script rewrites it
 Import package         ``import test_hub``        never changes
 Console script         ``test-hub …``             never changes
=====================  =========================  =============================

So a rename is invisible to users' code and to their shell; it changes the row on
PyPI and the string in ``pip install``. That is the whole point of doing it this
way instead of renaming the package.

What it refuses to do
---------------------

It rewrites exactly one line: the ``name = "..."`` assignment inside the
``[project]`` table. Not ``[tool.poetry] name``, not ``name`` inside
``[[project.authors]]``, not a second ``[project]``-looking table in a comment.
Before writing it re-reads its own output and asserts that precisely one line
differs; if anything else moved it writes nothing and exits non-zero. A
formatting-preserving line edit is used rather than a TOML round-trip for the
same reason: ``tomllib`` cannot write, and every library that can will also
reflow comments, reorder keys and normalise quotes across a file that is full of
deliberate prose.

Usage
-----

::

    python tools/set_dist_name.py testhub-io          # rewrite
    python tools/set_dist_name.py testhub-io --dry-run
    python tools/set_dist_name.py --show              # print the current name

Exit codes: ``0`` changed (or already correct), ``2`` invalid name or malformed
``pyproject.toml``, ``3`` the safety check tripped.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

__all__ = [
    "PEP_508_NAME",
    "DistNameError",
    "current_name",
    "main",
    "normalize",
    "rewrite",
    "validate",
]

#: PEP 508 §"Names" / PEP 426. A name is alphanumeric at both ends and may carry
#: ``-``, ``_`` and ``.`` in between. Matched case-insensitively; a single
#: character is legal.
PEP_508_NAME = re.compile(r"^([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9])$")

#: A ``name = "..."`` assignment at the top level of a table, with the value in
#: either quote style and an optional trailing comment. Leading whitespace is
#: matched but not permitted to be non-empty by :func:`rewrite` — an indented key
#: inside our own ``pyproject.toml`` would mean the file is not shaped the way
#: this script assumes.
_NAME_LINE = re.compile(
    r"""^(?P<indent>\s*)name(?P<pre>\s*)=(?P<post>\s*)
        (?P<quote>["'])(?P<value>[^"']*)(?P=quote)
        (?P<trail>\s*(?:\#.*)?)$""",
    re.VERBOSE,
)

#: The start of any TOML table header, e.g. ``[project]`` or ``[[tool.x]]``.
_TABLE_HEADER = re.compile(r"^\s*\[\[?\s*(?P<path>[^]\s]+)\s*]]?\s*(?:\#.*)?$")

_PROJECT_FILE = Path(__file__).resolve().parent.parent / "pyproject.toml"


class DistNameError(Exception):
    """The file is not shaped the way this script is allowed to edit."""


def normalize(name: str) -> str:
    """PEP 503 normalisation — what PyPI actually compares names by.

    >>> normalize("Test.Hub_IO")
    'test-hub-io'
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def validate(name: str) -> str:
    """Return ``name`` if PyPI could accept it, else raise :class:`DistNameError`."""
    if not name:
        raise DistNameError("the new name is empty")
    if not PEP_508_NAME.match(name):
        raise DistNameError(
            f"{name!r} is not a valid PEP 508 distribution name. Names must start and "
            "end with a letter or digit and may contain only letters, digits, '-', '_' "
            "and '.' in between."
        )
    if len(name) > 214:
        raise DistNameError(f"{name!r} is longer than the 214-character limit PyPI enforces")
    return name


def _project_table_span(lines: list[str]) -> tuple[int, int]:
    """Index range ``[start, end)`` of the body of the ``[project]`` table.

    The body runs from the line after the ``[project]`` header to the line before
    the next table header of any kind. ``[project.urls]`` is a *different* table
    and therefore ends the span, which is exactly what we want: the ``name`` key
    we may rewrite is the one that belongs to ``[project]`` itself.
    """
    start: int | None = None
    for index, line in enumerate(lines):
        match = _TABLE_HEADER.match(line)
        if match is None:
            continue
        if start is None:
            if match.group("path") == "project" and not line.lstrip().startswith("[["):
                start = index + 1
            continue
        return start, index
    if start is None:
        raise DistNameError("no [project] table found — is this a PEP 621 pyproject.toml?")
    return start, len(lines)


def _name_line_index(lines: list[str], span: tuple[int, int]) -> int:
    """Index of the sole ``name =`` line inside the ``[project]`` table."""
    start, end = span
    found = [
        index
        for index in range(start, end)
        if (match := _NAME_LINE.match(lines[index])) is not None and not match.group("indent")
    ]
    if not found:
        raise DistNameError('[project] has no `name = "..."` line to rewrite')
    if len(found) > 1:
        raise DistNameError(
            f"[project] has {len(found)} `name =` lines (lines "
            f"{', '.join(str(i + 1) for i in found)}); refusing to guess which one is the "
            "distribution name"
        )
    return found[0]


def current_name(text: str) -> str:
    """The distribution name currently declared in ``text``."""
    lines = text.splitlines()
    index = _name_line_index(lines, _project_table_span(lines))
    match = _NAME_LINE.match(lines[index])
    assert match is not None  # _name_line_index only returns indices that matched
    return match.group("value")


def rewrite(text: str, new_name: str) -> tuple[str, int, str]:
    """Return ``(new_text, line_number, old_name)`` for a one-line rename.

    ``line_number`` is 1-based, for messages. The returned text preserves the
    original line endings, indentation, spacing around ``=``, quote style and any
    trailing comment: only the characters between the quotes change.
    """
    validate(new_name)
    lines = text.splitlines(keepends=True)
    bare = text.splitlines()
    index = _name_line_index(bare, _project_table_span(bare))

    match = _NAME_LINE.match(bare[index])
    assert match is not None
    old_name = match.group("value")

    ending = lines[index][len(bare[index]) :]  # "\n", "\r\n" or "" on the last line
    lines[index] = (
        f"{match.group('indent')}name{match.group('pre')}={match.group('post')}"
        f"{match.group('quote')}{new_name}{match.group('quote')}{match.group('trail')}{ending}"
    )
    new_text = "".join(lines)

    _assert_only_one_line_changed(text, new_text, index, unchanged_ok=old_name == new_name)
    return new_text, index + 1, old_name


def _assert_only_one_line_changed(old: str, new: str, index: int, *, unchanged_ok: bool) -> None:
    """The safety net this script's whole promise rests on.

    Compares the two versions line by line and raises unless the single
    difference is at ``index``. Cheap, and it turns any future bug in the regex
    above into a refusal rather than a mangled ``pyproject.toml``.

    ``unchanged_ok`` covers the rename-to-the-name-it-already-has case, where
    zero differing lines is the correct outcome rather than a tripped check.
    """
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    if len(old_lines) != len(new_lines):
        raise DistNameError(
            f"internal check failed: line count changed from {len(old_lines)} to {len(new_lines)}"
        )
    differing = [i for i, (a, b) in enumerate(zip(old_lines, new_lines, strict=True)) if a != b]
    allowed: list[list[int]] = [[index], []] if unchanged_ok else [[index]]
    if differing not in allowed:
        where = ", ".join(str(i + 1) for i in differing) or "none"
        raise DistNameError(
            f"internal check failed: expected only line {index + 1} to change, but {where} did"
        )


def _diff(old: str, new: str, path: Path) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
            n=2,
        )
    )


def _read(path: Path) -> str:
    """Read without newline translation.

    ``Path.read_text`` would turn a CRLF checkout — which is what git hands a
    Windows contributor by default — into LF, and writing it back would rewrite
    every line of ``pyproject.toml``. ``newline=""`` keeps whatever is on disk.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(path: Path, text: str) -> None:
    """Write back verbatim, with the line endings :func:`_read` preserved."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="set_dist_name.py",
        description="Rewrite the [project] name in pyproject.toml and nothing else.",
        epilog=(
            "The import package (test_hub) and the console script (test-hub) are "
            "unaffected. After a rename, update the PyPI Trusted Publisher to match "
            "and note it in CHANGELOG.md."
        ),
    )
    parser.add_argument("name", nargs="?", help="the new distribution name")
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=_PROJECT_FILE,
        help="path to pyproject.toml (default: the one next to this checkout)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the diff and write nothing",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="print the current distribution name and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    path: Path = args.pyproject

    try:
        text = _read(path)
    except OSError as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return 2

    try:
        if args.show or args.name is None:
            name = current_name(text)
            print(name)
            if args.name is None and not args.show:
                print(
                    f"\nnothing to do — pass a new name to rewrite it, e.g.\n"
                    f"    python {Path(__file__).name} testhub-io",
                    file=sys.stderr,
                )
            return 0

        new_text, line_number, old_name = rewrite(text, args.name)
    except DistNameError as exc:
        print(f"error: {exc}", file=sys.stderr)
        # A tripped safety check is a different kind of failure from bad input:
        # it means the file is not what this script thought, and a human should
        # look at it rather than retrying with a different argument.
        return 3 if "internal check failed" in str(exc) else 2

    if new_text == text:
        print(f'{path.name} already declares name = "{old_name}"; nothing to do')
        return 0

    print(f"--- {path}  (line {line_number})")
    print(f"  before: {old_name}   (PyPI compares this as {normalize(old_name)!r})")
    print(f"  after:  {args.name}   (PyPI compares this as {normalize(args.name)!r})")
    print()
    print(_diff(text, new_text, path), end="")

    if args.dry_run:
        print("\ndry run — nothing written")
        return 0

    _write(path, new_text)

    # Re-read from disk rather than trusting the in-memory string: this catches a
    # truncated or partially-written file, which is the one failure mode that
    # would leave the project unbuildable.
    verified = _read(path)
    if verified != new_text or current_name(verified) != args.name:
        print(f"error: {path} on disk does not match what was written", file=sys.stderr)
        return 3

    print(f"\nwrote {path}")
    print(
        "next: `python -m build` to confirm the wheel is named as expected, and update the "
        "PyPI Trusted Publisher for the new project name before tagging a release."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
