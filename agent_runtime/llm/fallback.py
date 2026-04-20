"""多 endpoint 的最小 failover LLM client。"""

from __future__ import annotations

from typing import Iterable

from ..types import ConversationMessage, LLMResponse
from .base import BaseLLMClient


class FallbackLLMClient(BaseLLMClient):
    """顺序尝试多个底层 client，遇到暂时性故障时自动切换。"""

    def __init__(self, clients: Iterable[BaseLLMClient]) -> None:
        self._clients = list(clients)
        if not self._clients:
            raise ValueError("FallbackLLMClient 至少需要一个底层 client。")

    def generate(
        self,
        messages: list[ConversationMessage],
        tools: list[dict],
        system_prompt: str,
    ) -> LLMResponse:
        errors: list[str] = []

        for index, client in enumerate(self._clients):
            try:
                return client.generate(
                    messages=messages,
                    tools=tools,
                    system_prompt=system_prompt,
                )
            except RuntimeError as exc:
                message = str(exc)
                errors.append(f"endpoint[{index}] {message}")
                if not self._is_failover_worthy(message):
                    raise RuntimeError(message) from exc
                continue

        raise RuntimeError("；".join(errors))

    @staticmethod
    def _is_failover_worthy(message: str) -> bool:
        lowered = message.lower()
        keywords = (
            "http 502",
            "http 503",
            "http 504",
            "service unavailable",
            "gateway timeout",
            "bad gateway",
            "system cpu overloaded",
            "timeout",
            "timed out",
            "connection reset",
            "remote end closed",
            "temporarily unavailable",
            "disconnected",
        )
        return any(keyword in lowered for keyword in keywords)
