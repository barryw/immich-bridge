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

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from immich_bridge.admin_store import DEFAULT_MOUNT_SETTINGS, DEFAULT_WRITE_POLICY
from immich_bridge.admin_store import AdminStore, get_admin_store
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


class AdminSessionResponse(BaseModel):
    """Admin session response."""

    authenticated: bool
    user: AdminUser | None = None
    expires_at: str | None = None
    session_token: str | None = None


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


def _session_to_response(session: dict[str, Any]) -> AdminSessionResponse:
    return AdminSessionResponse(
        authenticated=True,
        user=AdminUser(
            id=str(session["user_id"]),
            email=session.get("email"),
            name=session.get("name"),
            api_key_name=session.get("api_key_name"),
        ),
        expires_at=str(session["expires_at"]),
    )


def _cookie_secure(settings: Settings) -> bool:
    return settings.immich_url.startswith("https://")


def _store(settings: Annotated[Settings, Depends(get_settings)]) -> AdminStore:
    return get_admin_store(settings.database_url)


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


@router.post("/session", response_model=AdminSessionResponse)
async def create_session(
    payload: AdminLoginRequest,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
) -> AdminSessionResponse:
    """Create an admin session by validating an Immich admin API key."""
    identity = validate_immich_api_key(
        settings.immich_url,
        payload.api_key,
        timeout_seconds=settings.immich_timeout_seconds,
    )
    if identity is None:
        raise HTTPException(status_code=401, detail="Invalid Immich API key")
    if not identity.is_admin:
        logger.warning("admin_login_rejected_non_admin", user_id=identity.user_id)
        raise HTTPException(status_code=403, detail="Immich admin user required")

    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=settings.admin_session_ttl_seconds)
    store.prune_sessions()
    token_hash = _token_hash(token)
    store.create_session(
        {
            "token_hash": token_hash,
            "user_id": identity.user_id,
            "email": identity.email,
            "name": identity.name,
            "api_key_name": identity.api_key_name,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "last_seen_at": now.isoformat(),
        }
    )
    _remember_admin_api_key(token_hash, payload.api_key, expires_at)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.admin_session_ttl_seconds,
        httponly=True,
        secure=_cookie_secure(settings),
        samesite="lax",
        path="/",
    )
    logger.info("admin_login_success", user_id=identity.user_id)
    return AdminSessionResponse(
        authenticated=True,
        user=_identity_to_user(identity),
        expires_at=expires_at.isoformat(),
        session_token=token,
    )


@router.get("/session", response_model=AdminSessionResponse)
async def get_session(
    session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> AdminSessionResponse:
    """Return the current admin session."""
    return _session_to_response(session)


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
    admin: Annotated[AdminContext | None, Depends(optional_admin_context)],
) -> ViewsResponse:
    """List saved DAV views."""
    return ViewsResponse(
        views=[
            _view_response(
                view,
                match_count=_optional_count_matching_assets(
                    settings,
                    admin,
                    view.get("filters") or {},
                ),
            )
            for view in store.list_views()
        ]
    )


@router.post("/views", response_model=ViewResponse, status_code=201)
async def create_view(
    payload: ViewPayload,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
    admin: Annotated[AdminContext | None, Depends(optional_admin_context)],
) -> ViewResponse:
    """Create a saved DAV view."""
    view = payload.model_dump()
    view["id"] = str(uuid4())
    stored = store.upsert_view(view)
    return _view_response(
        stored,
        match_count=_optional_count_matching_assets(settings, admin, stored.get("filters") or {}),
    )


