"""Admin API for configuring Immich Bridge."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from immich_bridge.admin_store import DEFAULT_LIBRARY_ID
from immich_bridge.admin_store import DEFAULT_MOUNT_SETTINGS, DEFAULT_WRITE_POLICY
from immich_bridge.admin_store import AdminStore, get_admin_store
from immich_bridge.authz import (
    Capability,
    has_capability,
    library_admin_grant,
    sign_grants,
    superadmin_grant,
)
from immich_bridge.config import Settings, get_settings
from immich_bridge.fs_model import safe_segment
from immich_bridge.immich_auth import ImmichIdentity, validate_immich_api_key
from immich_bridge.immich_client import ImmichApiError, ImmichClient
from immich_bridge.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])
SESSION_COOKIE = "immich_bridge_admin"
_admin_api_keys: dict[str, tuple[str, datetime]] = {}
_admin_api_keys_lock = Lock()


@dataclass(frozen=True)
class AdminContext:
    """Authenticated admin context with a live Immich API key."""

    session: dict[str, Any]
    api_key: str
    library_id: str | None = None
    library_url: str | None = None


class AdminLoginRequest(BaseModel):
    """Admin login payload."""

    username: str = Field(default="", max_length=320)
    api_key: str = Field(min_length=1, max_length=4096)


class AdminUser(BaseModel):
    """Authenticated Immich admin user."""

    id: str
    email: str | None = None
    name: str | None = None
    api_key_name: str | None = None


class PrincipalResponse(BaseModel):
    """Authenticated bridge principal."""

    id: str
    kind: str
    display_name: str | None = None


class GrantResponse(BaseModel):
    """Server-issued authorization grant hint."""

    scope: str
    library_id: str | None = None
    share_id: str | None = None
    share_name: str | None = None
    share_key_hash: str | None = None
    allow_download: bool | None = None
    allow_upload: bool | None = None
    asset_count: int | None = None
    expires_at: str | None = None
    capabilities: list[str]


class AdminSessionResponse(BaseModel):
    """Admin session response."""

    authenticated: bool
    user: AdminUser | None = None
    principal: PrincipalResponse | None = None
    grants: list[GrantResponse] = Field(default_factory=list)
    grant_token: str | None = None
    expires_at: str | None = None
    session_token: str | None = None


class LibraryResponse(BaseModel):
    """Configured Immich library."""

    id: str
    name: str
    immich_url: str
    public_url: str | None = None
    share_hosts: list[str] = Field(default_factory=list)
    is_default: bool
    created_at: str
    updated_at: str


class LibrariesResponse(BaseModel):
    """Configured Immich libraries visible to the current principal."""

    libraries: list[LibraryResponse]


class LibraryPayload(BaseModel):
    """Create/update payload for an Immich library."""

    id: str | None = Field(default=None, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    name: str = Field(min_length=1, max_length=120)
    immich_url: str = Field(min_length=1, max_length=2048)
    public_url: str | None = Field(default=None, max_length=2048)
    share_hosts: list[str] = Field(default_factory=list, max_length=50)
    is_default: bool = False

    @field_validator("public_url")
    @classmethod
    def validate_public_url(cls, value: str | None) -> str | None:
        """Normalize optional public URLs."""
        if value is None:
            return None
        normalized = value.strip().rstrip("/")
        return normalized or None

    @field_validator("share_hosts")
    @classmethod
    def validate_share_hosts(cls, value: list[str]) -> list[str]:
        """Normalize allowed share-link hosts."""
        hosts: list[str] = []
        seen: set[str] = set()
        for item in value:
            host = item.strip().casefold()
            if not host or host in seen:
                continue
            hosts.append(host)
            seen.add(host)
        return hosts


class ViewFilters(BaseModel):
    """Immich metadata-search filters for a saved view."""

    album_ids: list[str] = Field(default_factory=list)
    person_ids: list[str] = Field(default_factory=list)
    tag_ids: list[str] = Field(default_factory=list)
    is_favorite: bool | None = None
    media_type: Literal["IMAGE", "VIDEO"] | None = None
    taken_after: str | None = None
    taken_before: str | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    query: str | None = None
    original_file_name: str | None = None
    ocr: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None


class ViewPayload(BaseModel):
    """Saved view create/update payload."""

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    enabled: bool = True
    layout: Literal["date_buckets", "flat"] = "date_buckets"
    filters: ViewFilters = Field(default_factory=ViewFilters)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Reject path-hostile names."""
        name = value.strip()
        if not name or "/" in name or "\\" in name:
            raise ValueError("View names must be non-empty path segments")
        if name in {".", ".."}:
            raise ValueError("View name is reserved")
        if safe_segment(name, fallback="View") != name:
            raise ValueError("View name contains unsupported filesystem characters")
        return name


