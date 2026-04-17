"""当前 runtime 使用的 hook 实现。"""

from __future__ import annotations

from .background_jobs import BackgroundJobManager
from .hooks import HookContext, HookResult
from .llm.base import BaseLLMClient
from pathlib import Path

from .memory import AutoMemoryManager, LearnClaudeContextLoader
from .permissions import ApprovalCallback, PermissionPolicy
from .types import ConversationMessage


class PermissionHook:
    """在工具执行前做权限审查与用户确认。"""

    def __init__(
        self,
        permission_policy: PermissionPolicy,
        approval_callback: ApprovalCallback | None = None,
    ) -> None:
        self.permission_policy = permission_policy
        self.approval_callback = approval_callback

    def handle(self, ctx: HookContext) -> HookResult:
        if ctx.event != "before_tool_execute" or ctx.tool_call is None:
            return HookResult()

        result = self.permission_policy.evaluate(ctx.tool_call)
        if result.decision == "deny":
            return HookResult(decision="abort", reason=f"已拒绝执行：{result.reason}")

        if result.decision == "allow":
            return HookResult()

        if self.approval_callback is None:
            return HookResult(
                decision="abort",
                reason="需要用户确认，但当前没有可用的确认回调。",
            )

        approved = self.approval_callback(ctx.tool_call, result)
        if not approved:
            return HookResult(decision="abort", reason="用户拒绝执行该操作。")

        return HookResult()


class BackgroundJobHook:
    """在发起下一轮 LLM 请求前，把已完成后台任务结果注入历史。"""

    def __init__(self, manager: BackgroundJobManager) -> None:
        self.manager = manager

    def handle(self, ctx: HookContext) -> HookResult:
        if ctx.event != "before_llm_request":
            return HookResult()

        events = self.manager.drain_completed_events()
        if not events:
            return HookResult()

        messages = [
            ConversationMessage(
                role="user",
                content=(
                    "<background_job_result>\n"
                    f"job_id: {event.job_id}\n"
                    f"status: {event.status}\n"
                    f"command: {event.command}\n"
                    f"output:\n{event.output}\n"
                    "</background_job_result>"
                ),
            )
            for event in events
        ]
        return HookResult(append_messages=messages)


class MemoryRetrievalHook:
    """在每轮用户请求前按需加载长期经验。

    这里故意只在“最后一条消息来自用户”时触发，
    避免工具往返过程里重复检索同一批 memory topic。
    """

    def __init__(self, manager: AutoMemoryManager, llm_client: BaseLLMClient) -> None:
        self.manager = manager
        self.llm_client = llm_client

    def handle(self, ctx: HookContext) -> HookResult:
        if ctx.event != "before_llm_request" or not ctx.messages:
            return HookResult()

        last_message = ctx.messages[-1]
        if last_message.role != "user" or not last_message.content.strip():
            return HookResult()

        request_messages = self.manager.build_request_messages_for_query(
            query=last_message.content,
            llm_client=self.llm_client,
            top_k=5,
        )
        if not request_messages:
            return HookResult()

        return HookResult(request_messages=request_messages)


class PathScopedRuleHook:
    """在访问具体路径后，按需激活该路径上的子目录规则。"""

    def __init__(self, loader: LearnClaudeContextLoader, cwd: Path) -> None:
        self.loader = loader
        self.cwd = cwd.resolve()
        self._activated_dirs: set[Path] = set()

    def handle(self, ctx: HookContext) -> HookResult:
        if ctx.event != "after_tool_execute" or ctx.tool_call is None:
            return HookResult()

        if ctx.tool_call.name not in {"read_file", "write_file", "edit_file"}:
            return HookResult()

        raw_path = ctx.tool_call.arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return HookResult()

        resolved_path = (self.cwd / raw_path).resolve()
        target_dir = resolved_path if resolved_path.is_dir() else resolved_path.parent
        if target_dir in self._activated_dirs:
            return HookResult()

        activated_text = self.loader.render_path_scoped_for_target(resolved_path)
        if not activated_text:
            return HookResult()

        self._activated_dirs.add(target_dir)
        return HookResult(
            append_messages=[
                ConversationMessage(role="user", content=activated_text),
            ]
        )
