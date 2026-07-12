"""Structured logging.

One formatter, key=value fields, and two conventions:

- the framework logs one line per handled update on ``vitrine.update``
  (handler, user, duration, status) — installed automatically;
- apps log domain/audit events via :func:`audit` (logger ``vitrine.audit``)
  or :func:`log_event` on their own loggers.
"""

from __future__ import annotations

import logging
from typing import Any

FIELDS_KEY = "vitrine_fields"


class KeyValueFormatter(logging.Formatter):
    """``2026-07-11T12:00:00 INFO vitrine.update update.handled handler=start user=42 ms=13``"""  # noqa

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        fields: dict[str, Any] | None = getattr(record, FIELDS_KEY, None)
        if not fields:
            return base

        rendered = " ".join(f"{key}={_scalar(value)}" for key, value in fields.items())
        return f"{base} {rendered}"


def _scalar(value: Any) -> str:
    text = str(value)
    if " " in text or "=" in text:
        return repr(text)

    return text


def setup_logging(level: int | str = logging.INFO) -> None:
    """Opinionated default: key=value lines on stderr. Entirely optional."""
    handler = logging.StreamHandler()
    handler.setFormatter(KeyValueFormatter())
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def log_event(
    logger: logging.Logger, event: str, /, level: int = logging.INFO, **fields: Any
) -> None:
    """Emit one structured line: ``event key=value ...``."""
    logger.log(level, event, extra={FIELDS_KEY: fields})


_audit_logger = logging.getLogger("vitrine.audit")


def audit(action: str, *, actor: Any = None, **fields: Any) -> None:
    """App/audit-log convention: who did what, as structured fields."""
    if actor is not None:
        fields = {"actor": actor, **fields}
    log_event(_audit_logger, action, **fields)
