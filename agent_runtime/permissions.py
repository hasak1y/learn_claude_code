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

import os
from fnmatch import fnmatchcase
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
        self._deny_tool_patterns = self._load_patterns("PERMISSION_DENY_TOOL_PATTERNS")
        self._allow_tool_patterns = self._load_patterns("PERMISSION_ALLOW_TOOL_PATTERNS")

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

        if self._matches_tool_name(tool_call.name, self._deny_tool_patterns):
            return "工具命中硬拒绝规则。"

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
            if self._is_mcp_tool(tool_call.name) and not self._matches_tool_name(
                tool_call.name,
                self._allow_tool_patterns,
            ):
                return "read_only 模式下，MCP 工具默认不直接执行，除非显式加入允许列表。"

        return None

    def _allow_rules(self, tool_call: ToolCall) -> str | None:
        """放行规则。"""

        # dangerous 模式的语义是：除了命中硬拒绝规则的操作外，其余默认允许。
        # 这适合本地全权限开发环境，避免每次 teammate / review / shell 都反复确认。
        if self.mode == "dangerous":
            return "dangerous 模式允许该操作。"

        if self._matches_tool_name(tool_call.name, self._allow_tool_patterns):
            return "工具命中显式允许规则。"

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

    @staticmethod
    def _load_patterns(env_name: str) -> tuple[str, ...]:
        """从环境变量读取工具名匹配规则。

        约定格式：
        - 逗号分隔
        - 支持 shell 风格通配符
        例如：
        - mcp.github.*
        - mcp.github.get_*
        - mcp.figma.*
        """

        raw = os.getenv(env_name, "").strip()
        if not raw:
            return ()
        return tuple(
            item.strip()
            for item in raw.split(",")
            if item.strip()
        )

    @staticmethod
    def _matches_tool_name(tool_name: str, patterns: tuple[str, ...]) -> bool:
        """判断工具名是否匹配任意一条通配符规则。

        这里同时支持两类写法：
        - 实际注册给模型的安全名：mcp__github__search_prs
        - 逻辑上的 canonical 名：mcp.github.search_prs
        """

        candidates = {tool_name}
        canonical_name = PermissionPolicy._to_canonical_tool_name(tool_name)
        if canonical_name:
            candidates.add(canonical_name)
        return any(
            fnmatchcase(candidate, pattern)
            for pattern in patterns
            for candidate in candidates
        )

    @staticmethod
    def _is_mcp_tool(tool_name: str) -> bool:
        return tool_name.startswith("mcp.") or tool_name.startswith("mcp__")

    @staticmethod
    def _to_canonical_tool_name(tool_name: str) -> str | None:
        """把安全工具名恢复成逻辑工具名，便于权限规则仍按点号书写。"""

        if tool_name.startswith("mcp__"):
            parts = tool_name.split("__", 2)
            if len(parts) == 3 and parts[1] and parts[2]:
                return f"mcp.{parts[1]}.{parts[2]}"
        if tool_name.startswith("mcp."):
            return tool_name
        return None


ApprovalCallback = Callable[[ToolCall, PermissionCheckResult], bool]
