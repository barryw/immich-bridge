"""Request-scoped observability helpers."""

from __future__ import annotations

import contextvars
import uuid
from typing import Any

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "immich_bridge_request_id",
    default=None,
)


def new_request_id() -> str:
    """Return a compact request identifier."""
    return uuid.uuid4().hex[:16]


def get_request_id() -> str | None:
    """Return the current request identifier."""
    return _request_id.get()


def set_request_id(request_id: str) -> contextvars.Token[Any]:
    """Set the request identifier for the current context."""
    return _request_id.set(request_id)


def reset_request_id(token: contextvars.Token[Any]) -> None:
    """Reset the request identifier context."""
    _request_id.reset(token)
