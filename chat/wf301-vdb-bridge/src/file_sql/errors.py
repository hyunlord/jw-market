"""Typed failures exposed by the file-SQL boundary."""

from __future__ import annotations


class FileSqlError(RuntimeError):
    """Base error for scoped file SQL operations."""


class FileSqlDisabledError(FileSqlError):
    """Raised when dormant file SQL APIs are invoked while disabled."""


class FileSqlNotFoundError(FileSqlError):
    """Raised when a logical table is not owned by the caller session."""


class FileSqlRejectedError(FileSqlError):
    """Raised when a query violates the read-only policy or runtime limits."""
