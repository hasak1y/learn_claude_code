"""最小 hook 抽象。

目标：
1. 让 AgentLoop 只感知生命周期事件，而不理解每个横切能力的细节。
2. hook 只返回决策和附加消息，由主循环统一应用。
3. 保持接口足够小，先服务权限审查与后台任务注入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .types import ConversationMessage, ToolCall


HookEventName = Literal[
    "before_llm_request",
    "after_llm_response",
    "before_tool_execute",
    "after_tool_execute",
    "before_compact",
    "after_compact",
]


@dataclass(slots=True)
class HookContext:
    """描述一次 hook 触发时主循环交出的上下文。"""

    event: HookEventName
    messages: list[ConversationMessage]
    step_index: int | None = None
    tool_call: ToolCall | None = None
    tool_output: str | None = None
    llm_response_message: ConversationMessage | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HookResult:
    """hook 返回给主循环的结果。

    - decision 只表达控制意图，不直接修改主循环状态。
    - request_messages 会只在当前请求视图里生效。
    - append_messages 会被主循环真正写入活跃 history。
    """

    decision: Literal["continue", "skip", "abort"] = "continue"
    reason: str | None = None
    request_messages: list[ConversationMessage] = field(default_factory=list)
    append_messages: list[ConversationMessage] = field(default_factory=list)
    updates: dict[str, Any] = field(default_factory=dict)


class Hook(Protocol):
    """单个 hook 的最小接口。"""

    def handle(self, ctx: HookContext) -> HookResult:
        ...


class HookManager:
    """按事件分发 hook，并合并多个 hook 的结果。"""

    def __init__(self, hooks: dict[HookEventName, list[Hook]] | None = None) -> None:
        self._hooks = hooks or {}

    def emit(self, ctx: HookContext) -> HookResult:
        """触发某个生命周期事件并合并结果。"""

        merged = HookResult()
        for hook in self._hooks.get(ctx.event, []):
            result = hook.handle(ctx)

            if result.request_messages:
                merged.request_messages.extend(result.request_messages)
            if result.append_messages:
                merged.append_messages.extend(result.append_messages)
            if result.updates:
                merged.updates.update(result.updates)

            if result.decision == "abort":
                return HookResult(
                    decision="abort",
                    reason=result.reason,
                    request_messages=merged.request_messages,
                    append_messages=merged.append_messages,
                    updates=merged.updates,
                )

            if result.decision == "skip":
                merged.decision = "skip"
                merged.reason = result.reason

        return merged
