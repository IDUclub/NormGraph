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

import structlog
from langextract.core import types as core_types
from langextract.core.base_model import BaseLanguageModel
from langextract.resolver import Resolver, ResolverParsingError

from src.providers.base import LLMProvider

log = structlog.get_logger(__name__)


class InvalidExtractionOutput(ValueError):
    """The model failed to return parseable extraction data after bounded retries."""


class ProviderLanguageModel(BaseLanguageModel):
    """Adapts an ``LLMProvider`` to langextract's ``BaseLanguageModel`` interface."""

    def __init__(
        self,
        llm: LLMProvider,
        *,
        model_id: str,
        temperature: float = 0.0,
        output_attempts: int = 3,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._llm = llm
        self.model_id = model_id
        self._temperature = temperature
        self._output_attempts = max(1, output_attempts)

    def infer(
        self, batch_prompts: Sequence[str], **kwargs
    ) -> Iterator[Sequence[core_types.ScoredOutput]]:
        temperature = kwargs.get("temperature", self._temperature)
        resolver = Resolver()
        for prompt in batch_prompts:
            for attempt in range(1, self._output_attempts + 1):
                text = self._llm.complete_sync(
                    prompt,
                    temperature=temperature,
                    system=(
                        "Return only JSON with an extractions array in the format shown "
                        "in the examples. Do not include reasoning. Return "
                        '{"extractions": []} only when the text contains no restrictions.'
                        if attempt > 1
                        else None
                    ),
                )
                try:
                    resolver.resolve(text, suppress_parse_errors=False)
                except ResolverParsingError as exc:
                    # Never log the raw response or prompt: documents can be private.
                    log.warning(
                        "extraction_output_invalid",
                        attempt=attempt,
                        response_chars=len(text),
                        error_type=type(exc).__name__,
                    )
                    if attempt == self._output_attempts:
                        raise InvalidExtractionOutput(
                            f"invalid_llm_output after {attempt} attempts"
                        ) from exc
                    continue
                yield [core_types.ScoredOutput(score=1.0, output=text)]
                break
