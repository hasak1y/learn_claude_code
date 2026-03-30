"""与具体厂商无关的 LLM 客户端接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..types import LLMResponse, ConversationMessage


class BaseLLMClient(ABC):
    """模型提供方的抽象接口。

    Agent 循环只依赖这个接口，这意味着：
    - 以后新增 Anthropic 支持时，只需要增加一个新适配器
    - 更换模型厂商时，不需要去改循环层
    - 测试时可以用假的实现替代真实网络请求
    """

    @abstractmethod
    def generate(
        self,
        messages: list[ConversationMessage],
        tools: list[dict[str, Any]],
        system_prompt: str,
    ) -> LLMResponse:
        """返回模型的下一条 assistant 消息，格式需先标准化。"""
