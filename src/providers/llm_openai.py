"""OpenAI-compatible chat provider.

Works against any endpoint that speaks the OpenAI ``/v1/chat/completions`` protocol — vLLM,
LM Studio, llama.cpp's server, Ollama's ``/v1`` shim, or the OpenAI API itself. ``base_url`` must
point at the ``/v1`` root.
"""

from __future__ import annotations

import httpx
import structlog

from src.providers.base import LLMProvider

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
        timeout: float = 600.0,
        reasoning_effort: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._temperature = temperature
        self._max_tokens = max_tokens
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
        max_tokens: int | None,
    ) -> dict:
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self._temperature if temperature is None else temperature,
            "max_tokens": self._max_tokens if max_tokens is None else max_tokens,
        }
        if self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort
        return payload

    @staticmethod
    def _extract(data: dict) -> str:
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            # Reasoning models can exhaust max_tokens before emitting any answer.
            log.warning(
                "llm_output_truncated",
                completion_tokens=(data.get("usage") or {}).get("completion_tokens"),
                content_chars=len(choice["message"].get("content") or ""),
            )
        return choice["message"]["content"] or ""

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
        resp = await self._async.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json=self._payload(prompt, system, temperature, max_tokens),
        )
        resp.raise_for_status()
        return self._extract(resp.json())

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
        resp = self._sync.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json=self._payload(prompt, system, temperature, max_tokens),
        )
        resp.raise_for_status()
        return self._extract(resp.json())

    async def aclose(self) -> None:
        if self._async is not None:
            await self._async.aclose()
            self._async = None
        if self._sync is not None:
            self._sync.close()
            self._sync = None
