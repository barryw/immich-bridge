"""FastAPI application factory."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from immich_bridge.api.health import router as health_router
from immich_bridge.cache import init_cache
from immich_bridge.config import get_settings
from immich_bridge.logging import get_logger, setup_logging

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler for startup/shutdown."""
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)

    redis_password = settings.redis_password.get_secret_value() if settings.redis_password else None
    init_cache(
        redis_host=settings.redis_host,
        redis_port=settings.redis_port,
        redis_db=settings.redis_db,
        redis_password=redis_password,
    )

    logger.info("application_starting", admin_port=settings.admin_port)
    yield
    logger.info("application_stopping")


def create_app() -> FastAPI:
    """Application factory for FastAPI."""
    app = FastAPI(
        title="Immich Bridge",
        description="Bridge service for Immich libraries",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)

    return app


app = ProxyHeadersMiddleware(create_app(), trusted_hosts=["*"])  # type: ignore[arg-type]
