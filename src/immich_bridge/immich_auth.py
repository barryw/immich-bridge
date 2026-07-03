"""Immich-backed identity validation shared by DAV and admin APIs."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from immich_bridge.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ImmichIdentity:
    """Validated Immich identity derived from an API key."""

    user_id: str
    email: str | None
    name: str | None
    api_key_name: str | None = None
    is_admin: bool = False


def validate_immich_api_key(
    immich_url: str,
    api_key: str,
    *,
    timeout_seconds: float = 10.0,
) -> ImmichIdentity | None:
    """Validate an Immich API key and return user/API-key metadata."""
    headers = {"x-api-key": api_key}
    base_url = immich_url.rstrip("/")

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            user_response = client.get(f"{base_url}/users/me", headers=headers)
            if user_response.status_code != 200:
                logger.info("immich_auth_failed", status=user_response.status_code)
                return None

            user_data = user_response.json()

            api_key_name = None
            key_response = client.get(f"{base_url}/api-keys/me", headers=headers)
            if key_response.status_code == 200:
                key_data = key_response.json()
                api_key_name = key_data.get("name")

    except (httpx.RequestError, ValueError) as e:
        logger.warning("immich_auth_error", error=str(e))
        return None

    user_id = user_data.get("id")
    if not user_id:
        logger.warning("immich_auth_missing_user_id")
        return None

    return ImmichIdentity(
        user_id=user_id,
        email=user_data.get("email"),
        name=user_data.get("name"),
        api_key_name=api_key_name,
        is_admin=bool(user_data.get("isAdmin")),
    )
