.PHONY: help install up down run test test-unit test-integration test-all

help:
	@echo "Available targets:"
	@echo "  install           Install dependencies (incl. dev group) via uv"
	@echo "  up                Start Neo4j (docker compose)"
	@echo "  down              Stop the local stack"
	@echo "  run               Run the API locally (uvicorn, reload)"
	@echo "  test              Run unit tests only (fast, no external services)"
	@echo "  test-integration  Run integration tests (needs Neo4j, IDU_DVD, LLM)"
	@echo "  test-all          Run the full test suite (unit + integration)"

install:
	uv sync

up:
	docker compose up -d neo4j

down:
	docker compose down

run:
	uv run python -m src.dev_runner

# Unit tests: hermetic, mock every external boundary — safe to run anywhere.
test test-unit:
	uv run pytest tests/unit

# Integration tests: exercise the live local stack; each test self-skips if its
# service (Neo4j / IDU_DVD / LLM / embeddings) is unavailable.
test-integration:
	uv run pytest tests/integration -m integration

test-all:
	uv run pytest
