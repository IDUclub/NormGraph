"""Settings load with defaults and honour NG_-prefixed environment overrides."""

from __future__ import annotations

from src.common.config import Settings


def test_defaults():
    s = Settings()
    assert s.llm_provider == "openai_compatible"
    assert s.embeddings_provider == "openai_compatible"
    assert s.vector_size == 2048
    assert s.neo4j_uri.startswith("bolt://")


def test_env_override(monkeypatch):
    monkeypatch.setenv("NG_LLM_MODEL", "custom-model")
    monkeypatch.setenv("NG_VECTOR_SIZE", "1024")
    monkeypatch.setenv("NG_EMBEDDINGS_PROVIDER", "ollama")
    s = Settings()
    assert s.llm_model == "custom-model"
    assert s.vector_size == 1024
    assert s.embeddings_provider == "ollama"