class ViewResponse(ViewPayload):
    """Saved view response."""

    id: str
    created_at: str
    updated_at: str
    match_count: int | None = None


class ViewsResponse(BaseModel):
    """Saved views list response."""

    views: list[ViewResponse]


class MatchCountRequest(BaseModel):
    """Saved view count preview request."""

    filters: ViewFilters


class MatchCountResponse(BaseModel):
    """Saved view count preview response."""

    count: int | None


class OptionItem(BaseModel):
    """Selectable Immich metadata option."""

    id: str
    name: str
    color: str | None = None
    asset_count: int | None = None
    hidden: bool | None = None


class OptionsResponse(BaseModel):
    """Selectable options response."""

    items: list[OptionItem]


class MountSettings(BaseModel):
    """Top-level mount configuration."""

    albums_enabled: bool = True
    timeline_enabled: bool = True
    favorites_enabled: bool = True
    views_enabled: bool = True
    tags_enabled: bool = False
    people_enabled: bool = False
    album_folder_split_threshold: int = Field(default=200, ge=0, le=100_000)
    day_folder_split_threshold: int = Field(default=1000, ge=0, le=100_000)
    filename_mode: Literal["date-original-id", "original", "stable"] = "date-original-id"


class WritePolicy(BaseModel):
    """WebDAV write policy configuration."""

    root_uploads: bool = True
    album_uploads: bool = True
    album_create: bool = True
    album_membership_delete: bool = True
    permanent_delete: bool = False
    move_copy: bool = False
    overwrite: bool = False


class DiagnosticsResponse(BaseModel):
    """Admin diagnostics response."""

    immich_url: str
    database_path: str
    redis_enabled: bool
    metrics_enabled: bool
    webdav_port: int
    admin_port: int
    view_count: int
    mount: MountSettings
    write_policy: WritePolicy


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _remember_admin_api_key(token_hash: str, api_key: str, expires_at: datetime) -> None:
    with _admin_api_keys_lock:
        _admin_api_keys[token_hash] = (api_key, expires_at)


def _forget_admin_api_key(token_hash: str) -> None:
    with _admin_api_keys_lock:
        _admin_api_keys.pop(token_hash, None)


def _get_admin_api_key(token_hash: str) -> str | None:
    with _admin_api_keys_lock:
        entry = _admin_api_keys.get(token_hash)
        if entry is None:
            return None
        api_key, expires_at = entry
        if expires_at <= datetime.now(UTC):
            _admin_api_keys.pop(token_hash, None)
            return None
        return api_key


def _identity_to_user(identity: ImmichIdentity) -> AdminUser:
    return AdminUser(
        id=identity.user_id,
        email=identity.email,
        name=identity.name,
        api_key_name=identity.api_key_name,
    )


def _session_to_response(
    session: dict[str, Any],
    settings: Settings,
    *,
    session_token: str | None = None,
) -> AdminSessionResponse:
    grants = _grants_from_session(session)
    principal_id = str(session.get("principal_id") or session["user_id"])
    principal_kind = str(session.get("principal_kind") or "library_admin")
    return AdminSessionResponse(
        authenticated=True,
        user=AdminUser(
            id=str(session["user_id"]),
            email=session.get("email"),
            name=session.get("name"),
            api_key_name=session.get("api_key_name"),
        ),
        principal=PrincipalResponse(
            id=principal_id,
            kind=principal_kind,
            display_name=session.get("name") or session.get("email"),
        ),
        grants=[GrantResponse.model_validate(grant) for grant in grants],
        grant_token=sign_grants(
            _grant_signing_secret(settings),
            session_id=str(session.get("token_hash") or ""),
            principal_id=principal_id,
            principal_kind=principal_kind,
            grants=grants,
            expires_at=str(session["expires_at"]),
        ),
        expires_at=str(session["expires_at"]),
        session_token=session_token,
    )


def _cookie_secure(settings: Settings) -> bool:
    if settings.public_base_url:
        return settings.public_base_url.startswith("https://")
    return settings.immich_url.startswith("https://")


def _store(settings: Annotated[Settings, Depends(get_settings)]) -> AdminStore:
    store = get_admin_store(settings.database_url)
    store.ensure_default_library(settings.immich_url)
    return store


def _grant_signing_secret(settings: Settings) -> str:
    return settings.grant_signing_secret.get_secret_value()


def _grants_from_session(session: dict[str, Any]) -> list[dict[str, Any]]:
    grants = session.get("grants")
    if isinstance(grants, list):
        cleaned = [grant for grant in grants if isinstance(grant, dict)]
        if cleaned:
            return cleaned

    principal_kind = str(session.get("principal_kind") or "")
    if principal_kind == "superadmin":
        return [superadmin_grant()]
    if principal_kind in {"immich_admin", "library_admin"}:
        return [library_admin_grant(DEFAULT_LIBRARY_ID)]
    return []


