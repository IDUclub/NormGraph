"""Native Ollama chat provider (``/api/chat``).

An alternative to the OpenAI-compatible provider for deployments that talk to Ollama directly.
``base_url`` is the Ollama root (e.g. ``http://localhost:11434``), without ``/v1``.
"""

from __future__ import annotations

import httpx

from src.providers.base import LLMProvider


class OllamaLLM(LLMProvider):
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: float = 600.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._async: httpx.AsyncClient | None = None
        self._sync: httpx.Client | None = None

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
            "stream": False,
            "options": {
                "temperature": (
                    self._temperature if temperature is None else temperature
                ),
                "num_predict": self._max_tokens if max_tokens is None else max_tokens,
            },
        }

    @staticmethod
    def _extract(data: dict) -> str:
        return data.get("message", {}).get("content", "") or ""

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
            f"{self.base_url}/api/chat",
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
            f"{self.base_url}/api/chat",
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
