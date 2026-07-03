"""FastAPI application factory."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from immich_bridge.admin_store import get_admin_store
from immich_bridge.api.admin import router as admin_router
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
    get_admin_store(settings.database_url)

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

    app.include_router(admin_router)
    app.include_router(health_router)
    _mount_admin_ui(app)

    return app


def _mount_admin_ui(app: FastAPI) -> None:
    """Serve the compiled admin UI when present."""
    static_dir = Path(__file__).with_name("admin_static")
    index_file = static_dir / "index.html"
    assets_dir = static_dir / "assets"
    if not index_file.exists():
        logger.info("admin_ui_static_missing", path=str(static_dir))
        return

    if assets_dir.exists():
        app.mount(
            "/admin/assets",
            StaticFiles(directory=assets_dir),
            name="admin-assets",
        )

    @app.get("/", include_in_schema=False)
    async def admin_root() -> RedirectResponse:
        return RedirectResponse("/admin")

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/{path:path}", include_in_schema=False)
    async def admin_ui(path: str = "") -> FileResponse:
        return FileResponse(index_file)


app = ProxyHeadersMiddleware(create_app(), trusted_hosts=["*"])  # type: ignore[arg-type]
