"""One error taxonomy for the whole application.

Every failure that a user can cause is one of these. They are raised anywhere and
mapped to HTTP in exactly one place (:mod:`test_hub.api.app`) and to an exit code
in exactly one place (:mod:`test_hub.cli`), so an error message reads the same
whether it arrives through ``test-hub ingest`` or ``POST /api/v1/imports``.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AdapterError",
    "AuthenticationError",
    "AuthorizationError",
    "ConfigurationError",
    "ConflictError",
    "NotFoundError",
    "PayloadTooLargeError",
    "StorageBusyError",
    "TestHubError",
    "UnknownFormatError",
    "ValidationError",
]


class TestHubError(Exception):
    """Base for every error Test Hub raises on purpose.

    ``code`` is a stable, machine-readable string that appears in API responses
    and can be matched on by scripts; ``detail`` carries structured context that
    is safe to show the caller.
    """

    code = "test_hub_error"
    http_status = 500
    exit_code = 1

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = detail or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.detail:
            payload["detail"] = self.detail
        return payload


class ValidationError(TestHubError):
    """The request or file was well-formed but said something impossible."""

    code = "validation_error"
    http_status = 422
    exit_code = 2


class AdapterError(TestHubError):
    """An adapter could not parse input it had claimed to recognise."""

    code = "adapter_error"
    http_status = 422
    exit_code = 2


class UnknownFormatError(TestHubError):
    """No installed adapter recognised the input.

    Carries the list of installed adapters so the message can tell the user what
    *is* supported instead of only what is not.
    """

    code = "unknown_format"
    http_status = 415
    exit_code = 2

    def __init__(self, message: str, *, adapters: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message, detail={"installed_adapters": adapters or []})


class NotFoundError(TestHubError):
    code = "not_found"
    http_status = 404
    exit_code = 3


class ConflictError(TestHubError):
    """A write contradicted a write that already landed."""

    code = "conflict"
    http_status = 409
    exit_code = 4


class AuthenticationError(TestHubError):
    code = "unauthorized"
    http_status = 401
    exit_code = 5


class AuthorizationError(TestHubError):
    code = "forbidden"
    http_status = 403
    exit_code = 5


class PayloadTooLargeError(TestHubError):
    code = "payload_too_large"
    http_status = 413
    exit_code = 6


class StorageBusyError(TestHubError):
    """Other writers held the database for longer than this one would wait.

    Not a failure of the request: nothing was written, and the same request
    sent again a moment later succeeds. Hence a 503 with ``Retry-After`` — which
    the SDK, and most CI HTTP clients, already back off on and retry — rather
    than the 500 or the dropped connection it used to be.
    """

    code = "storage_busy"
    http_status = 503
    exit_code = 75  # EX_TEMPFAIL: "try again later", to a supervisor
    #: Seconds a client should wait before sending the request again.
    retry_after = 2


class ConfigurationError(TestHubError):
    """Test Hub was asked to start in a state that is not safe or not coherent."""

    code = "configuration_error"
    http_status = 500
    exit_code = 78  # EX_CONFIG, so a supervisor can tell it apart from a crash
