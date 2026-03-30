"""Agent 运行时使用的核心共享数据结构。

这里的类型会故意保持简单，它们是循环层、工具层、LLM 适配层之间
共同使用的“内部语言”。

这些结构保持和具体厂商无关很重要：
- 循环层不应该关心底层模型是 Anthropic、OpenAI 还是别家
- 工具层不应该关心工具调用请求来自哪个 SDK
- 后续功能可以继续复用这些结构，而不是每加一层能力就重写一遍
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


MessageRole = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class ToolCall:
    """LLM 产生的一次标准化工具调用。

    不同模型厂商返回的工具调用格式都会有一些差异。
    适配层会把这些厂商私有格式转换成统一结构，
    这样 Agent 循环就可以保持简单。
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ConversationMessage:
    """运行时内部保存的一条对话消息。

    说明：
    - `content` 在内部状态里先统一保存为纯文本，便于第一版实现
    - `tool_calls` 只会出现在 assistant 消息上，表示模型请求执行工具
    - `tool_call_id` 只会出现在 tool 消息上，用来让模型把工具结果和原调用对应起来
    """

    role: MessageRole
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(slots=True)
class LLMResponse:
    """一次模型调用后的标准化结果。

    对循环层来说，真正关心的只有两件事：
    - 模型这次有没有返回文本内容
    - 模型这次有没有请求工具调用

    如果 `tool_calls` 为空，就可以把这条 assistant 消息
    当作当前回合的最终回答。
    """

    message: ConversationMessage
