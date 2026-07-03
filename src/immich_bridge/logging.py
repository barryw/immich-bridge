"""Structured logging configuration."""

import logging
import sys
from collections.abc import MutableMapping
from typing import Any, cast

import structlog


REDACTED_FIELDS = {"token", "password", "secret", "api_key", "encryption_key"}


def _redact_sensitive(
    logger: structlog.types.WrappedLogger,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Redact sensitive fields from log output."""
    for field in REDACTED_FIELDS:
        if field in event_dict:
            event_dict[field] = "[REDACTED]"
    return event_dict


def setup_logging(log_level: str = "INFO", log_format: str = "json") -> None:
    """Configure structlog for JSON or console output."""
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _redact_sensitive,
    ]

    renderer: structlog.types.Processor
    if log_format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, log_level.upper())),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    """Get a logger instance with the given name."""
    return cast(structlog.typing.FilteringBoundLogger, structlog.get_logger(name))
