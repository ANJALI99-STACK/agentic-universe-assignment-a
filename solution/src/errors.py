"""Exceptions shared across modules."""
from __future__ import annotations


class IngestError(RuntimeError):
    """The drop cannot be ingested (bad layout, unknown workflow, unreadable or changed file...).

    Messages never contain file content, so they are safe to log.
    """
