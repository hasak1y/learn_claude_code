"""当前 runtime 使用的最小 hook 实现。"""

from __future__ import annotations

from .background_jobs import BackgroundJobManager
from .hooks import HookContext, HookResult
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
