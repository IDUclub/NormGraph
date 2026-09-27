"""Native Ollama chat provider (``/api/chat``).

An alternative to the OpenAI-compatible provider for deployments that talk to Ollama directly.
``base_url`` is the Ollama root (e.g. ``http://localhost:11434``), without ``/v1``. An answer cut
at ``num_predict`` is requested again with a doubled window, up to ``max_tokens_limit``.
"""

from __future__ import annotations

import httpx
import structlog

from src.providers.base import LLMProvider, next_output_window

log = structlog.get_logger(__name__)


class OllamaLLM(LLMProvider):
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        max_tokens_limit: int | None = None,
        timeout: float = 600.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_tokens_limit = max(max_tokens_limit or 0, max_tokens)
        self._timeout = timeout
        self._async: httpx.AsyncClient | None = None
        self._sync: httpx.Client | None = None

    def _payload(
        self,
        prompt: str,
        system: str | None,
        temperature: float | None,
        max_tokens: int,
    ) -> dict:
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": (
                    self._temperature if temperature is None else temperature
                ),
                "num_predict": max_tokens,
            },
        }

    def _read(self, resp: httpx.Response, budget: int) -> tuple[str, int | None]:
        """The answer and, when it was cut short, the window to request it again with."""
        resp.raise_for_status()
        data = resp.json()
        text = data.get("message", {}).get("content", "") or ""
        if data.get("done_reason") != "length":
            return text, None
        grown = next_output_window(budget, self._max_tokens_limit)
        log.warning(
            "llm_output_truncated",
            max_tokens=budget,
            retry_max_tokens=grown,
            content_chars=len(text),
        )
        return text, grown

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        if self._async is None:
            self._async = httpx.AsyncClient(timeout=self._timeout)
        budget = self._max_tokens if max_tokens is None else max_tokens
        while True:
            resp = await self._async.post(
                f"{self.base_url}/api/chat",
                json=self._payload(prompt, system, temperature, budget),
            )
            text, grown = self._read(resp, budget)
            if grown is None:
                return text
            budget = grown

    def complete_sync(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        if self._sync is None:
            self._sync = httpx.Client(timeout=self._timeout)
        budget = self._max_tokens if max_tokens is None else max_tokens
        while True:
            resp = self._sync.post(
                f"{self.base_url}/api/chat",
                json=self._payload(prompt, system, temperature, budget),
            )
            text, grown = self._read(resp, budget)
            if grown is None:
                return text
            budget = grown

    async def aclose(self) -> None:
        if self._async is not None:
            await self._async.aclose()
            self._async = None
        if self._sync is not None:
            self._sync.close()
            self._sync = None
