"""Exception hierarchy.

Every error that reaches the UI carries a message a founder can act on. Routes catch
``AppError`` and show ``str(exc)``; nothing here should need a stack trace to be understood.
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for expected, user-facing failures."""


class ConfigError(AppError):
    """The environment configuration is invalid or incomplete."""


class IngestError(AppError):
    """The deck could not be read (corrupt, encrypted, empty, unreadable)."""


class UnsupportedFormatError(IngestError):
    """The file is not a supported deck format."""


class UploadRejectedError(IngestError):
    """The upload failed a security check (size, type, archive structure)."""


class LLMError(AppError):
    """The external model is unavailable or returned an unusable response."""


class SessionError(AppError):
    """The requested action does not fit the current session state."""


class ReportError(AppError):
    """The PDF report could not be generated."""
