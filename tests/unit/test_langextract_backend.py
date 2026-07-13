"""The langextract backend delegates inference to the LLMProvider."""

from __future__ import annotations

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
