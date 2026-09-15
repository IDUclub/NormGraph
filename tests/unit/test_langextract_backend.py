"""The langextract backend delegates inference to the LLMProvider."""

from __future__ import annotations

import pytest
from langextract.resolver import ResolverParsingError

from src.pipeline.extractor import RestrictionExtractor
from src.providers.base import LLMProvider
from src.providers.langextract_backend import ProviderLanguageModel


class FakeLLM(LLMProvider):
    def __init__(self) -> None:
        self.model = "fake"
        self.calls: list[str] = []

    async def complete(self, prompt, **kwargs):  # pragma: no cover - unused here
        return "async"

    def complete_sync(self, prompt, **kwargs):
        self.calls.append(prompt)
        return f"out:{prompt}"


def test_infer_yields_one_scored_output_per_prompt():
    llm = FakeLLM()
    model = ProviderLanguageModel(llm, model_id="m")
    results = list(model.infer(["p1", "p2"]))
    assert [r[0].output for r in results] == ["out:p1", "out:p2"]
    assert all(r[0].score == 1.0 for r in results)
    assert llm.calls == ["p1", "p2"]


def test_unparseable_model_answer_does_not_become_zero_restrictions():
    llm = FakeLLM()
    llm.complete_sync = lambda *args, **kwargs: "Пришлите текст документа"
    model = ProviderLanguageModel(llm, model_id="m")
    with pytest.raises(ResolverParsingError):
        RestrictionExtractor(model).extract_clause_sync("Расстояние не менее 10 м.")


def test_valid_empty_extraction_is_allowed():
    llm = FakeLLM()
    llm.complete_sync = lambda *args, **kwargs: '```json\n{"extractions": []}\n```'
    model = ProviderLanguageModel(llm, model_id="m")
    assert RestrictionExtractor(model).extract_clause_sync("Предисловие") == []
