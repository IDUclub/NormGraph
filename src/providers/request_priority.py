"""Scheduling priorities for generative LLM requests (never embeddings)."""

from __future__ import annotations

from typing import Literal

import structlog

log = structlog.get_logger(__name__)

RequestKind = Literal["extraction", "check_plans", "query"]
DEFAULT_PRIORITIES = {"extraction": 100, "check_plans": 100, "query": -100, "user": 20}


class RequestPriorities:
    def __init__(self, overrides: str = "", *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.values = dict(DEFAULT_PRIORITIES)
        invalid = 0
        for entry in overrides.split(",") if overrides.strip() else ():
            key, separator, value = entry.partition("=")
            key = key.strip()
            try:
                if not separator or key not in self.values:
                    raise ValueError
                self.values[key] = int(value.strip())
            except ValueError:
                invalid += 1
        if invalid:
            log.warning("llm_request_priorities_invalid", entries=invalid)

    def resolve(self, kind: RequestKind, *, user_document: bool = False) -> int:
        if kind not in DEFAULT_PRIORITIES or kind == "user":
            raise ValueError(f"unknown LLM request kind: {kind!r}")
        if not self.enabled:
            return 0
        return self.values["user" if user_document else kind]
