"""HTTP Basic authentication for WebDAV using Immich API keys."""

import hashlib
from dataclasses import asdict
from typing import Any

from wsgidav.dc.base_dc import BaseDomainController  # type: ignore[import-untyped]

from immich_bridge.cache import get_cache
from immich_bridge.immich_auth import ImmichIdentity, validate_immich_api_key
from immich_bridge.logging import get_logger

logger = get_logger(__name__)


class ImmichBasicAuthenticator(BaseDomainController):  # type: ignore[misc]
    """WsgiDAV domain controller for Immich API-key Basic Auth."""

    def __init__(
        self,
        immich_url: str,
        cache_ttl_seconds: int = 300,
        timeout_seconds: float = 10.0,
        auth_failure_limit: int = 10,
        auth_failure_window_seconds: int = 300,
    ) -> None:
        """Initialize the authenticator."""
        super().__init__(None, None)
        self._immich_url = immich_url.rstrip("/")
        self._cache_ttl_seconds = cache_ttl_seconds
        self._timeout_seconds = timeout_seconds
        self._auth_failure_limit = auth_failure_limit
        self._auth_failure_window_seconds = auth_failure_window_seconds

    def get_domain_realm(self, path_info: str, environ: dict[str, Any] | None) -> str:
        """Return the authentication realm."""
        return "Immich Bridge"

    def require_authentication(self, realm: str, environ: dict[str, Any] | None) -> bool:
        """Always require authentication for WebDAV."""
        return True

    def supports_http_digest_auth(self) -> bool:
        """Only Basic auth is supported because the password is the API key."""
        return False

    def _cache_key(self, api_key: str) -> str:
        return f"auth:{hashlib.sha256(api_key.encode()).hexdigest()}"

    def _failure_key(self, username: str, environ: dict[str, Any]) -> str:
        remote_addr = str(environ.get("REMOTE_ADDR") or "unknown")
        payload = f"{remote_addr}:{username.casefold()}"
        return f"auth-fail:{hashlib.sha256(payload.encode()).hexdigest()}"

    def _is_rate_limited(self, key: str, username: str) -> bool:
        if self._auth_failure_limit <= 0:
            return False
        count = get_cache().get_int(key) or 0
        if count < self._auth_failure_limit:
            return False
        logger.warning("webdav_auth_rate_limited", username=username, count=count)
        return True

    def _record_auth_failure(self, key: str, username: str) -> None:
        if self._auth_failure_limit <= 0:
            return
        count = get_cache().incr_with_ttl(key, ttl=self._auth_failure_window_seconds)
        logger.info("webdav_auth_failure_recorded", username=username, count=count)

    def _validate_api_key(self, api_key: str) -> ImmichIdentity | None:
        """Validate an Immich API key and return identity metadata."""
        return validate_immich_api_key(
            self._immich_url,
            api_key,
            timeout_seconds=self._timeout_seconds,
        )

    def _get_cached_identity(self, api_key: str) -> ImmichIdentity | None:
        cached = get_cache().get_json(self._cache_key(api_key))
        if cached is None:
            return None

        user_id = cached.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            return None

        identity = ImmichIdentity(
            user_id=user_id,
            email=cached.get("email") if isinstance(cached.get("email"), str) else None,
            name=cached.get("name") if isinstance(cached.get("name"), str) else None,
            api_key_name=cached.get("api_key_name")
            if isinstance(cached.get("api_key_name"), str)
            else None,
            is_admin=bool(cached.get("is_admin")),
        )
        logger.debug("webdav_auth_cache_hit", user_id=identity.user_id)
        return identity

    def _set_cached_identity(self, api_key: str, identity: ImmichIdentity) -> None:
        get_cache().set_json(
            self._cache_key(api_key),
            asdict(identity),
            ttl=self._cache_ttl_seconds,
        )

    def basic_auth_user(
        self,
        realm: str,
        username: str,
        password: str,
        environ: dict[str, Any],
    ) -> bool | str:
        """Authenticate using DAV username plus Immich API key as password."""
        failure_key = self._failure_key(username, environ)
        if self._is_rate_limited(failure_key, username):
            return False

        if not password:
            logger.info("webdav_auth_failed_empty_api_key", username=username)
            self._record_auth_failure(failure_key, username)
            return False

        identity = self._get_cached_identity(password)
        if identity is None:
            identity = self._validate_api_key(password)
            if identity is None:
                logger.info("webdav_auth_failed", username=username)
                self._record_auth_failure(failure_key, username)
                return False
            self._set_cached_identity(password, identity)

        environ["immich.username"] = username
        environ["immich.user_id"] = identity.user_id
        environ["immich.email"] = identity.email
        environ["immich.api_key_name"] = identity.api_key_name
        environ["immich.api_key"] = password

        logger.info("webdav_auth_success", username=username, user_id=identity.user_id)
        return username
