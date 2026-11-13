"""Structured (key=value) logging helpers. Callers must never pass PII as fields."""
from __future__ import annotations

import logging
import sys


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("ts=%(asctime)s level=%(levelname)s logger=%(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def log_kv(logger: logging.Logger, message: str, level: int = logging.INFO, **fields: object) -> None:
    """Log ``message`` followed by ``key=value`` pairs."""
    pairs = " ".join(f"{k}={v}" for k, v in fields.items())
    logger.log(level, "msg=%s %s", message, pairs)
