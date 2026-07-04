"""Public authentication and self-service APIs."""

from __future__ import annotations

import secrets
from datetime import datetime
from threading import Lock
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from immich_bridge.admin_store import AdminStore
from immich_bridge.api.admin import (
    SESSION_COOKIE,
    AdminSessionResponse,
    _create_session_response,
    _forget_admin_api_key,
    _grants_from_session,
    _session_to_response,
    _store,
    _token_hash,
    require_admin_session,
)
from immich_bridge.authz import share_guest_grant
from immich_bridge.config import Settings, get_settings
from immich_bridge.share_auth import (
    ShareLinkError,
    parse_share_link,
    share_key_hash,
    share_link_matches_library,
    validate_immich_share_link,
)

router = APIRouter(prefix="/api", tags=["auth"])
_share_keys: dict[str, dict[str, tuple[str, datetime | None]]] = {}
_share_keys_lock = Lock()


class ShareLoginRequest(BaseModel):
    """Login payload for an Immich shared link."""

    share_url: str = Field(min_length=1, max_length=4096)


class MountResponse(BaseModel):
    """A mount available to the current principal."""

    id: str
    kind: str
    library_id: str | None = None
    library_name: str | None = None
    display_name: str
    scope: str
    capabilities: list[str]
    share_id: str | None = None
    asset_count: int | None = None
    expires_at: str | None = None


class MountsResponse(BaseModel):
    """Available mounts for the current principal."""

    mounts: list[MountResponse]


@router.post("/auth/share-link", response_model=AdminSessionResponse)
async def create_share_session(
    payload: ShareLoginRequest,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
) -> AdminSessionResponse:
    """Create a guest session from an Immich shared link."""
    grant, share_key = _share_grant_from_payload(payload, store, settings)

    token = secrets.token_urlsafe(32)
    token_hash = _token_hash(token)
    principal_id = f"share:{grant['library_id']}:{str(grant['share_key_hash'])[:16]}"
    session_response = _create_session_response(
        response=response,
        store=store,
        settings=settings,
        token=token,
        principal_id=principal_id,
        principal_kind="share_guest",
        user_id=principal_id,
        email=None,
        name=str(grant.get("share_name") or "Immich Share"),
        api_key_name=None,
        grants=[grant],
    )
    _remember_share_key(token_hash, share_key, session_response.expires_at)
    return session_response


