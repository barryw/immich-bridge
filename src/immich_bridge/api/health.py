"""Health check endpoints for liveness and readiness probes."""

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Response

from immich_bridge.config import Settings, get_settings
from immich_bridge.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Liveness probe endpoint."""
    return {"status": "healthy"}


async def _check_immich(settings: Settings) -> bool:
    url = f"{settings.immich_url.rstrip('/')}/server/ping"
    try:
        async with httpx.AsyncClient(timeout=settings.immich_timeout_seconds) as client:
            response = await client.get(url)
        return response.status_code < 500
    except httpx.RequestError as e:
        logger.warning("immich_readiness_failed", error=str(e))
        return False


async def _check_redis(settings: Settings) -> bool:
    if not settings.redis_host:
        return True

    try:
        import redis.asyncio as redis

        client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            password=settings.redis_password.get_secret_value()
            if settings.redis_password
            else None,
        )
        try:
            pong = await client.ping()
            return bool(pong)
        finally:
            await client.aclose()
    except Exception as e:
        logger.warning("redis_readiness_failed", error=str(e))
        return False


@router.get("/ready")
async def readiness_check(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Readiness probe endpoint."""
    checks = {
        "immich": await _check_immich(settings),
        "redis": await _check_redis(settings),
    }
    all_ready = all(checks.values())

    if not all_ready:
        response.status_code = 503
        logger.warning("readiness_check_failed", checks=checks)

    return {
        "status": "ready" if all_ready else "not_ready",
        "checks": checks,
    }
