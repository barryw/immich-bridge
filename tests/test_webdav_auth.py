"""Tests for WebDAV HTTP Basic authentication."""

from unittest.mock import MagicMock, patch

import pytest

from immich_bridge.cache import get_cache
from immich_bridge.webdav_auth import ImmichBasicAuthenticator


@pytest.fixture(autouse=True)
def clear_auth_cache() -> None:
    """Keep auth cache state isolated between tests."""
    get_cache().clear()


def test_basic_auth_stores_identity_in_environ() -> None:
    """Successful auth should put Immich identity and API key in request environ."""
    auth = ImmichBasicAuthenticator("http://immich.test/api")

    user_response = MagicMock()
    user_response.status_code = 200
    user_response.json.return_value = {
        "id": "user-1",
        "email": "barry@example.com",
        "name": "Barry",
    }
    key_response = MagicMock()
    key_response.status_code = 200
    key_response.json.return_value = {"name": "webdav"}

    client = MagicMock()
    client.get.side_effect = [user_response, key_response]

    with patch("immich_bridge.webdav_auth.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value = client
        environ: dict[str, object] = {}

        result = auth.basic_auth_user("realm", "barry", "api-key", environ)

    assert result == "barry"
    assert environ["immich.user_id"] == "user-1"
    assert environ["immich.email"] == "barry@example.com"
    assert environ["immich.api_key_name"] == "webdav"
    assert environ["immich.api_key"] == "api-key"


def test_basic_auth_returns_false_on_invalid_key() -> None:
    """Invalid Immich API keys should fail Basic auth."""
    auth = ImmichBasicAuthenticator("http://immich.test/api")

    user_response = MagicMock()
    user_response.status_code = 401

    client = MagicMock()
    client.get.return_value = user_response

    with patch("immich_bridge.webdav_auth.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value = client
        result = auth.basic_auth_user("realm", "barry", "bad-key", {})

    assert result is False


def test_basic_auth_uses_shared_cache_backend() -> None:
    """Successful auth validation should be shared through the cache backend."""
    first_auth = ImmichBasicAuthenticator("http://immich.test/api")
    second_auth = ImmichBasicAuthenticator("http://immich.test/api")

    user_response = MagicMock()
    user_response.status_code = 200
    user_response.json.return_value = {
        "id": "user-1",
        "email": "barry@example.com",
        "name": "Barry",
    }
    key_response = MagicMock()
    key_response.status_code = 200
    key_response.json.return_value = {"name": "webdav"}

    client = MagicMock()
    client.get.side_effect = [user_response, key_response]

    with patch("immich_bridge.webdav_auth.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value = client

        first_result = first_auth.basic_auth_user("realm", "barry", "api-key", {})
        second_environ: dict[str, object] = {}
        second_result = second_auth.basic_auth_user(
            "realm",
            "barry",
            "api-key",
            second_environ,
        )

    assert first_result == "barry"
    assert second_result == "barry"
    assert second_environ["immich.user_id"] == "user-1"
    assert client.get.call_count == 2


def test_basic_auth_rate_limits_failed_attempts() -> None:
    """Repeated failed Basic Auth attempts should block validation briefly."""
    auth = ImmichBasicAuthenticator(
        "http://immich.test/api",
        auth_failure_limit=1,
        auth_failure_window_seconds=60,
    )

    user_response = MagicMock()
    user_response.status_code = 401

    client = MagicMock()
    client.get.return_value = user_response

    with patch("immich_bridge.webdav_auth.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value = client
        first_result = auth.basic_auth_user(
            "realm",
            "barry",
            "bad-key",
            {"REMOTE_ADDR": "127.0.0.1"},
        )
        second_result = auth.basic_auth_user(
            "realm",
            "barry",
            "good-key",
            {"REMOTE_ADDR": "127.0.0.1"},
        )

    assert first_result is False
    assert second_result is False
    assert client.get.call_count == 1
