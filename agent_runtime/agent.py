"""Agent 主循环。

这一版不是把错误处理继续堆成更多 try/except，而是引入一层最小恢复模型：
- 先把异常分类
- 再决定恢复动作
- 最后用显式运行状态承载恢复过程

当前已经覆盖三条最关键路径：
1. LLM 调用
2. compact
3. tool 执行
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from .background_jobs import BackgroundJobManager
from .compaction import ConversationCompactor
from .hooks import HookContext, HookManager, HookResult
from .llm.base import BaseLLMClient
from .recovery import (
    AgentRuntimeState,
    RecoveryDecision,
    RuntimeErrorInfo,
    classify_error,
    decide_recovery,
)
from .session_log import SessionLogger
from .task_graph import TaskGraphManager
from .todo import TodoManager
from .tools.base import ToolRegistry
from .types import AgentRunResult, ConversationMessage, LLMResponse, ToolCall


@dataclass(slots=True)
class AgentLoop:
    """一个最小但可复用的 Agent 运行时。"""

    llm_client: BaseLLMClient
    tool_registry: ToolRegistry
    system_prompt: str
    max_steps: int = 8
    max_recovery_attempts: int = 2
    echo_tool_calls: bool = True
    todo_manager: TodoManager | None = None
    task_graph_manager: TaskGraphManager | None = None
    background_job_manager: BackgroundJobManager | None = None
    compactor: ConversationCompactor | None = None
    session_logger: SessionLogger | None = None
    log_scope: str = "parent"
    hook_manager: HookManager | None = None
    runtime_state: AgentRuntimeState = "RUNNING"

    def run(
        self,
        messages: list[ConversationMessage],
        should_cancel: Callable[[], bool] | None = None,
    ) -> AgentRunResult:
        """执行一次完整的 assistant 回合。"""

        self.runtime_state = "RUNNING"
        runtime_notes: list[str] = []
        step_index = 1

        while step_index <= self.max_steps:
            if should_cancel is not None and should_cancel():
                return self._build_terminal_result(
                    messages=messages,
                    status="cancelled",
                    steps=max(0, step_index - 1),
                    fallback_text="子代理任务已取消。",
                    append_message=True,
                    runtime_notes=runtime_notes,
                )

            task_graph_summary = (
                self.task_graph_manager.render_summary()
                if self.task_graph_manager is not None
                else None
            )

            before_request_result = self._emit_hook_safely(
                ctx=HookContext(
                    event="before_llm_request",
                    messages=messages,
                    step_index=step_index,
                    extras={"system_prompt": self.system_prompt},
                ),
                runtime_notes=runtime_notes,
                required=False,
            )
            if isinstance(before_request_result, AgentRunResult):
                return before_request_result

            request_messages = list(messages)
            request_messages.extend(before_request_result.request_messages)

            auto_compact_result = self._maybe_auto_compact(
                messages=messages,
                step_index=step_index,
                task_graph_summary=task_graph_summary,
                runtime_notes=runtime_notes,
            )
            if isinstance(auto_compact_result, AgentRunResult):
                return auto_compact_result
            if auto_compact_result == "restart":
                continue

            if self.todo_manager is not None and self.todo_manager.should_remind():
                request_messages.append(
                    ConversationMessage(
                        role="user",
                        content=self.todo_manager.build_reminder(
                            task_graph_summary=task_graph_summary
                        ),
                    )
                )

            if self.compactor is not None:
                request_messages = self.compactor.build_request_messages(request_messages)

            llm_result = self._generate_with_recovery(
                messages=messages,
                request_messages=request_messages,
                step_index=step_index,
                task_graph_summary=task_graph_summary,
                runtime_notes=runtime_notes,
            )
            if isinstance(llm_result, AgentRunResult):
                return llm_result
            if llm_result is None:
                # 当前 step 已完成压缩恢复，重新执行同一步。
                continue

            response = llm_result
            self._append_message(messages, response.message)
            after_llm_result = self._emit_hook_safely(
                ctx=HookContext(
                    event="after_llm_response",
                    messages=messages,
                    step_index=step_index,
                    llm_response_message=response.message,
                ),
                runtime_notes=runtime_notes,
                required=False,
            )
            if isinstance(after_llm_result, AgentRunResult):
                return after_llm_result

            if not response.message.tool_calls:
                if self.todo_manager is not None:
                    self.todo_manager.note_round(touched_tracking=False)

                self.runtime_state = "COMPLETED"
                final_text = self._compose_final_text(
                    response.message.content or "（无最终文本）",
                    runtime_notes,
                )
                return AgentRunResult(
                    status="completed",
                    final_text=final_text,
                    steps=step_index,
                    last_message=response.message,
                )

            touched_tracking = False
            for tool_call in response.message.tool_calls:
                if should_cancel is not None and should_cancel():
                    return self._build_terminal_result(
                        messages=messages,
                        status="cancelled",
                        steps=step_index,
                        fallback_text="子代理任务已取消。",
                        append_message=True,
                        runtime_notes=runtime_notes,
                    )

                if self.echo_tool_calls:
                    print(f"\033[33m[{step_index}] $ {tool_call.name} {tool_call.arguments}\033[0m")

                before_tool_result = self._emit_hook_safely(
                    ctx=HookContext(
                        event="before_tool_execute",
                        messages=messages,
                        step_index=step_index,
                        tool_call=tool_call,
                    ),
                    runtime_notes=runtime_notes,
                    required=True,
                )
                if isinstance(before_tool_result, AgentRunResult):
                    return before_tool_result

                tool_result = self._execute_tool_path(
                    messages=messages,
                    tool_call=tool_call,
                    before_tool_result=before_tool_result,
                    step_index=step_index,
                    task_graph_summary=task_graph_summary,
                    runtime_notes=runtime_notes,
                )
                if isinstance(tool_result, AgentRunResult):
                    return tool_result

                tool_output = tool_result
                if tool_call.name in {"todo", "task_create", "task_update"}:
                    touched_tracking = True

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

                after_tool_result = self._emit_hook_safely(
                    ctx=HookContext(
                        event="after_tool_execute",
                        messages=messages,
                        step_index=step_index,
                        tool_call=tool_call,
                        tool_output=tool_output,
                    ),
                    runtime_notes=runtime_notes,
                    required=False,
                )
                if isinstance(after_tool_result, AgentRunResult):
                    return after_tool_result

            if self.todo_manager is not None:
                self.todo_manager.note_round(touched_tracking=touched_tracking)

            step_index += 1

        self.runtime_state = "FAILED"
        limit_text = (
            f"运行时在达到步数上限（{self.max_steps}）后停止。"
            "如果你提高上限或调整提示词，可以继续执行。"
        )
        limit_text = self._compose_final_text(limit_text, runtime_notes)
        limit_message = ConversationMessage(role="assistant", content=limit_text)
        self._append_message(messages, limit_message)
        return AgentRunResult(
            status="max_steps",
            final_text=limit_text,
            steps=self.max_steps,
            last_message=limit_message,
        )

    def _generate_with_recovery(
        self,
        *,
        messages: list[ConversationMessage],
        request_messages: list[ConversationMessage],
        step_index: int,
        task_graph_summary: str | None,
        runtime_notes: list[str],
    ) -> LLMResponse | AgentRunResult | None:
        """执行 LLM 调用，并在需要时触发重试或压缩恢复。"""

        compact_attempted = False

        for attempt in range(self.max_recovery_attempts + 1):
            self.runtime_state = "RUNNING" if attempt == 0 else "RETRYING"
            try:
                return self.llm_client.generate(
                    messages=request_messages,
                    tools=self.tool_registry.get_tool_schemas(),
                    system_prompt=self.system_prompt,
                )
            except Exception as exc:
                error = classify_error(exc, stage="llm")
                decision = decide_recovery(
                    error,
                    attempt=attempt,
                    max_attempts=self.max_recovery_attempts,
                    has_compactor=self.compactor is not None,
                    compact_already_attempted=compact_attempted,
                )

                if decision.action == "retry":
                    self._record_runtime_note(
                        runtime_notes,
                        f"LLM 请求失败，正在进行第 {attempt + 1} 次重试：{error.message}",
                    )
                    continue

                if decision.action == "compact_and_resume":
                    compact_attempted = True
                    compact_result = self._run_compact_with_recovery(
                        messages=messages,
                        step_index=step_index,
                        reason="recovery_context",
                        task_graph_summary=task_graph_summary,
                        runtime_notes=runtime_notes,
                        is_manual=False,
                    )
                    if isinstance(compact_result, AgentRunResult):
                        return compact_result
                    self._record_runtime_note(
                        runtime_notes,
                        f"LLM 请求失败且触发了压缩恢复：{error.message}",
                    )
                    self.runtime_state = "RESUMING"
                    return None

                return self._build_failure_result(
                    messages=messages,
                    error=error,
                    decision=decision,
                    steps=max(0, step_index - 1),
                    runtime_notes=runtime_notes,
                )

        error = RuntimeErrorInfo(
            stage="llm",
            category="unknown",
            scope="fatal",
            message="LLM 请求在重试后仍未成功。",
            original_exception=RuntimeError("LLM 请求在重试后仍未成功。"),
            retryable=False,
        )
        return self._build_failure_result(
            messages=messages,
            error=error,
            decision=RecoveryDecision(action="fail", reason="重试耗尽后仍未恢复。"),
            steps=max(0, step_index - 1),
            runtime_notes=runtime_notes,
        )

    def _maybe_auto_compact(
        self,
        *,
        messages: list[ConversationMessage],
        step_index: int,
        task_graph_summary: str | None,
        runtime_notes: list[str],
    ) -> AgentRunResult | Literal["restart"] | None:
        """在进入 LLM 之前尝试自动 compact。"""

        if self.compactor is None:
            return None

        if not self.compactor.should_auto_compact(
            messages=messages,
            system_prompt=self.system_prompt,
        ):
            return None

        compact_result = self._run_compact_with_recovery(
            messages=messages,
            step_index=step_index,
            reason="auto",
            task_graph_summary=task_graph_summary,
            runtime_notes=runtime_notes,
            is_manual=False,
        )
        if isinstance(compact_result, AgentRunResult):
            return compact_result
        return "restart"

    def _execute_tool_path(
        self,
        *,
        messages: list[ConversationMessage],
        tool_call: ToolCall,
        before_tool_result: HookResult,
        step_index: int,
        task_graph_summary: str | None,
        runtime_notes: list[str],
    ) -> str | AgentRunResult:
        """执行工具路径，并在步骤级错误上做恢复或降级。"""

        if before_tool_result.decision == "abort":
            return before_tool_result.reason or "操作已中止。"
        if before_tool_result.decision == "skip":
            return before_tool_result.reason or "操作已跳过。"

        if tool_call.name == "compact" and self.compactor is not None:
            manual_compact_result = self._run_compact_with_recovery(
                messages=messages,
                step_index=step_index,
                reason="manual",
                task_graph_summary=task_graph_summary,
                runtime_notes=runtime_notes,
                is_manual=True,
                tool_call=tool_call,
            )
            if isinstance(manual_compact_result, AgentRunResult):
                return manual_compact_result
            return manual_compact_result or "上下文压缩已完成。"

        for attempt in range(self.max_recovery_attempts + 1):
            self.runtime_state = "RUNNING" if attempt == 0 else "RETRYING"
            try:
                return self.tool_registry.execute(
                    name=tool_call.name,
                    arguments=tool_call.arguments,
                )
            except Exception as exc:
                error = classify_error(exc, stage="tool")
                decision = decide_recovery(
                    error,
                    attempt=attempt,
                    max_attempts=self.max_recovery_attempts,
                    has_compactor=False,
                )

                if decision.action == "retry":
                    self._record_runtime_note(
                        runtime_notes,
                        f"工具 {tool_call.name} 执行失败，正在进行第 {attempt + 1} 次重试：{error.message}",
                    )
                    continue

                if decision.action == "record_and_continue":
                    step_error = self._format_step_error(
                        stage="tool",
                        name=tool_call.name,
                        message=error.message,
                    )
                    self._record_runtime_note(
                        runtime_notes,
                        f"工具 {tool_call.name} 失败，已转成步骤级错误继续：{error.message}",
                    )
                    return step_error

                return self._build_failure_result(
                    messages=messages,
                    error=error,
                    decision=decision,
                    steps=step_index,
                    runtime_notes=runtime_notes,
                )

        return self._format_step_error(
            stage="tool",
            name=tool_call.name,
            message="工具在重试后仍未成功执行。",
        )

    def _run_compact_with_recovery(
        self,
        *,
        messages: list[ConversationMessage],
        step_index: int,
        reason: str,
        task_graph_summary: str | None,
        runtime_notes: list[str],
        is_manual: bool,
        tool_call: ToolCall | None = None,
    ) -> str | AgentRunResult | None:
        """执行 compact，并对其失败进行有限恢复。"""

        if self.compactor is None:
            return "当前未启用 compact。"

        self._emit_hook_safely(
            ctx=HookContext(
                event="before_compact",
                messages=messages,
                step_index=step_index,
                tool_call=tool_call,
                extras={"reason": reason},
            ),
            runtime_notes=runtime_notes,
            required=False,
        )

        for attempt in range(self.max_recovery_attempts + 1):
            self.runtime_state = "COMPACTING"
            try:
                compact_output = self.compactor.compact_history(
                    messages=messages,
                    llm_client=self.llm_client,
                    system_prompt=self.system_prompt,
                    todo_manager=self.todo_manager,
                    reason=reason,
                    session_logger=self.session_logger,
                    log_scope=self.log_scope,
                    task_graph_summary=task_graph_summary,
                )
                if self.echo_tool_calls and not is_manual:
                    print(f"[auto_compact] {compact_output}")

                self._emit_hook_safely(
                    ctx=HookContext(
                        event="after_compact",
                        messages=messages,
                        step_index=step_index,
                        tool_call=tool_call,
                        tool_output=compact_output,
                        extras={"reason": reason},
                    ),
                    runtime_notes=runtime_notes,
                    required=False,
                )
                self.runtime_state = "RESUMING"
                return compact_output
            except Exception as exc:
                error = classify_error(
                    exc,
                    stage="compact",
                    is_manual_compact=is_manual,
                )
                decision = decide_recovery(
                    error,
                    attempt=attempt,
                    max_attempts=self.max_recovery_attempts,
                    has_compactor=False,
                )

                if decision.action == "retry":
                    self._record_runtime_note(
                        runtime_notes,
                        f"compact 失败，正在进行第 {attempt + 1} 次重试：{error.message}",
                    )
                    continue

                if decision.action == "record_and_continue" and not is_manual:
                    self._record_runtime_note(
                        runtime_notes,
                        f"自动 compact 失败，已跳过本次压缩：{error.message}",
                    )
                    self.runtime_state = "RUNNING"
                    return None

                if decision.action == "record_and_continue" and is_manual:
                    step_error = self._format_step_error(
                        stage="compact",
                        name="compact",
                        message=error.message,
                    )
                    self._record_runtime_note(
                        runtime_notes,
                        f"手动 compact 失败，已转成步骤级错误继续：{error.message}",
                    )
                    self.runtime_state = "RUNNING"
                    return step_error

                return self._build_failure_result(
                    messages=messages,
                    error=error,
                    decision=decision,
                    steps=step_index,
                    runtime_notes=runtime_notes,
                )

        if is_manual:
            return self._format_step_error(
                stage="compact",
                name="compact",
                message="手动 compact 在重试后仍未成功。",
            )
        return None

    def _emit_hook_safely(
        self,
        *,
        ctx: HookContext,
        runtime_notes: list[str],
        required: bool,
    ) -> HookResult | AgentRunResult:
        """统一触发 hook，并对 hook 自身异常做步骤级或附属级处理。"""

        if self.hook_manager is None:
            return HookResult()

        try:
            result = self.hook_manager.emit(ctx)
            for message in result.append_messages:
                self._append_message(messages=ctx.messages, message=message)
            return result
        except Exception as exc:
            error = classify_error(
                exc,
                stage="hook",
                hook_event=ctx.event,
            )
            decision = decide_recovery(
                error,
                attempt=self.max_recovery_attempts,
                max_attempts=self.max_recovery_attempts,
                has_compactor=False,
            )

            if not required and decision.action == "record_and_continue":
                self._record_runtime_note(
                    runtime_notes,
                    f"hook {ctx.event} 执行失败，已记录并继续：{error.message}",
                )
                return HookResult()

            return self._build_failure_result(
                messages=ctx.messages,
                error=error,
                decision=decision,
                steps=max(0, (ctx.step_index or 1) - 1),
                runtime_notes=runtime_notes,
            )

    def _build_failure_result(
        self,
        *,
        messages: list[ConversationMessage],
        error: RuntimeErrorInfo,
        decision: RecoveryDecision,
        steps: int,
        runtime_notes: list[str],
    ) -> AgentRunResult:
        """构造统一的失败结果。"""

        degraded_result = self._maybe_build_tool_backed_result(
            messages=messages,
            error=error,
            steps=steps,
            runtime_notes=runtime_notes,
        )
        if degraded_result is not None:
            self.runtime_state = "COMPLETED"
            return degraded_result

        self.runtime_state = "FAILED"
        error_text = (
            "运行失败：\n"
            f"- 阶段：{error.stage}\n"
            f"- 分类：{error.category}\n"
            f"- 范围：{error.scope}\n"
            f"- 恢复决策：{decision.action}\n"
            f"- 原因：{error.message}"
        )
        final_text = self._compose_final_text(error_text, runtime_notes)
        failure_message = ConversationMessage(role="assistant", content=final_text)
        self._append_message(messages, failure_message)
        return AgentRunResult(
            status="failed",
            final_text=final_text,
            steps=steps,
            last_message=failure_message,
            error=error.message,
        )

    def _maybe_build_tool_backed_result(
        self,
        *,
        messages: list[ConversationMessage],
        error: RuntimeErrorInfo,
        steps: int,
        runtime_notes: list[str],
    ) -> AgentRunResult | None:
        """在“工具已成功执行，但收尾 LLM 失败”时做保守降级。

        典型场景：
        - MCP 搜索工具已经返回结果
        - 主循环准备再调用一次 LLM 去总结或润色
        - 上游模型服务 503 / overload

        这时与其把整轮直接判成 fatal，更合理的是把最近一次 tool 输出直接交给用户，
        同时附带运行时提示，明确说明失败发生在收尾阶段。
        """

        if error.stage != "llm":
            return None
        if not messages:
            return None

        last_message = messages[-1]
        if last_message.role != "tool":
            return None

        self._record_runtime_note(
            runtime_notes,
            f"工具结果已成功取得，但后续 LLM 收尾失败，已直接返回最近一次工具输出：{error.message}",
        )
        fallback_text = self._compose_final_text(last_message.content, runtime_notes)
        final_message = ConversationMessage(role="assistant", content=fallback_text)
        self._append_message(messages, final_message)
        return AgentRunResult(
            status="completed",
            final_text=fallback_text,
            steps=steps,
            last_message=final_message,
        )

    def _build_terminal_result(
        self,
        *,
        messages: list[ConversationMessage],
        status: str,
        steps: int,
        fallback_text: str,
        append_message: bool,
        runtime_notes: list[str],
    ) -> AgentRunResult:
        """构造取消等终止态结果。"""

        final_text = self._compose_final_text(fallback_text, runtime_notes)
        terminal_message = ConversationMessage(role="assistant", content=final_text)

        if append_message:
            self._append_message(messages, terminal_message)

        return AgentRunResult(
            status=status,
            final_text=final_text,
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

    @staticmethod
    def _record_runtime_note(runtime_notes: list[str], note: str) -> None:
        """去重记录运行时提示。"""

        if note not in runtime_notes:
            runtime_notes.append(note)

    @staticmethod
    def _format_step_error(*, stage: str, name: str, message: str) -> str:
        """把步骤级失败包装成可回填给模型的 tool 输出。"""

        return (
            f"错误：{name} 在 {stage} 阶段执行失败。\n"
            f"详情：{message}\n"
            "该错误已被保留到当前回合上下文，你可以基于这个错误调整后续动作。"
        )

    @staticmethod
    def _compose_final_text(base_text: str, runtime_notes: list[str]) -> str:
        """把非致命运行时提示附加到最终文本中。"""

        if not runtime_notes:
            return base_text

        lines = [base_text, "", "[运行时提示]"]
        for note in runtime_notes:
            lines.append(f"- {note}")
        return "\n".join(lines)
