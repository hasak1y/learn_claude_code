"""Agent 的核心循环。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .compaction import ConversationCompactor
from .llm.base import BaseLLMClient
from .session_log import SessionLogger
from .todo import TodoManager
from .tools.base import ToolRegistry
from .types import AgentRunResult, ConversationMessage


@dataclass(slots=True)
class AgentLoop:
    """一个最小但可复用的 Agent 运行时。

    当前版本在原有工具循环上补了三层压缩能力：
    - 每轮请求前的 micro_compact
    - 超阈值时的 auto_compact
    - 模型显式触发的 manual compact
    """

    llm_client: BaseLLMClient
    tool_registry: ToolRegistry
    system_prompt: str
    max_steps: int = 8
    echo_tool_calls: bool = True
    todo_manager: TodoManager | None = None
    compactor: ConversationCompactor | None = None
    session_logger: SessionLogger | None = None
    log_scope: str = "parent"

    def run(
        self,
        messages: list[ConversationMessage],
        should_cancel: Callable[[], bool] | None = None,
    ) -> AgentRunResult:
        """执行一次完整的 assistant 回合。"""

        for step_index in range(1, self.max_steps + 1):
            if should_cancel is not None and should_cancel():
                return self._build_terminal_result(
                    messages=messages,
                    status="cancelled",
                    steps=step_index - 1,
                    fallback_text="子代理任务已取消。",
                    append_message=True,
                )

            if self.compactor is not None and self.compactor.should_auto_compact(
                messages=messages,
                system_prompt=self.system_prompt,
            ):
                compact_result = self.compactor.compact_history(
                    messages=messages,
                    llm_client=self.llm_client,
                    system_prompt=self.system_prompt,
                    todo_manager=self.todo_manager,
                    reason="auto",
                    session_logger=self.session_logger,
                    log_scope=self.log_scope,
                )
                if self.echo_tool_calls:
                    print(f"[auto_compact] {compact_result}")

            request_messages = list(messages)
            if self.todo_manager is not None and self.todo_manager.should_remind():
                request_messages.append(
                    ConversationMessage(
                        role="user",
                        content=self.todo_manager.build_reminder(),
                    )
                )

            if self.compactor is not None:
                request_messages = self.compactor.build_request_messages(request_messages)

            response = self.llm_client.generate(
                messages=request_messages,
                tools=self.tool_registry.get_tool_schemas(),
                system_prompt=self.system_prompt,
            )
            self._append_message(messages, response.message)

            if not response.message.tool_calls:
                if self.todo_manager is not None:
                    self.todo_manager.note_round(touched_todo=False)
                return AgentRunResult(
                    status="completed",
                    final_text=response.message.content or "（无最终文本）",
                    steps=step_index,
                    last_message=response.message,
                )

            touched_todo = False
            for tool_call in response.message.tool_calls:
                if should_cancel is not None and should_cancel():
                    return self._build_terminal_result(
                        messages=messages,
                        status="cancelled",
                        steps=step_index,
                        fallback_text="子代理任务已取消。",
                        append_message=True,
                    )

                if self.echo_tool_calls:
                    print(f"\033[33m[{step_index}] $ {tool_call.name} {tool_call.arguments}\033[0m")

                if tool_call.name == "compact" and self.compactor is not None:
                    tool_output = self.compactor.compact_history(
                        messages=messages,
                        llm_client=self.llm_client,
                        system_prompt=self.system_prompt,
                        todo_manager=self.todo_manager,
                        reason="manual",
                        session_logger=self.session_logger,
                        log_scope=self.log_scope,
                    )
                else:
                    tool_output = self.tool_registry.execute(
                        name=tool_call.name,
                        arguments=tool_call.arguments,
                    )

                if tool_call.name == "todo":
                    touched_todo = True

                if self.echo_tool_calls:
                    preview = tool_output[:200]
                    print(preview)
                    if len(tool_output) > 200:
                        print("... [预览已截断]")

                self._append_message(
                    messages,
                    ConversationMessage(
                        role="tool",
                        content=tool_output,
                        tool_call_id=tool_call.id,
                        name=tool_call.name,
                    ),
                )

            if self.todo_manager is not None:
                self.todo_manager.note_round(touched_todo=touched_todo)

        limit_message = ConversationMessage(
            role="assistant",
            content=(
                f"运行时在达到步数上限（{self.max_steps}）后停止。"
                "如果你提高上限或调整提示词，可以继续执行。"
            ),
        )
        self._append_message(messages, limit_message)
        return AgentRunResult(
            status="max_steps",
            final_text=limit_message.content,
            steps=self.max_steps,
            last_message=limit_message,
        )

    def _build_terminal_result(
        self,
        messages: list[ConversationMessage],
        status: str,
        steps: int,
        fallback_text: str,
        append_message: bool,
    ) -> AgentRunResult:
        """构造取消等终止态结果。"""

        terminal_message = ConversationMessage(
            role="assistant",
            content=fallback_text,
        )

        if append_message:
            self._append_message(messages, terminal_message)

        return AgentRunResult(
            status=status,
            final_text=fallback_text,
            steps=steps,
            last_message=terminal_message,
        )

    def _append_message(
        self,
        messages: list[ConversationMessage],
        message: ConversationMessage,
    ) -> None:
        """统一追加消息，并在需要时同步写入 session log。"""

        messages.append(message)
        if self.session_logger is not None:
            self.session_logger.append_message(message, scope=self.log_scope)