@router.post("/auth/session/share-link", response_model=AdminSessionResponse)
async def add_share_to_current_session(
    payload: ShareLoginRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> AdminSessionResponse:
    """Attach another Immich shared link to the current viewer session."""
    if str(session.get("principal_kind") or "") != "share_guest":
        raise HTTPException(status_code=403, detail="Only viewer sessions can add share links")

    token_hash = str(session.get("_token_hash") or "")
    if not token_hash:
        raise HTTPException(status_code=401, detail="Admin session required")

    grant, share_key = _share_grant_from_payload(payload, store, settings)
    grants = _grants_from_session(session)
    share_hash = str(grant["share_key_hash"])
    if not any(str(existing.get("share_key_hash") or "") == share_hash for existing in grants):
        grants.append(grant)
        store.update_session_grants(token_hash, grants)

    _remember_share_key(token_hash, share_key, str(session.get("expires_at") or ""))
    updated_session = store.get_session(token_hash)
    if updated_session is None:
        raise HTTPException(status_code=401, detail="Admin session expired")
    updated_session["_token_hash"] = token_hash
    return _session_to_response(updated_session, settings)


@router.get("/me", response_model=AdminSessionResponse)
async def get_current_principal(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> AdminSessionResponse:
    """Return the current bridge principal and grants."""
    return _session_to_response(session, settings)


@router.get("/me/mounts", response_model=MountsResponse)
async def get_current_mounts(
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> MountsResponse:
    """Return mounts available to the current bridge principal."""
    libraries = {str(library["id"]): library for library in store.list_libraries()}
    mounts: list[MountResponse] = []
    for grant in _grants_from_session(session):
        scope = str(grant.get("scope") or "")
        library_id = grant.get("library_id") if isinstance(grant.get("library_id"), str) else None
        library = libraries.get(library_id or "")
        capabilities = [
            capability
            for capability in grant.get("capabilities", [])
            if isinstance(capability, str)
        ]

        if scope == "instance":
            for visible_library in libraries.values():
                mounts.append(_library_mount_response(visible_library, capabilities=capabilities))
            continue

        if scope == "library" and library is not None:
            mounts.append(_library_mount_response(library, capabilities=capabilities))
            continue

        if scope == "share" and library is not None:
            share_id = str(grant.get("share_id") or "shared-link")
            share_name = str(grant.get("share_name") or "Immich Share")
            mount_id = f"share:{library_id}:{share_id}"
            mounts.append(
                MountResponse(
                    id=mount_id,
                    kind="share",
                    library_id=library_id,
                    library_name=str(library["name"]),
                    display_name=share_name,
                    scope="share",
                    capabilities=capabilities,
                    share_id=share_id,
                    asset_count=_optional_int(grant.get("asset_count")),
                    expires_at=grant.get("expires_at")
                    if isinstance(grant.get("expires_at"), str)
                    else None,
                )
            )

    return MountsResponse(mounts=mounts)


@router.delete("/auth/session", status_code=204)
async def delete_current_session(
    response: Response,
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> None:
    """Delete the current bridge session."""
    token_hash = str(session.get("_token_hash") or "")
    if token_hash:
        store.delete_session(token_hash)
        _forget_share_key(token_hash)
        _forget_admin_api_key(token_hash)
    response.delete_cookie(SESSION_COOKIE, path="/")


def get_share_key_for_session(token_hash: str, share_key_hash_value: str | None = None) -> str | None:
    """Return a live raw share key for a session token hash, when cached."""
    with _share_keys_lock:
        entries = _share_keys.get(token_hash)
        if not entries:
            return None
        if share_key_hash_value:
            entry = entries.get(share_key_hash_value)
        else:
            entry = next(iter(entries.values()), None)
        if entry is None:
            return None
        share_key, expires_at = entry
        if expires_at is not None and expires_at <= datetime.now(expires_at.tzinfo):
            stale_hashes = [
                key
                for key, (_, cached_expires_at) in entries.items()
                if cached_expires_at is not None
                and cached_expires_at <= datetime.now(cached_expires_at.tzinfo)
            ]
            for key in stale_hashes:
                entries.pop(key, None)
            if not entries:
                _share_keys.pop(token_hash, None)
            return None
        return share_key


def _remember_share_key(token_hash: str, share_key: str, expires_at: str | None) -> None:
    parsed_expires_at = None
    if expires_at:
        try:
            parsed_expires_at = datetime.fromisoformat(expires_at)
        except ValueError:
            parsed_expires_at = None
    with _share_keys_lock:
        _share_keys.setdefault(token_hash, {})[share_key_hash(share_key)] = (
            share_key,
            parsed_expires_at,
        )


def _forget_share_key(token_hash: str) -> None:
    with _share_keys_lock:
        _share_keys.pop(token_hash, None)


def _share_grant_from_payload(
    payload: ShareLoginRequest,
    store: AdminStore,
    settings: Settings,
) -> tuple[dict[str, Any], str]:
    try:
        parsed = parse_share_link(payload.share_url)
    except ShareLinkError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    library = _library_for_share_link(store, parsed)
    try:
        share = validate_immich_share_link(
            str(library["immichUrl"]),
            parsed.share_key,
            timeout_seconds=settings.immich_timeout_seconds,
        )
    except ShareLinkError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e

    return (
        share_guest_grant(
            str(library["id"]),
            share_id=share.share_id,
            share_name=share.name,
            share_key_hash=share_key_hash(parsed.share_key),
            allow_download=share.allow_download,
            allow_upload=share.allow_upload,
            expires_at=share.expires_at,
            asset_count=share.asset_count,
        ),
        parsed.share_key,
    )


def _library_for_share_link(store: AdminStore, parsed_share_link: Any) -> dict[str, Any]:
    for library in store.list_libraries():
        if share_link_matches_library(
            parsed_share_link,
            str(library["immichUrl"]),
            public_url=library.get("publicUrl")
            if isinstance(library.get("publicUrl"), str)
            else None,
            share_hosts=[
                str(host) for host in library.get("shareHosts", []) if isinstance(host, str)
            ],
        ):
            return library
    raise HTTPException(status_code=400, detail="Share link host is not configured on this bridge")


def _library_mount_response(
    library: dict[str, Any],
    *,
    capabilities: list[str],
) -> MountResponse:
    return MountResponse(
        id=f"library:{library['id']}",
        kind="library",
        library_id=str(library["id"]),
        library_name=str(library["name"]),
        display_name=str(library["name"]),
        scope="library",
        capabilities=capabilities,
    )


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
