FROM node:26-alpine AS admin-ui

WORKDIR /ui

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim

LABEL org.opencontainers.image.title="immich-bridge"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.vendor="barryw"

WORKDIR /app

RUN pip install uv

COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY --from=admin-ui /ui/dist ./src/immich_bridge/admin_static/

RUN uv sync --frozen --no-dev

EXPOSE 8080 8081

CMD ["uv", "run", "python", "scripts/entrypoint.py"]
