"""Authorization grants and signed grant tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, Literal

Capability = Literal[
    "browse",
    "download",
    "thumbnail",
    "upload",
    "manage_views",
    "manage_policy",
    "manage_library",
    "manage_instance",
    "diagnostics",
]

LIBRARY_ADMIN_CAPABILITIES: tuple[Capability, ...] = (
    "browse",
    "download",
    "thumbnail",
    "upload",
    "manage_views",
    "manage_policy",
    "manage_library",
    "diagnostics",
)

SHARE_GUEST_BASE_CAPABILITIES: tuple[Capability, ...] = (
    "browse",
    "thumbnail",
)

SUPERADMIN_CAPABILITIES: tuple[Capability, ...] = (
    *LIBRARY_ADMIN_CAPABILITIES,
    "manage_instance",
)


def library_admin_grant(library_id: str) -> dict[str, Any]:
    """Return the standard grant for an Immich admin of one library."""
    return {
        "scope": "library",
        "library_id": library_id,
        "capabilities": list(LIBRARY_ADMIN_CAPABILITIES),
    }


def superadmin_grant() -> dict[str, Any]:
    """Return the standard instance-wide superadmin grant."""
    return {
        "scope": "instance",
        "library_id": None,
        "capabilities": list(SUPERADMIN_CAPABILITIES),
    }


def share_guest_grant(
    library_id: str,
    *,
    share_id: str,
    share_name: str,
    share_key_hash: str,
    allow_download: bool,
    allow_upload: bool,
    expires_at: str | None,
    asset_count: int | None = None,
) -> dict[str, Any]:
    """Return a read-mostly grant for one Immich shared link."""
    capabilities: list[Capability] = list(SHARE_GUEST_BASE_CAPABILITIES)
    if allow_download:
        capabilities.append("download")
    if allow_upload:
        capabilities.append("upload")
    return {
        "scope": "share",
        "library_id": library_id,
        "share_id": share_id,
        "share_name": share_name,
        "share_key_hash": share_key_hash,
        "allow_download": allow_download,
        "allow_upload": allow_upload,
        "asset_count": asset_count,
        "expires_at": expires_at,
        "capabilities": capabilities,
    }


def has_capability(
    grants: list[dict[str, Any]],
    capability: Capability,
    *,
    library_id: str | None = None,
) -> bool:
    """Return whether a grant set includes a capability."""
    for grant in grants:
        capabilities = grant.get("capabilities")
        if not isinstance(capabilities, list) or capability not in capabilities:
            continue

        if grant.get("scope") == "instance":
            return True

        if library_id is not None and grant.get("library_id") == library_id:
            return True

    return False


def sign_grants(
    secret: str,
    *,
    session_id: str,
    principal_id: str,
    principal_kind: str,
    grants: list[dict[str, Any]],
    expires_at: str,
) -> str:
    """Return a compact HMAC-signed grants token."""
    payload = {
        "iss": "immich-bridge",
        "aud": "immich-bridge-api",
        "sid": session_id,
        "sub": principal_id,
        "kind": principal_kind,
        "grants": grants,
        "exp": expires_at,
    }
    payload_bytes = _canonical_json(payload)
    signature = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).digest()
    return f"{_b64(payload_bytes)}.{_b64(signature)}"


def verify_grants_token(secret: str, token: str) -> dict[str, Any] | None:
    """Return a signed grant payload when the token signature is valid."""
    payload_text, separator, signature_text = token.partition(".")
    if not separator:
        return None

    try:
        payload_bytes = _unb64(payload_text)
        signature = _unb64(signature_text)
    except ValueError:
        return None

    expected = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        return None

    try:
        payload = json.loads(payload_bytes.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    return payload if isinstance(payload, dict) else None


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
