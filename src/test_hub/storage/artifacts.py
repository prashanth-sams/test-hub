"""Content-addressed artifact storage on the local filesystem.

Screenshots, videos, traces and the raw uploaded reports all land here. The key
is the SHA-256 of the content, sharded two levels deep
(``<root>/<aa>/<bb>/<sha256>``) so that a directory never accumulates more
entries than a filesystem is happy to list. Addressing by content is not an
optimisation, it is the only thing that makes a hundred parallel shards
uploading the same 400 KiB screenshot cost 400 KiB.

Three properties this module exists to guarantee:

**The size limit is enforced while reading, never after.** Reading an upload
into memory and then measuring it is precisely the denial of service the limit
was added to prevent, so :meth:`FilesystemArtifactStore.put` counts bytes as
they stream past and aborts the moment the count is exceeded.

**A file appears complete or not at all.** Content is streamed into a temporary
file in the same directory tree and then :meth:`~pathlib.Path.replace`\\ d into
place, which is atomic within a filesystem. Two publishers racing to upload the
same bytes therefore produce one intact file, never a half-written one that the
next reader hashes and finds corrupt.

**No path is ever built from a name a user chose.** The only thing that becomes
a path here is a validated 64-character hex digest. Display names are stored as
data and never as a path, and a name that is shaped like one — a separator, a
``..``, a NUL, a Windows device — is refused outright rather than quietly
rewritten into something the caller never asked for. Callers that want a
best-effort repair instead of a refusal should sanitize before they get here;
``test_hub.service.runs.safe_artifact_name`` is that repair.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import uuid
from pathlib import Path
from typing import IO

from sqlalchemy import select
from sqlalchemy.orm import Session

from test_hub.config import Settings
from test_hub.domain.identity import workspace_id as derive_workspace_id
from test_hub.domain.models import Artifact, utcnow
from test_hub.errors import NotFoundError, PayloadTooLargeError, ValidationError
from test_hub.storage.base import StoredArtifact
from test_hub.storage.models import ArtifactRow, RunRow, short_text
from test_hub.storage.repositories import artifact_to_model

__all__ = ["CHUNK_SIZE", "FilesystemArtifactStore", "sanitize_artifact_name"]

#: Streaming chunk. Large enough that the syscall overhead disappears, small
#: enough that a rejected 10 GB upload never holds 10 GB of anything.
CHUNK_SIZE = 64 * 1024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

#: Names Windows resolves to devices rather than files, with or without an
#: extension. Writing to one of these is not a file operation at all.
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


def sanitize_artifact_name(name: str) -> str:
    """Return a display name that is safe to echo, log and offer as a download.

    Refuses rather than repairs. A name is a label — it never becomes a path
    here — but it does end up in a ``Content-Disposition`` header and in a
    browser, and a store that silently turned ``../../etc/passwd`` into
    ``passwd`` would hand the next component a name its author never wrote and
    make the guarantee impossible to test. So the four dangerous shapes are
    errors: a path separator, a ``..``, a NUL, and a Windows device name.

    A publisher that legitimately reports ``tests/checkout/shot.png`` should be
    reduced to its last component *before* the store sees it — that is a
    presentation decision, and ``test_hub.service.runs.safe_artifact_name``
    already makes it on the upload path.
    """
    if "\x00" in name:
        raise ValidationError("artifact name contains a NUL byte")

    candidate = _CONTROL_RE.sub("", name).strip()
    if not candidate:
        raise ValidationError(f"artifact name {name!r} is empty once cleaned")
    if "/" in candidate or "\\" in candidate:
        raise ValidationError(
            f"artifact name {name!r} contains a path separator; pass the file name only"
        )
    if ".." in candidate:
        raise ValidationError(f"artifact name {name!r} looks like a path traversal")
    if candidate.split(".")[0].lower() in _RESERVED_NAMES:
        raise ValidationError(f"artifact name {name!r} is a reserved device name")
    return candidate[:255]


def _validated_digest(sha256: str) -> str:
    """The only value in this module that is ever allowed to become a path."""
    digest = sha256.strip().lower()
    if not _SHA256_RE.match(digest):
        raise ValidationError(
            "artifact digest must be 64 lowercase hex characters",
            detail={"sha256": sha256[:80]},
        )
    return digest


class FilesystemArtifactStore:
    """The shipped :class:`~test_hub.storage.base.ArtifactStore`.

    Blobs live on disk, keyed by digest; the rows that describe them live in the
    same transaction as everything else, so an artifact recorded by an import
    that later fails rolls back with it. The blob itself is left behind — it is
    unreferenced, deduplicated and harmless, and deleting it would be wrong the
    moment two runs share it.
    """

    def __init__(self, root: Path, session: Session, settings: Settings) -> None:
        self._root = root
        self._session = session
        self._settings = settings

    # ------------------------------------------------------------- blobs

    def path_for(self, sha256: str) -> Path:
        digest = _validated_digest(sha256)
        return self._root / digest[0:2] / digest[2:4] / digest

    def put(self, stream: IO[bytes], *, max_bytes: int) -> StoredArtifact:
        incoming = self._root / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        temp = incoming / f"{uuid.uuid4().hex}.part"

        digest = hashlib.sha256()
        size = 0
        try:
            with temp.open("wb") as handle:
                while True:
                    chunk = stream.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise PayloadTooLargeError(
                            f"artifact exceeds the {max_bytes} byte limit",
                            detail={"limit_bytes": max_bytes},
                        )
                    digest.update(chunk)
                    handle.write(chunk)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

        sha256 = digest.hexdigest()
        final = self.path_for(sha256)
        if final.exists():
            # Same content, already stored. Rewriting it would replace a good
            # file with an identical one and briefly unlink it for readers.
            temp.unlink(missing_ok=True)
            return StoredArtifact(sha256=sha256, size_bytes=size, path=str(final), created=False)

        final.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            temp.chmod(0o600)
        temp.replace(final)
        return StoredArtifact(sha256=sha256, size_bytes=size, path=str(final), created=True)

    def open(self, sha256: str) -> IO[bytes]:
        path = self.path_for(sha256)
        try:
            return path.open("rb")
        except FileNotFoundError as exc:
            raise NotFoundError(
                "no stored artifact with that digest", detail={"sha256": sha256}
            ) from exc

    def exists(self, sha256: str) -> bool:
        return self.path_for(sha256).is_file()

    def delete(self, sha256: str) -> bool:
        path = self.path_for(sha256)
        if not path.is_file():
            return False
        path.unlink()
        # Prune the shard directories, ignoring the race where another upload
        # has just refilled one.
        for parent in (path.parent, path.parent.parent):
            with contextlib.suppress(OSError):
                parent.rmdir()
        return True

    # -------------------------------------------------------------- rows

    def record(self, artifact: Artifact) -> Artifact:
        # Validate and resolve EVERYTHING before touching the session. Two
        # separate traps, one rule:
        #
        # * `_workspace_for` issues a query, and SQLAlchemy autoflushes pending
        #   objects before a query runs — so adding the row first would flush a
        #   half-built row and fail on a NOT NULL column.
        # * a rejected name must leave the session exactly as it found it. Adding
        #   the row and *then* raising leaves a row with `name = NULL` pending,
        #   and the next query in the same unit of work dies on an autoflush
        #   nobody can trace back to here.
        name = sanitize_artifact_name(artifact.name)
        digest = _validated_digest(artifact.sha256)
        workspace_id = self._workspace_for(artifact.run_id)

        row = self._session.get(ArtifactRow, artifact.id)
        if row is None:
            row = ArtifactRow(id=artifact.id, run_id=artifact.run_id, created_at=utcnow())
            self._session.add(row)
        row.workspace_id = workspace_id
        row.run_id = artifact.run_id
        row.test_id = artifact.test_id
        row.attempt_id = artifact.attempt_id
        row.kind = str(artifact.kind)
        row.name = name
        row.content_type = short_text(artifact.content_type) or "application/octet-stream"
        row.size_bytes = artifact.size_bytes
        row.sha256 = digest
        row.url = short_text(artifact.url)
        row.meta = dict(artifact.metadata)
        self._session.flush()
        return artifact.model_copy(update={"name": row.name, "created_at": row.created_at})

    def _workspace_for(self, run_id: uuid.UUID) -> uuid.UUID:
        run = self._session.get(RunRow, run_id)
        if run is not None:
            return run.workspace_id
        return derive_workspace_id(self._settings.workspace_slug)

    def list_for_run(self, run_id: uuid.UUID) -> list[Artifact]:
        rows = (
            self._session.execute(
                select(ArtifactRow)
                .where(ArtifactRow.run_id == run_id)
                .order_by(ArtifactRow.created_at.asc())
            )
            .scalars()
            .all()
        )
        return [self._to_model(row) for row in rows]

    def get(self, artifact_id: uuid.UUID) -> Artifact | None:
        row = self._session.get(ArtifactRow, artifact_id)
        return self._to_model(row) if row is not None else None

    @staticmethod
    def _to_model(row: ArtifactRow) -> Artifact:
        return artifact_to_model(row)