@router.get("/views/{view_id}", response_model=ViewResponse)
async def get_view(
    view_id: str,
    store: Annotated[AdminStore, Depends(_store)],
    _session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> ViewResponse:
    """Return one saved DAV view."""
    view = store.get_view(view_id)
    if view is None:
        raise HTTPException(status_code=404, detail="View not found")
    return _view_response(view)


@router.put("/views/{view_id}", response_model=ViewResponse)
async def update_view(
    view_id: str,
    payload: ViewPayload,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
    admin: Annotated[AdminContext | None, Depends(optional_admin_context)],
) -> ViewResponse:
    """Update a saved DAV view."""
    if store.get_view(view_id) is None:
        raise HTTPException(status_code=404, detail="View not found")
    view = payload.model_dump()
    view["id"] = view_id
    stored = store.upsert_view(view)
    return _view_response(
        stored,
        match_count=_optional_count_matching_assets(settings, admin, stored.get("filters") or {}),
    )


@router.delete("/views/{view_id}", status_code=204)
async def delete_view(
    view_id: str,
    store: Annotated[AdminStore, Depends(_store)],
    _session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> None:
    """Delete a saved DAV view."""
    if not store.delete_view(view_id):
        raise HTTPException(status_code=404, detail="View not found")


@router.post("/views/match-count", response_model=MatchCountResponse)
async def match_count(
    payload: MatchCountRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    admin: Annotated[AdminContext, Depends(require_admin_context)],
) -> MatchCountResponse:
    """Return the number of Immich assets matching a saved-view filter."""
    return MatchCountResponse(
        count=_count_matching_assets(settings, admin, payload.filters.model_dump())
    )


@router.get("/options/tags", response_model=OptionsResponse)
async def list_tag_options(
    settings: Annotated[Settings, Depends(get_settings)],
    admin: Annotated[AdminContext, Depends(require_admin_context)],
) -> OptionsResponse:
    """Return Immich tags for view configuration."""
    client = _admin_immich_client(settings, admin)
    try:
        return OptionsResponse(items=[_tag_option(tag) for tag in client.list_tags()])
    finally:
        client.close()


@router.get("/options/people", response_model=OptionsResponse)
async def list_people_options(
    settings: Annotated[Settings, Depends(get_settings)],
    admin: Annotated[AdminContext, Depends(require_admin_context)],
) -> OptionsResponse:
    """Return Immich people for view configuration."""
    client = _admin_immich_client(settings, admin)
    try:
        return OptionsResponse(items=[_person_option(person) for person in client.list_people()])
    finally:
        client.close()


@router.get("/mount", response_model=MountSettings)
async def get_mount_settings(
    store: Annotated[AdminStore, Depends(_store)],
    _session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> MountSettings:
    """Return mount layout settings."""
    return _mount_settings(store)


@router.put("/mount", response_model=MountSettings)
async def update_mount_settings(
    payload: MountSettings,
    store: Annotated[AdminStore, Depends(_store)],
    _session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> MountSettings:
    """Update mount layout settings."""
    store.set_setting("mount", _to_camel(payload.model_dump()))
    return payload


@router.get("/write-policy", response_model=WritePolicy)
async def get_write_policy(
    store: Annotated[AdminStore, Depends(_store)],
    _session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> WritePolicy:
    """Return WebDAV write policy settings."""
    return _write_policy(store)


@router.put("/write-policy", response_model=WritePolicy)
async def update_write_policy(
    payload: WritePolicy,
    store: Annotated[AdminStore, Depends(_store)],
    _session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> WritePolicy:
    """Update WebDAV write policy settings."""
    if payload.permanent_delete:
        raise HTTPException(status_code=400, detail="Permanent deletion is not implemented")
    store.set_setting("write_policy", _to_camel(payload.model_dump()))
    return payload


@router.get("/diagnostics", response_model=DiagnosticsResponse)
async def get_diagnostics(
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(_store)],
    _session: Annotated[dict[str, Any], Depends(require_admin_session)],
) -> DiagnosticsResponse:
    """Return admin diagnostics and high-level configuration."""
    return DiagnosticsResponse(
        immich_url=settings.immich_url,
        database_path=str(store.database_path),
        redis_enabled=bool(settings.redis_host),
        metrics_enabled=settings.immich_bridge_metrics,
        webdav_port=settings.webdav_port,
        admin_port=settings.admin_port,
        view_count=len(store.list_views()),
        mount=_mount_settings(store),
        write_policy=_write_policy(store),
    )


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
        base_url=settings.immich_url,
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
        page = client.search_assets(
            page=1,
            size=1,
            with_exif=False,
            **_search_kwargs_from_filters(filters),
        )
        return page.total if page.total is not None else len(page.items)
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


def _mount_settings(store: AdminStore) -> MountSettings:
    data = {**DEFAULT_MOUNT_SETTINGS, **store.get_setting("mount")}
    return MountSettings.model_validate(_to_snake(data))


def _write_policy(store: AdminStore) -> WritePolicy:
    data = {**DEFAULT_WRITE_POLICY, **store.get_setting("write_policy")}
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
