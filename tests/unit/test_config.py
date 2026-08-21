"""Settings load with defaults and honour NG_-prefixed environment overrides."""

from __future__ import annotations

import pytest

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


def test_remote_native_ollama_is_rejected_for_llm():
    with pytest.raises(ValueError, match="must point to local Ollama"):
        Settings(llm_provider="ollama", ollama_base="http://a.dgx:11434")


def test_a_dgx_is_rejected_as_openai_compatible_llm():
    with pytest.raises(ValueError, match="must not target 'a.dgx'"):
        Settings(
            llm_provider="openai_compatible",
            llm_base_url="http://a.dgx:11434/v1",
        )


def test_remote_embeddings_on_a_dgx_remain_allowed():
    settings = Settings(embeddings_url="http://a.dgx:8010")

    assert settings.embeddings_url == "http://a.dgx:8010"
