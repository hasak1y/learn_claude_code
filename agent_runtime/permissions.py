"""最小权限与审查机制。

目标：
1. 允许把敏感操作拦在执行前。
2. 一旦触发敏感动作，交给用户确认。
3. 支持简单的“模式 + 规则”分层。

执行流程：
1) deny rules
2) mode check
3) allow rules
4) ask user
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from .types import ToolCall


PermissionMode = Literal["read_only", "dev_safe", "dangerous"]
PermissionDecision = Literal["allow", "deny", "ask"]


@dataclass(slots=True)
class PermissionCheckResult:
    """权限检查结果。"""

    decision: PermissionDecision
    reason: str


class PermissionPolicy:
    """最小权限策略。"""

    def __init__(self, mode: PermissionMode = "dev_safe") -> None:
        self.mode = mode

    def evaluate(self, tool_call: ToolCall) -> PermissionCheckResult:
        """按 deny -> mode -> allow -> ask 的顺序进行评估。"""

        deny_reason = self._deny_rules(tool_call)
        if deny_reason:
            return PermissionCheckResult(decision="deny", reason=deny_reason)

        mode_reason = self._mode_check(tool_call)
        if mode_reason:
            return PermissionCheckResult(decision="deny", reason=mode_reason)

        allow_reason = self._allow_rules(tool_call)
        if allow_reason:
            return PermissionCheckResult(decision="allow", reason=allow_reason)

        return PermissionCheckResult(
            decision="ask",
            reason="操作敏感度较高，需要用户确认。",
        )

    def _deny_rules(self, tool_call: ToolCall) -> str | None:
        """硬拒绝规则。"""

        if tool_call.name in {"shell", "shell_background"}:
            command = str(tool_call.arguments.get("command", "")).lower()
            dangerous_fragments = [
                "rm -rf /",
                "format ",
                "del /s",
                "del /q",
                "rd /s",
                "rmdir /s",
                "shutdown",
                "reboot",
                "mkfs",
                "> /dev/",
            ]
            if any(fragment in command for fragment in dangerous_fragments):
                return "命令命中硬拒绝规则。"

        return None

    def _mode_check(self, tool_call: ToolCall) -> str | None:
        """根据模式限制敏感工具。"""

        if self.mode == "read_only":
            if tool_call.name in {"write_file", "edit_file", "shell", "shell_background"}:
                return "read_only 模式禁止写入或执行 shell。"

        return None

    def _allow_rules(self, tool_call: ToolCall) -> str | None:
        """放行规则。"""

        if tool_call.name in {"read_file", "task", "compact", "todo", "task_create", "task_update"}:
            return "工具属于低风险操作。"

        if tool_call.name in {"team_spawn_agent", "team_list_agents", "team_get_agent", "team_peek_inbox"}:
            return "team 管理类操作允许直接执行。"

        if tool_call.name == "shell":
            command = str(tool_call.arguments.get("command", "")).strip().lower()
            safe_prefixes = (
                "dir",
                "ls",
                "rg ",
                "type ",
                "cat ",
                "git status",
                "git diff",
                "python -c",
                "python ",
            )
            if command.startswith(safe_prefixes):
                return "命令匹配安全白名单。"

        if tool_call.name in {"write_file", "edit_file"}:
            if self.mode in {"dev_safe", "dangerous"}:
                return "允许文件写入操作。"

        if tool_call.name == "shell_background":
            command = str(tool_call.arguments.get("command", "")).strip().lower()
            safe_prefixes = (
                "dir",
                "ls",
                "rg ",
                "type ",
                "cat ",
                "git status",
                "git diff",
                "python -c",
                "python ",
            )
            if command.startswith(safe_prefixes):
                return "后台命令匹配安全白名单。"
            if self.mode == "dangerous":
                return "dangerous 模式允许后台命令执行。"

        return None


ApprovalCallback = Callable[[ToolCall, PermissionCheckResult], bool]
