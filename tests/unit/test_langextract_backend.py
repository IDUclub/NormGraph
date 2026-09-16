"""Retry only malformed LLM chunks; valid empty extractions are not failures."""

from __future__ import annotations

import json

import pytest

from src.pipeline.extractor import RestrictionExtractor
from src.providers.base import LLMProvider
from src.providers.langextract_backend import (
    InvalidExtractionOutput,
    ProviderLanguageModel,
)

EMPTY = '{"extractions": []}'
VALID = json.dumps(
    {
        "extractions": [
            {
                "ограничение": "Расстояние между школой и домом не менее 50 м.",
                "ограничение_attributes": {
                    "subject": "Школа",
                    "object": "Дом",
                    "kind": "минимальное_расстояние",
                    "value_operator": ">=",
                    "value_number": "50",
                    "value_unit": "м",
                },
            }
        ]
    },
    ensure_ascii=False,
)


class FakeLLM(LLMProvider):
    def __init__(self, outputs):
        self.model = "fake"
        self.outputs = iter(outputs)
        self.calls = []

    async def complete(self, prompt, **kwargs):
        raise AssertionError("sync extraction must use complete_sync")

    def complete_sync(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return next(self.outputs)


def test_infer_yields_one_scored_output_per_prompt():
    llm = FakeLLM([VALID, EMPTY])
    model = ProviderLanguageModel(llm, model_id="m")
    results = list(model.infer(["p1", "p2"]))
    assert [r[0].output for r in results] == [VALID, EMPTY]
    assert all(r[0].score == 1.0 for r in results)
    assert [p for p, _ in llm.calls] == ["p1", "p2"]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "thinking only",
        '{"extractions": [',
        '{"extractions": [{"ограничение": []}]}',
        '{"extractions": [{"ограничение": "text", "ограничение_attributes": []}]}',
    ],
)
def test_retries_invalid_output_without_repeating_successful_prompts(bad):
    llm = FakeLLM([VALID, bad, VALID])
    model = ProviderLanguageModel(llm, model_id="m")
    results = list(model.infer(["first", "second"]))
    assert len(results) == 2
    assert [p for p, _ in llm.calls] == ["first", "second", "second"]
    assert llm.calls[-1][1]["system"]


def test_exhausted_retries_raise_instead_of_becoming_an_empty_extraction():
    llm = FakeLLM(["", "not JSON", ""])
    with pytest.raises(InvalidExtractionOutput, match="after 3 attempts"):
        list(ProviderLanguageModel(llm, model_id="m").infer(["p"]))
    assert len(llm.calls) == 3


def test_real_langextract_recovers_and_grounds_output():
    llm = FakeLLM(["", "not JSON", VALID])
    extractor = RestrictionExtractor(ProviderLanguageModel(llm, model_id="m"))
    norms = extractor.extract_clause_sync(
        "Расстояние между школой и домом не менее 50 м."
    )
    assert len(norms) == 1
    assert (norms[0].subject, norms[0].object, norms[0].value.number) == (
        "Школа",
        "Дом",
        50,
    )
    assert norms[0].char_start == 0
    assert len(llm.calls) == 3


def test_real_langextract_does_not_swallow_exhaustion():
    llm = FakeLLM([""] * 3)
    extractor = RestrictionExtractor(ProviderLanguageModel(llm, model_id="m"))
    with pytest.raises(InvalidExtractionOutput):
        extractor.extract_clause_sync("Расстояние между школой и домом не менее 50 м.")


@pytest.mark.parametrize(
    "output", [EMPTY, f"```json\n{EMPTY}\n```", f"```json\n{VALID}\n```"]
)
def test_valid_empty_or_fenced_response_does_not_retry(output):
    llm = FakeLLM([output])
    assert len(list(ProviderLanguageModel(llm, model_id="m").infer(["p"]))) == 1
    assert len(llm.calls) == 1
