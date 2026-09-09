"""Test Hub — universal test observability.

Test Hub collects test results from any language, framework, team or pipeline,
normalizes them into one canonical model and shows them in one dashboard.

The package is layered, and the layers only point one way::

    cli ──▶ api ──▶ service ──▶ storage
     │       │         │
     │       │         └──▶ domain ◀── adapters
     ├──▶ export ──────────▶ domain
     └──▶ sdk ─(http)─▶ api

:mod:`test_hub.domain` imports nothing else from the package: it is the contract.
"""

from __future__ import annotations

from test_hub.version import CLOUDEVENTS_SPEC_VERSION, SCHEMA_VERSION, __version__

__all__ = ["CLOUDEVENTS_SPEC_VERSION", "SCHEMA_VERSION", "__version__"]
