"""持久化 agent team 相关工具。"""

from __future__ import annotations

from typing import Any

from ..team import TeamAgentRunner, TeamManager
from .base import BaseTool


class SpawnTeamAgentTool(BaseTool):
    """创建一个持久 teammate。"""

    name = "team_spawn_agent"
    description = (
        "创建一个会跨多轮对话存活的持久 teammate。"
        "适合需要独立身份、独立历史和可持续协作的 Agent。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "teammate 的唯一标识，只能包含字母、数字、下划线和中横线。",
            },
            "role": {
                "type": "string",
                "description": "teammate 的角色，例如 coder、reviewer、researcher。",
            },
            "description": {
                "type": "string",
                "description": "teammate 的职责描述，可选。",
            },
            "system_prompt": {
                "type": "string",
                "description": "这个 teammate 的额外个人说明或长期工作约束，可选。",
            },
        },
        "required": ["agent_id", "role"],
    }

    def __init__(self, manager: TeamManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        return self.manager.create_agent(
            agent_id=str(arguments.get("agent_id", "")).strip(),
            role=str(arguments.get("role", "")).strip(),
            description=str(arguments.get("description", "")),
            system_prompt=str(arguments.get("system_prompt", "")),
        )


class ListTeamAgentsTool(BaseTool):
    """查看当前所有 teammate。"""

    name = "team_list_agents"
    description = "查看当前 team roster、Agent 状态和每个 inbox 的待处理消息数。"
    input_schema = {
        "type": "object",
        "properties": {},
    }

    def __init__(self, manager: TeamManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        return self.manager.list_agents()


class GetTeamAgentTool(BaseTool):
    """查看单个 teammate 的详细信息。"""

    name = "team_get_agent"
    description = "查看单个 teammate 的详细信息。"
    input_schema = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "要查看的 teammate 标识。",
            }
        },
        "required": ["agent_id"],
    }

    def __init__(self, manager: TeamManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        return self.manager.get_agent(str(arguments.get("agent_id", "")).strip())


class SendTeamMessageTool(BaseTool):
    """给其他 teammate 发送一条消息。"""

    name = "team_send_message"
    description = (
        "向某个 teammate 的 inbox 发送消息。"
        "适合分发任务、发送备注或回传结果。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "to_agent": {
                "type": "string",
                "description": "目标 teammate 的 agent_id。",
            },
            "message_type": {
                "type": "string",
                "enum": ["task", "note", "result"],
                "description": "消息类型，默认 task。",
            },
            "content": {
                "type": "string",
                "description": "要发送的消息正文。",
            },
        },
        "required": ["to_agent", "content"],
    }

    def __init__(self, manager: TeamManager, sender_id: str) -> None:
        self.manager = manager
        self.sender_id = sender_id

    def execute(self, arguments: dict[str, Any]) -> str:
        message_type = str(arguments.get("message_type", "task")).strip() or "task"
        if message_type not in {"task", "note", "result"}:
            return "错误：message_type 必须是 task、note 或 result"

        return self.manager.send_message(
            sender=self.sender_id,
            recipient=str(arguments.get("to_agent", "")).strip(),
            message_type=message_type,  # type: ignore[arg-type]
            content=str(arguments.get("content", "")),
        )


class PeekTeamInboxTool(BaseTool):
    """查看某个 teammate 当前 inbox。"""

    name = "team_peek_inbox"
    description = (
        "查看某个 teammate 当前 inbox 中尚未处理的消息。"
        "这是只读查看，不会清空 inbox。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "要查看 inbox 的 teammate 标识。",
            }
        },
        "required": ["agent_id"],
    }

    def __init__(self, manager: TeamManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        return self.manager.list_inbox(str(arguments.get("agent_id", "")).strip())


class RunTeamAgentTool(BaseTool):
    """让某个 teammate 处理自己当前的 inbox。"""

    name = "team_run_agent"
    description = (
        "让某个持久 teammate 读取并处理自己当前 inbox 中的消息。"
        "teammate 会沿用自己的持久历史运行，完成后会自动把结果回发给原发送方。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "要运行的 teammate 标识。",
            }
        },
        "required": ["agent_id"],
    }

    def __init__(self, runner: TeamAgentRunner) -> None:
        self.runner = runner

    def execute(self, arguments: dict[str, Any]) -> str:
        return self.runner.run_once(str(arguments.get("agent_id", "")).strip())


class ShutdownTeamAgentTool(BaseTool):
    """关闭一个 teammate。"""

    name = "team_shutdown_agent"
    description = "把某个 teammate 标记为 shutdown，后续不再允许运行。"
    input_schema = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "要关闭的 teammate 标识。",
            }
        },
        "required": ["agent_id"],
    }

    def __init__(self, manager: TeamManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        return self.manager.shutdown_agent(str(arguments.get("agent_id", "")).strip())
