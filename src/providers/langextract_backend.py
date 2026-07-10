"""A langextract language-model backend that delegates to our ``LLMProvider``.

langextract drives the extraction (prompt assembly, example formatting, output parsing and source
grounding); this adapter is the thin seam that lets it call *our* provider abstraction instead of
its built-in OpenAI/Ollama clients. That keeps a single, provider-agnostic HTTP path: whatever
``NG_LLM_PROVIDER`` points at (OpenAI-compatible or Ollama) is exactly what does the extraction.

The contract is small: ``infer`` receives a batch of fully-assembled prompts and returns, for each,
one ``ScoredOutput`` with the model's raw text. langextract's resolver parses the fenced JSON out of
that text (``fence_output=True``), so the provider only has to complete the prompt.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from langextract.core import types as core_types
from langextract.core.base_model import BaseLanguageModel

from src.providers.base import LLMProvider


class ProviderLanguageModel(BaseLanguageModel):
    """Adapts an ``LLMProvider`` to langextract's ``BaseLanguageModel`` interface."""

    def __init__(
        self,
        llm: LLMProvider,
        *,
        model_id: str,
        temperature: float = 0.0,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._llm = llm
        self.model_id = model_id
        self._temperature = temperature

    def infer(
        self, batch_prompts: Sequence[str], **kwargs
    ) -> Iterator[Sequence[core_types.ScoredOutput]]:
        temperature = kwargs.get("temperature", self._temperature)
        for prompt in batch_prompts:
            text = self._llm.complete_sync(prompt, temperature=temperature)
            yield [core_types.ScoredOutput(score=1.0, output=text)]
