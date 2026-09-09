"""Single source of truth for the package version.

Read by Hatchling at build time (``[tool.hatch.version]``) and by the API, the
CLI and the snapshot exporter at run time.
"""

from __future__ import annotations

__version__ = "0.1.0"

#: The version of the public wire contract — the shape of everything in
#: :mod:`test_hub.domain.models` and :mod:`test_hub.domain.events`. It moves
#: independently of ``__version__`` and only ever on a breaking change.
SCHEMA_VERSION = 1

#: The CloudEvents ``specversion`` we emit and accept.
CLOUDEVENTS_SPEC_VERSION = "1.0"
