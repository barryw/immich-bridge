.PHONY: help install lint typecheck test ci build up down

help:
	@echo "immich-bridge development commands"
	@echo ""
	@echo "Development:"
	@echo "  make install     - Install dependencies"
	@echo "  make lint        - Run linters"
	@echo "  make typecheck   - Run type checker"
	@echo "  make test        - Run tests"
	@echo ""
	@echo "Docker:"
	@echo "  make build       - Build Docker image"
	@echo "  make up          - Start local Compose stack"
	@echo "  make down        - Stop local Compose stack"

IMAGE_NAME := immich-bridge

install:
	pip install uv
	uv sync

lint:
	uv run --extra dev ruff check src/ tests/
	uv run --extra dev ruff format --check src/ tests/

typecheck:
	uv run --extra dev mypy src/

test:
	IMMICH_URL=http://immich.test/api uv run --extra dev pytest --tb=short -q

ci: lint typecheck test

build:
	docker build -t $(IMAGE_NAME):latest .

up:
	docker compose up --build

down:
	docker compose down
