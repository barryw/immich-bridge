# Repository Guidelines

## Project Structure & Module Organization

Application code lives in `src/immich_bridge/`. The main WebDAV surface is split across
`webdav_server.py`, `webdav_provider.py`, `webdav_auth.py`, `immich_client.py`,
`fs_service.py`, `fs_model.py`, `cache.py`, and `blob_cache.py`. FastAPI admin/API
startup lives in `app.py`, `main.py`, and `api/`.

Tests live in `tests/` and mirror the major modules. Deployment assets are at the repo
root: `Dockerfile`, `docker-compose.yml`, `.env.example`, and `scripts/entrypoint.py`.
`DESIGN.md` contains product and architecture notes. Native filesystem client planning
lives in `macos/` and `windows/`; shared client/backend contracts live in `docs/`.

## Build, Test, and Development Commands

- `uv sync` installs runtime and dev dependencies.
- `make test` runs the pytest suite with a test Immich URL.
- `make lint` runs Ruff linting and format checks.
- `make typecheck` runs mypy against `src/`.
- `make ci` runs lint, typecheck, and tests.
- `docker compose up --build` starts the app plus Redis.

## Coding Style & Naming Conventions

Target Python 3.12+. Use 4-space indentation, type hints for public functions, and
`snake_case` for modules, functions, and variables. Use `PascalCase` for classes and
`UPPER_SNAKE_CASE` for constants. Keep Immich API access behind `ImmichClient`; keep
path resolution, auth, caching, locking, and write policy in separate modules.
Native clients should consume a platform-neutral `/api/fs/v1` contract instead of
duplicating Immich layout logic in Swift or Windows code.

Ruff is the formatter and linter. Run `make lint` before publishing changes.

## Testing Guidelines

Use `pytest`. Name test files `test_*.py` and prefer behavior-focused test names.
Add tests for DAV client behavior, path resolution, pagination boundaries, Range
streaming, cache behavior, authentication, locking, and destructive-operation safety.

## Commit & Pull Request Guidelines

Use short imperative commit subjects. Conventional Commit prefixes such as `feat:`,
`fix:`, `docs:`, and `test:` are preferred.

Pull requests should include a concise description, linked issue or design section when
relevant, test results, and screenshots for UI changes. Call out any change affecting
auth, deletion semantics, token handling, caching, or DAV client compatibility.

## Security & Configuration Tips

Never commit Immich API keys, `.env` files, Redis passwords, local databases, or cache
volumes. Default examples should remain conservative and require explicit configuration
before enabling writes or permanent deletion.