def _library_response(library: dict[str, Any]) -> LibraryResponse:
    return LibraryResponse(
        id=str(library["id"]),
        name=str(library["name"]),
        immich_url=str(library["immichUrl"]),
        public_url=library.get("publicUrl") if isinstance(library.get("publicUrl"), str) else None,
        share_hosts=[str(host) for host in library.get("shareHosts", []) if isinstance(host, str)],
        is_default=bool(library["isDefault"]),
        created_at=str(library["createdAt"]),
        updated_at=str(library["updatedAt"]),
    )


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not token:
        return None
    return token


def require_admin_session(
    store: Annotated[AdminStore, Depends(_store)],
    authorization: Annotated[str | None, Header()] = None,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> dict[str, Any]:
    """Require an active Immich-admin-backed session."""
    token = _extract_bearer(authorization) or session_cookie
    if not token:
        raise HTTPException(status_code=401, detail="Admin session required")
    session = store.get_session(_token_hash(token))
    if session is None:
        raise HTTPException(status_code=401, detail="Admin session expired")
    session["_token_hash"] = _token_hash(token)
    return session


def require_admin_context(
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> AdminContext:
    """Require an active admin session with a live Immich API key."""
    context = _optional_admin_context(session)
    if context is None:
        raise HTTPException(status_code=401, detail="Admin session requires re-login")
    return context


def optional_admin_context(
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> AdminContext | None:
    """Return live admin context when the API key is still available."""
    return _optional_admin_context(session)


def _optional_admin_context(session: dict[str, Any]) -> AdminContext | None:
    token_hash = str(session["_token_hash"])
    api_key = _get_admin_api_key(token_hash)
    if api_key is None:
        return None
    return AdminContext(session=session, api_key=api_key)


def _require_capability(
    session: dict[str, Any],
    capability: Capability,
    *,
    library_id: str | None = None,
) -> None:
    if not has_capability(_grants_from_session(session), capability, library_id=library_id):
        raise HTTPException(status_code=403, detail="Insufficient grant")


def _require_library(
    store: AdminStore,
    library_id: str,
) -> dict[str, Any]:
    library = store.get_library(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    return library


def _admin_context_for_library(
    admin: AdminContext | None,
    *,
    library: dict[str, Any],
) -> AdminContext | None:
    if admin is None:
        return None
    return AdminContext(
        session=admin.session,
        api_key=admin.api_key,
        library_id=str(library["id"]),
        library_url=str(library["immichUrl"]),
    )


def _validate_admin_key_for_libraries(
    store: AdminStore,
    settings: Settings,
    api_key: str,
) -> tuple[ImmichIdentity, list[dict[str, Any]]]:
    """Validate an Immich API key against configured libraries."""
    libraries = store.list_libraries()
    first_identity: ImmichIdentity | None = None
    saw_valid_non_admin = False
    grants: list[dict[str, Any]] = []

    for library in libraries:
        identity = validate_immich_api_key(
            str(library["immichUrl"]),
            api_key,
            timeout_seconds=settings.immich_timeout_seconds,
        )
        if identity is None:
            continue
        first_identity = first_identity or identity
        if identity.is_admin:
            grants.append(library_admin_grant(str(library["id"])))
        else:
            saw_valid_non_admin = True

    if first_identity is None:
        raise HTTPException(status_code=401, detail="Invalid Immich API key")
    if not grants:
        if saw_valid_non_admin:
            logger.warning("admin_login_rejected_non_admin", user_id=first_identity.user_id)
        raise HTTPException(status_code=403, detail="Immich admin user required")

    return first_identity, grants


def _is_superadmin_login(payload: AdminLoginRequest, settings: Settings) -> bool:
    if not settings.superadmin_username or not settings.superadmin_password:
        return False
    expected_password = settings.superadmin_password.get_secret_value()
    return secrets.compare_digest(payload.username, settings.superadmin_username) and (
        secrets.compare_digest(payload.api_key, expected_password)
    )


def _create_session_response(
    *,
    response: Response,
    store: AdminStore,
    settings: Settings,
    token: str,
    principal_id: str,
    principal_kind: str,
    user_id: str,
    email: str | None,
    name: str | None,
    api_key_name: str | None,
    grants: list[dict[str, Any]],
) -> AdminSessionResponse:
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=settings.admin_session_ttl_seconds)
    token_hash = _token_hash(token)
    store.prune_sessions()
    store.create_session(
        {
            "token_hash": token_hash,
            "principal_id": principal_id,
            "principal_kind": principal_kind,
            "user_id": user_id,
            "email": email,
            "name": name,
            "api_key_name": api_key_name,
            "grants": grants,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "last_seen_at": now.isoformat(),
        }
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.admin_session_ttl_seconds,
        httponly=True,
        secure=_cookie_secure(settings),
        samesite="lax",
        path="/",
    )
    session = {
        "token_hash": token_hash,
        "principal_id": principal_id,
        "principal_kind": principal_kind,
        "user_id": user_id,
        "email": email,
        "name": name,
        "api_key_name": api_key_name,
        "grants": grants,
        "expires_at": expires_at.isoformat(),
    }
    return _session_to_response(session, settings, session_token=token)


@router.post("/session", response_model=AdminSessionResponse)
async def create_session(
    payload: AdminLoginRequest,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
) -> AdminSessionResponse:
    """Create an admin session by validating an Immich admin API key."""
    token = secrets.token_urlsafe(32)
    if _is_superadmin_login(payload, settings):
        logger.info("superadmin_login_success", username=payload.username)
        return _create_session_response(
            response=response,
            store=store,
            settings=settings,
            token=token,
            principal_id=f"superadmin:{payload.username}",
            principal_kind="superadmin",
            user_id=f"superadmin:{payload.username}",
            email=None,
            name=payload.username,
            api_key_name=None,
            grants=[superadmin_grant()],
        )

    identity, grants = _validate_admin_key_for_libraries(store, settings, payload.api_key)

    token_hash = _token_hash(token)
    session_response = _create_session_response(
        response=response,
        store=store,
        settings=settings,
        token=token,
        principal_id=identity.user_id,
        principal_kind="immich_admin",
        user_id=identity.user_id,
        email=identity.email,
        name=identity.name,
        api_key_name=identity.api_key_name,
        grants=grants,
    )
    expires_at = datetime.fromisoformat(str(session_response.expires_at))
    _remember_admin_api_key(token_hash, payload.api_key, expires_at)
    logger.info("admin_login_success", user_id=identity.user_id)
    return session_response


@router.get("/session", response_model=AdminSessionResponse)
async def get_session(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> AdminSessionResponse:
    """Return the current admin session."""
    return _session_to_response(session, settings)


@router.delete("/session", status_code=204)
async def delete_session(
    response: Response,
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
    authorization: Annotated[str | None, Header()] = None,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> None:
    """Delete the current admin session."""
    token = _extract_bearer(authorization) or session_cookie
    if token:
        token_hash = _token_hash(token)
        store.delete_session(token_hash)
        _forget_admin_api_key(token_hash)
    response.delete_cookie(SESSION_COOKIE, path="/")
    logger.info("admin_logout", user_id=session["user_id"])


@router.get("/views", response_model=ViewsResponse)
async def list_views(
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
    admin: Annotated[AdminContext | None, Depends(optional_admin_context)],
    include_counts: Annotated[
        bool,
        Query(description="Include Immich-backed match counts for each saved view."),
    ] = True,
) -> ViewsResponse:
    """List saved DAV views for the default library."""
    return _list_views_for_library(
        settings,
        store,
        session,
        admin,
        library_id=DEFAULT_LIBRARY_ID,
        include_counts=include_counts,
    )


@router.post("/views", response_model=ViewResponse, status_code=201)
async def create_view(
    payload: ViewPayload,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
    admin: Annotated[AdminContext | None, Depends(optional_admin_context)],
) -> ViewResponse:
    """Create a saved DAV view for the default library."""
    return _create_view_for_library(
        payload,
        settings,
        store,
        session,
        admin,
        library_id=DEFAULT_LIBRARY_ID,
    )


@router.get("/views/{view_id}", response_model=ViewResponse)
async def get_view(
    view_id: str,
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> ViewResponse:
    """Return one saved DAV view for the default library."""
    return _get_view_for_library(store, session, library_id=DEFAULT_LIBRARY_ID, view_id=view_id)


@router.put("/views/{view_id}", response_model=ViewResponse)
async def update_view(
    view_id: str,
    payload: ViewPayload,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
    admin: Annotated[AdminContext | None, Depends(optional_admin_context)],
) -> ViewResponse:
    """Update a saved DAV view for the default library."""
    return _update_view_for_library(
        view_id,
        payload,
        settings,
        store,
        session,
        admin,
        library_id=DEFAULT_LIBRARY_ID,
    )


@router.delete("/views/{view_id}", status_code=204)
async def delete_view(
    view_id: str,
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> None:
    """Delete a saved DAV view for the default library."""
    _delete_view_for_library(store, session, library_id=DEFAULT_LIBRARY_ID, view_id=view_id)


@router.post("/views/match-count", response_model=MatchCountResponse)
async def match_count(
    payload: MatchCountRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
    admin: Annotated[AdminContext, Depends(require_admin_context)],
) -> MatchCountResponse:
    """Return the number of Immich assets matching a saved-view filter."""
    library = _require_library_capability(
        store,
        session,
        DEFAULT_LIBRARY_ID,
        "manage_views",
    )
    return MatchCountResponse(
        count=_count_matching_assets(
            settings,
            _admin_context_for_library(admin, library=library) or admin,
            payload.filters.model_dump(),
        )
    )


@router.get("/options/tags", response_model=OptionsResponse)
async def list_tag_options(
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
    admin: Annotated[AdminContext, Depends(require_admin_context)],
) -> OptionsResponse:
    """Return Immich tags for view configuration."""
    library = _require_library_capability(store, session, DEFAULT_LIBRARY_ID, "manage_views")
    return _tag_options_for_library(settings, admin, library)


@router.get("/options/people", response_model=OptionsResponse)
async def list_people_options(
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
    admin: Annotated[AdminContext, Depends(require_admin_context)],
) -> OptionsResponse:
    """Return Immich people for view configuration."""
    library = _require_library_capability(store, session, DEFAULT_LIBRARY_ID, "manage_views")
    return _people_options_for_library(settings, admin, library)


@router.get("/mount", response_model=MountSettings)
async def get_mount_settings(
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> MountSettings:
    """Return mount layout settings."""
    _require_library_capability(store, session, DEFAULT_LIBRARY_ID, "manage_policy")
    return _mount_settings(store, library_id=DEFAULT_LIBRARY_ID)


@router.put("/mount", response_model=MountSettings)
async def update_mount_settings(
    payload: MountSettings,
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> MountSettings:
    """Update mount layout settings."""
    _require_library_capability(store, session, DEFAULT_LIBRARY_ID, "manage_policy")
    store.set_library_setting(DEFAULT_LIBRARY_ID, "mount", _to_camel(payload.model_dump()))
    return payload


@router.get("/write-policy", response_model=WritePolicy)
async def get_write_policy(
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> WritePolicy:
    """Return WebDAV write policy settings."""
    _require_library_capability(store, session, DEFAULT_LIBRARY_ID, "manage_policy")
    return _write_policy(store, library_id=DEFAULT_LIBRARY_ID)


@router.put("/write-policy", response_model=WritePolicy)
async def update_write_policy(
    payload: WritePolicy,
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> WritePolicy:
    """Update WebDAV write policy settings."""
    if payload.permanent_delete:
        raise HTTPException(status_code=400, detail="Permanent deletion is not implemented")
    _require_library_capability(store, session, DEFAULT_LIBRARY_ID, "manage_policy")
    store.set_library_setting(DEFAULT_LIBRARY_ID, "write_policy", _to_camel(payload.model_dump()))
    return payload


@router.get("/diagnostics", response_model=DiagnosticsResponse)
async def get_diagnostics(
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> DiagnosticsResponse:
    """Return admin diagnostics and high-level configuration."""
    library = _require_library_capability(
        store,
        session,
        DEFAULT_LIBRARY_ID,
        "diagnostics",
    )
    return _diagnostics_response(settings, store, library)


@router.get("/libraries", response_model=LibrariesResponse)
async def list_libraries(
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> LibrariesResponse:
    """List libraries visible to the current principal."""
    grants = _grants_from_session(session)
    libraries = [
        library
        for library in store.list_libraries()
        if has_capability(grants, "manage_library", library_id=str(library["id"]))
        or has_capability(grants, "manage_instance")
    ]
    return LibrariesResponse(libraries=[_library_response(library) for library in libraries])


@router.post("/libraries", response_model=LibraryResponse, status_code=201)
async def create_library(
    payload: LibraryPayload,
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> LibraryResponse:
    """Create a configured Immich library."""
    _require_capability(session, "manage_instance")
    library_id = payload.id or str(uuid4())
    return _library_response(
        store.upsert_library(
            {
                "id": library_id,
                "name": payload.name.strip(),
                "immich_url": payload.immich_url.strip().rstrip("/"),
                "public_url": payload.public_url,
                "share_hosts": payload.share_hosts,
                "is_default": payload.is_default,
            }
        )
    )


@router.put("/libraries/{library_id}", response_model=LibraryResponse)
async def update_library(
    library_id: str,
    payload: LibraryPayload,
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> LibraryResponse:
    """Update a configured Immich library."""
    _require_capability(session, "manage_instance")
    _require_library(store, library_id)
    return _library_response(
        store.upsert_library(
            {
                "id": library_id,
                "name": payload.name.strip(),
                "immich_url": payload.immich_url.strip().rstrip("/"),
                "public_url": payload.public_url,
                "share_hosts": payload.share_hosts,
                "is_default": payload.is_default,
            }
        )
    )


@router.get("/libraries/{library_id}/views", response_model=ViewsResponse)
async def list_library_views(
    library_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
    admin: Annotated[AdminContext | None, Depends(optional_admin_context)],
    include_counts: Annotated[
        bool,
        Query(description="Include Immich-backed match counts for each saved view."),
    ] = True,
) -> ViewsResponse:
    """List saved DAV views for one library."""
    return _list_views_for_library(
        settings,
        store,
        session,
        admin,
        library_id=library_id,
        include_counts=include_counts,
    )


@router.post("/libraries/{library_id}/views", response_model=ViewResponse, status_code=201)
async def create_library_view(
    library_id: str,
    payload: ViewPayload,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
    admin: Annotated[AdminContext | None, Depends(optional_admin_context)],
) -> ViewResponse:
    """Create a saved DAV view for one library."""
    return _create_view_for_library(
        payload,
        settings,
        store,
        session,
        admin,
        library_id=library_id,
    )


@router.get("/libraries/{library_id}/views/{view_id}", response_model=ViewResponse)
async def get_library_view(
    library_id: str,
    view_id: str,
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> ViewResponse:
    """Return one saved DAV view for one library."""
    return _get_view_for_library(store, session, library_id=library_id, view_id=view_id)


@router.put("/libraries/{library_id}/views/{view_id}", response_model=ViewResponse)
async def update_library_view(
    library_id: str,
    view_id: str,
    payload: ViewPayload,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
    admin: Annotated[AdminContext | None, Depends(optional_admin_context)],
) -> ViewResponse:
    """Update a saved DAV view for one library."""
    return _update_view_for_library(
        view_id,
        payload,
        settings,
        store,
        session,
        admin,
        library_id=library_id,
    )


@router.delete("/libraries/{library_id}/views/{view_id}", status_code=204)
async def delete_library_view(
    library_id: str,
    view_id: str,
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> None:
    """Delete one saved DAV view for one library."""
    _delete_view_for_library(store, session, library_id=library_id, view_id=view_id)


@router.post("/libraries/{library_id}/views/match-count", response_model=MatchCountResponse)
async def library_match_count(
    library_id: str,
    payload: MatchCountRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
    admin: Annotated[AdminContext, Depends(require_admin_context)],
) -> MatchCountResponse:
    """Return the number of Immich assets matching a saved-view filter for one library."""
    library = _require_library_capability(store, session, library_id, "manage_views")
    return MatchCountResponse(
        count=_count_matching_assets(
            settings,
            _admin_context_for_library(admin, library=library) or admin,
            payload.filters.model_dump(),
        )
    )


@router.get("/libraries/{library_id}/options/tags", response_model=OptionsResponse)
async def list_library_tag_options(
    library_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
    admin: Annotated[AdminContext, Depends(require_admin_context)],
) -> OptionsResponse:
    """Return Immich tags for one library."""
    library = _require_library_capability(store, session, library_id, "manage_views")
    return _tag_options_for_library(settings, admin, library)


@router.get("/libraries/{library_id}/options/people", response_model=OptionsResponse)
async def list_library_people_options(
    library_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
    admin: Annotated[AdminContext, Depends(require_admin_context)],
) -> OptionsResponse:
    """Return Immich people for one library."""
    library = _require_library_capability(store, session, library_id, "manage_views")
    return _people_options_for_library(settings, admin, library)


@router.get("/libraries/{library_id}/mount", response_model=MountSettings)
async def get_library_mount_settings(
    library_id: str,
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> MountSettings:
    """Return mount layout settings for one library."""
    _require_library_capability(store, session, library_id, "manage_policy")
    return _mount_settings(store, library_id=library_id)


@router.put("/libraries/{library_id}/mount", response_model=MountSettings)
async def update_library_mount_settings(
    library_id: str,
    payload: MountSettings,
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> MountSettings:
    """Update mount layout settings for one library."""
    _require_library_capability(store, session, library_id, "manage_policy")
    store.set_library_setting(library_id, "mount", _to_camel(payload.model_dump()))
    return payload


@router.get("/libraries/{library_id}/write-policy", response_model=WritePolicy)
async def get_library_write_policy(
    library_id: str,
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> WritePolicy:
    """Return WebDAV write policy settings for one library."""
    _require_library_capability(store, session, library_id, "manage_policy")
    return _write_policy(store, library_id=library_id)


@router.put("/libraries/{library_id}/write-policy", response_model=WritePolicy)
async def update_library_write_policy(
    library_id: str,
    payload: WritePolicy,
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> WritePolicy:
    """Update WebDAV write policy settings for one library."""
    if payload.permanent_delete:
        raise HTTPException(status_code=400, detail="Permanent deletion is not implemented")
    _require_library_capability(store, session, library_id, "manage_policy")
    store.set_library_setting(library_id, "write_policy", _to_camel(payload.model_dump()))
    return payload


@router.get("/libraries/{library_id}/diagnostics", response_model=DiagnosticsResponse)
async def get_library_diagnostics(
    library_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> DiagnosticsResponse:
    """Return diagnostics for one library."""
    library = _require_library_capability(store, session, library_id, "diagnostics")
    return _diagnostics_response(settings, store, library)


@router.get("/events")
async def events(
    _session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> StreamingResponse:
    """Stream lightweight admin events for realtime UI status."""

    async def stream() -> Any:
        yield 'event: ready\ndata: {"status":"connected"}\n\n'
        while True:
            await asyncio.sleep(15)
            yield 'event: heartbeat\ndata: {"status":"ok"}\n\n'

    return StreamingResponse(stream(), media_type="text/event-stream")


def _require_library_capability(
    store: AdminStore,
    session: dict[str, Any],
    library_id: str,
    capability: Capability,
) -> dict[str, Any]:
    library = _require_library(store, library_id)
    _require_capability(session, capability, library_id=library_id)
    return library


def _list_views_for_library(
    settings: Settings,
    store: AdminStore,
    session: dict[str, Any],
    admin: AdminContext | None,
    *,
    library_id: str,
    include_counts: bool,
) -> ViewsResponse:
    library = _require_library_capability(store, session, library_id, "manage_views")
    library_admin = _admin_context_for_library(admin, library=library)
    return ViewsResponse(
        views=[
            _view_response(
                view,
                match_count=(
                    _optional_count_matching_assets(
                        settings,
                        library_admin,
                        view.get("filters") or {},
                    )
                    if include_counts
                    else None
                ),
            )
            for view in store.list_views(library_id=library_id)
        ]
    )


def _create_view_for_library(
    payload: ViewPayload,
    settings: Settings,
    store: AdminStore,
    session: dict[str, Any],
    admin: AdminContext | None,
    *,
    library_id: str,
) -> ViewResponse:
    library = _require_library_capability(store, session, library_id, "manage_views")
    view = payload.model_dump()
    view["id"] = str(uuid4())
    stored = store.upsert_view(view, library_id=library_id)
    return _view_response(
        stored,
        match_count=_optional_count_matching_assets(
            settings,
            _admin_context_for_library(admin, library=library),
            stored.get("filters") or {},
        ),
    )


def _get_view_for_library(
    store: AdminStore,
    session: dict[str, Any],
    *,
    library_id: str,
    view_id: str,
) -> ViewResponse:
    _require_library_capability(store, session, library_id, "manage_views")
    view = store.get_view(view_id, library_id=library_id)
    if view is None:
        raise HTTPException(status_code=404, detail="View not found")
    return _view_response(view)


def _update_view_for_library(
    view_id: str,
    payload: ViewPayload,
    settings: Settings,
    store: AdminStore,
    session: dict[str, Any],
    admin: AdminContext | None,
    *,
    library_id: str,
) -> ViewResponse:
    library = _require_library_capability(store, session, library_id, "manage_views")
    if store.get_view(view_id, library_id=library_id) is None:
        raise HTTPException(status_code=404, detail="View not found")
    view = payload.model_dump()
    view["id"] = view_id
    stored = store.upsert_view(view, library_id=library_id)
    return _view_response(
        stored,
        match_count=_optional_count_matching_assets(
            settings,
            _admin_context_for_library(admin, library=library),
            stored.get("filters") or {},
        ),
    )


def _delete_view_for_library(
    store: AdminStore,
    session: dict[str, Any],
    *,
    library_id: str,
    view_id: str,
) -> None:
    _require_library_capability(store, session, library_id, "manage_views")
    if not store.delete_view(view_id, library_id=library_id):
        raise HTTPException(status_code=404, detail="View not found")


def _tag_options_for_library(
    settings: Settings,
    admin: AdminContext,
    library: dict[str, Any],
) -> OptionsResponse:
    client = _admin_immich_client(
        settings,
        _admin_context_for_library(admin, library=library) or admin,
    )
    try:
        return OptionsResponse(items=[_tag_option(tag) for tag in client.list_tags()])
    finally:
        client.close()


def _people_options_for_library(
    settings: Settings,
    admin: AdminContext,
    library: dict[str, Any],
) -> OptionsResponse:
    client = _admin_immich_client(
        settings,
        _admin_context_for_library(admin, library=library) or admin,
    )
    try:
        return OptionsResponse(items=[_person_option(person) for person in client.list_people()])
    finally:
        client.close()


def _diagnostics_response(
    settings: Settings,
    store: AdminStore,
    library: dict[str, Any],
) -> DiagnosticsResponse:
    library_id = str(library["id"])
    return DiagnosticsResponse(
        immich_url=str(library["immichUrl"]),
        database_path=str(store.database_path),
        redis_enabled=bool(settings.redis_host),
        metrics_enabled=settings.immich_bridge_metrics,
        webdav_port=settings.webdav_port,
        admin_port=settings.admin_port,
        view_count=len(store.list_views(library_id=library_id)),
        mount=_mount_settings(store, library_id=library_id),
        write_policy=_write_policy(store, library_id=library_id),
    )


def _view_response(view: dict[str, Any], *, match_count: int | None = None) -> ViewResponse:
    return ViewResponse(
        id=str(view["id"]),
        name=str(view["name"]),
        description=str(view.get("description") or ""),
        enabled=bool(view.get("enabled", True)),
        layout=str(view.get("layout") or "date_buckets"),  # type: ignore[arg-type]
        filters=ViewFilters.model_validate(view.get("filters") or {}),
        created_at=str(view.get("createdAt") or ""),
        updated_at=str(view.get("updatedAt") or ""),
        match_count=match_count,
    )


def _admin_immich_client(settings: Settings, admin: AdminContext) -> ImmichClient:
    return ImmichClient(
        base_url=admin.library_url or settings.immich_url,
        api_key=admin.api_key,
        user_scope=str(admin.session["user_id"]),
        timeout_seconds=settings.immich_timeout_seconds,
        search_cache_ttl_seconds=settings.search_cache_ttl_seconds,
    )


def _optional_count_matching_assets(
    settings: Settings,
    admin: AdminContext | None,
    filters: dict[str, Any],
) -> int | None:
    if admin is None:
        return None
    return _count_matching_assets(settings, admin, filters)


def _count_matching_assets(
    settings: Settings,
    admin: AdminContext,
    filters: dict[str, Any],
) -> int | None:
    client = _admin_immich_client(settings, admin)
    try:
        search_kwargs = _search_kwargs_from_filters(filters)
        try:
            return client.count_assets(**search_kwargs)
        except ImmichApiError as e:
            logger.warning(
                "admin_view_match_count_statistics_failed_using_paged_fallback",
                error=str(e),
                status_code=e.status_code,
            )

        count = 0
        page_number = 1
        next_page: str | None = None
        while page_number <= settings.search_max_pages:
            page = client.search_assets(
                page=page_number,
                size=settings.search_page_size,
                with_exif=False,
                **search_kwargs,
            )
            count += len(page.items)
            next_page = page.next_page
            if not next_page:
                return count

            try:
                page_number = int(next_page)
            except ValueError:
                logger.warning("admin_view_match_count_invalid_next_page", next_page=next_page)
                return None

        logger.warning(
            "admin_view_match_count_truncated",
            search_max_pages=settings.search_max_pages,
            search_page_size=settings.search_page_size,
            next_page=next_page,
        )
        return None
    except ImmichApiError as e:
        logger.warning("admin_view_match_count_failed", error=str(e), status_code=e.status_code)
        return None
    finally:
        client.close()


def _search_kwargs_from_filters(filters: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "album_ids",
        "person_ids",
        "tag_ids",
        "is_favorite",
        "media_type",
        "taken_after",
        "taken_before",
        "rating",
        "query",
        "original_file_name",
        "ocr",
        "city",
        "state",
        "country",
    }
    return {
        key: value
        for key, value in filters.items()
        if key in allowed and value is not None and value != "" and value != []
    }


def _tag_option(tag: dict[str, Any]) -> OptionItem:
    return OptionItem(
        id=str(tag.get("id") or ""),
        name=str(tag.get("name") or tag.get("value") or "Untitled tag"),
        color=tag.get("color") if isinstance(tag.get("color"), str) else None,
        asset_count=_optional_int(tag.get("assetCount")),
    )


def _person_option(person: dict[str, Any]) -> OptionItem:
    return OptionItem(
        id=str(person.get("id") or ""),
        name=str(person.get("name") or person.get("birthName") or "Unnamed person"),
        asset_count=_optional_int(person.get("assetCount")),
        hidden=person.get("isHidden") if isinstance(person.get("isHidden"), bool) else None,
    )


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mount_settings(store: AdminStore, *, library_id: str = DEFAULT_LIBRARY_ID) -> MountSettings:
    data = {**DEFAULT_MOUNT_SETTINGS, **store.get_library_setting(library_id, "mount")}
    return MountSettings.model_validate(_to_snake(data))


def _write_policy(store: AdminStore, *, library_id: str = DEFAULT_LIBRARY_ID) -> WritePolicy:
    data = {**DEFAULT_WRITE_POLICY, **store.get_library_setting(library_id, "write_policy")}
    return WritePolicy.model_validate(_to_snake(data))


def _to_snake(data: dict[str, Any]) -> dict[str, Any]:
    return {_snake(key): value for key, value in data.items()}


def _to_camel(data: dict[str, Any]) -> dict[str, Any]:
    return {_camel(key): value for key, value in data.items()}


def _snake(value: str) -> str:
    chars: list[str] = []
    for char in value:
        if char.isupper():
            chars.append("_")
            chars.append(char.lower())
        else:
            chars.append(char)
    return "".join(chars).lstrip("_")


def _camel(value: str) -> str:
    head, *tail = value.split("_")
    return f"{head}{''.join(part.capitalize() for part in tail)}"
