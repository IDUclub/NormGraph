"""OpenAI-compatible chat provider.

Works against any endpoint that speaks the OpenAI ``/v1/chat/completions`` protocol — vLLM,
LM Studio, llama.cpp's server, Ollama's ``/v1`` shim, or the OpenAI API itself. ``base_url`` must
point at the ``/v1`` root.
"""

from __future__ import annotations

import httpx

from src.providers.base import LLMProvider


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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._timeout = timeout
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
        return {
            "model": self.model,
            "messages": messages,
            "temperature": self._temperature if temperature is None else temperature,
            "max_tokens": self._max_tokens if max_tokens is None else max_tokens,
        }

    @staticmethod
    def _extract(data: dict) -> str:
        return data["choices"][0]["message"]["content"] or ""

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
