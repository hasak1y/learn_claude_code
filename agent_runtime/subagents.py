"""阻塞式子代理运行器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .agent import AgentLoop
from .compaction import ConversationCompactor
from .llm.base import BaseLLMClient
from .session_log import SessionLogger
from .tools.base import ToolRegistry
from .types import AgentRunResult, ConversationMessage


@dataclass(slots=True)
class SubagentRunner:
    """同步运行子代理的最小执行器。"""

    llm_client_factory: Callable[[], BaseLLMClient]
    child_tool_registry_factory: Callable[[], ToolRegistry]
    child_system_prompt: str
    child_startup_messages: list[ConversationMessage] | None = None
    child_max_steps: int = 12
    compactor: ConversationCompactor | None = None
    session_logger: SessionLogger | None = None

    def run_subagent(self, prompt: str) -> AgentRunResult:
        """同步执行一个子代理任务，并返回结构化运行结果。"""

        llm_client = self.llm_client_factory()
        tool_registry = self.child_tool_registry_factory()
        sub_messages: list[ConversationMessage] = []

        # 启动上下文作为“系统提示之后的上下文消息”注入，而不是拼进 system prompt。
        for message in self.child_startup_messages or []:
            copied = ConversationMessage(role=message.role, content=message.content)
            sub_messages.append(copied)
            if self.session_logger is not None:
                self.session_logger.append_message(copied, scope="subagent")

        user_message = ConversationMessage(role="user", content=prompt)
        sub_messages.append(user_message)
        if self.session_logger is not None:
            self.session_logger.append_message(user_message, scope="subagent")

        return AgentLoop(
            llm_client=llm_client,
            tool_registry=tool_registry,
            system_prompt=self.child_system_prompt,
            max_steps=self.child_max_steps,
            echo_tool_calls=False,
            todo_manager=None,
            compactor=self.compactor,
            session_logger=self.session_logger,
            log_scope="subagent",
        ).run(messages=sub_messages)
