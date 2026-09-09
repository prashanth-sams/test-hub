"""Paths to the invented report fixtures.

A module rather than functions in ``conftest.py`` so test files can import them
by name without pytest's rootdir games getting involved.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["FIXTURES", "canonical", "junit"]

FIXTURES = Path(__file__).parent / "fixtures"


def junit(name: str) -> Path:
    """A JUnit XML fixture."""
    return FIXTURES / "junit" / name


def canonical(name: str) -> Path:
    """A Test Hub canonical JSON fixture."""
    return FIXTURES / "canonical" / name
