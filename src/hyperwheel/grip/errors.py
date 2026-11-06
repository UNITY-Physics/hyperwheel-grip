"""Errors from the GRIP client. Messages are safe to log: no secrets, no patient data."""

from __future__ import annotations


class GripError(Exception):
    pass


class Transient(GripError):
    """Timeout, dropped connection, 429, 503 or another 5xx, still failing after the in-call retries.

    The file is kept and tried again later. ``no_answer`` is true for a timeout or a dropped
    connection (as opposed to an error answer); ``retry_after`` is GRIP's Retry-After, if given.
    """

    def __init__(self, message: str, *, no_answer: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.no_answer = no_answer
        self.retry_after = retry_after


class KeyRejected(GripError):
    """GRIP's token endpoint refused this project's key (wrong, disabled or expired)."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class AuthFailed(GripError):
    """The API still answers 401 with a freshly issued token."""


class Refused(GripError):
    """GRIP answered with a 4xx that must not be retried. The file is kept and flagged.

    ``error`` is GRIP's error text from the answer, if any. It is used to tell
    refusals apart and is not put in the message, so the message stays safe to log.
    """

    def __init__(self, message: str, status: int, error: str | None = None):
        super().__init__(message)
        self.status = status
        self.error = error

    @property
    def invalid_name(self) -> bool:
        """GRIP refused a raw file's name (its mock answers 400 "invalid file name")."""
        text = (self.error or "").lower().replace("_", " ")
        return self.status == 400 and ("invalid file name" in text or "invalid filename" in text)
