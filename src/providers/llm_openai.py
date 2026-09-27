"""OpenAI-compatible chat provider.

Works against any endpoint that speaks the OpenAI ``/v1/chat/completions`` protocol — vLLM,
LM Studio, llama.cpp's server, Ollama's ``/v1`` shim, or the OpenAI API itself. ``base_url`` must
point at the ``/v1`` root.

An answer cut at ``max_tokens`` is requested again with a doubled window, up to
``max_tokens_limit`` (see ``next_output_window``).
"""

from __future__ import annotations

import httpx
import structlog

from src.providers.base import LLMProvider, next_output_window

log = structlog.get_logger(__name__)


class OpenAICompatibleLLM(LLMProvider):
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        max_tokens_limit: int | None = None,
        timeout: float = 600.0,
        reasoning_effort: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_tokens_limit = max(max_tokens_limit or 0, max_tokens)
        self._timeout = timeout
        self._reasoning_effort = reasoning_effort
        self._async: httpx.AsyncClient | None = None
        self._sync: httpx.Client | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

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
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self._temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
        }
        if self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort
        return payload

    def _read(self, resp: httpx.Response, budget: int) -> tuple[str, int | None]:
        """The answer and, when it was cut short, the window to request it again with."""
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        text = choice["message"]["content"] or ""
        if choice.get("finish_reason") != "length":
            return text, None
        # Reasoning models can exhaust max_tokens before emitting any answer.
        grown = next_output_window(budget, self._max_tokens_limit)
        log.warning(
            "llm_output_truncated",
            max_tokens=budget,
            retry_max_tokens=grown,
            completion_tokens=(data.get("usage") or {}).get("completion_tokens"),
            content_chars=len(text),
        )
        return text, grown

    @staticmethod
    def _window_rejected(resp: httpx.Response, budget: int) -> bool:
        # A grown window may not fit the model context next to a long prompt (vLLM answers 400);
        # the truncated answer already in hand is then the best there is.
        if resp.status_code != 400:
            return False
        log.warning("llm_output_window_rejected", max_tokens=budget)
        return True

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
        text, grown = "", None
        while True:
            resp = await self._async.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=self._payload(prompt, system, temperature, budget),
            )
            if grown and self._window_rejected(resp, budget):
                return text
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
        text, grown = "", None
        while True:
            resp = self._sync.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=self._payload(prompt, system, temperature, budget),
            )
            if grown and self._window_rejected(resp, budget):
                return text
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
