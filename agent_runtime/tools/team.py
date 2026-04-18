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
            "auto_pull_tasks": {
                "type": "boolean",
                "description": "是否允许该 teammate 在空闲且 inbox 为空时，自动从任务图中认领下一项匹配自己角色的 ready 任务。",
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
            auto_pull_tasks=bool(arguments.get("auto_pull_tasks", False)),
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


class SendTeamProtocolTool(BaseTool):
    """创建一条需要请求-响应追踪的 protocol request。"""

    name = "team_send_protocol"
    description = (
        "创建一条带 request_id 的协议请求，并投递到目标 teammate 的 inbox。"
        "适合审批、关机请求、交接和签收。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "to_agent": {
                "type": "string",
                "description": "目标 teammate 的 agent_id。",
            },
            "action": {
                "type": "string",
                "enum": [
                    "approval_request",
                    "shutdown_request",
                    "handoff_request",
                    "ack_request",
                    "integration_request",
                ],
                "description": "协议动作类型。",
            },
            "summary": {
                "type": "string",
                "description": "供对方快速理解请求目的的短摘要。",
            },
            "content": {
                "type": "string",
                "description": "详细说明或上下文。",
            },
        },
        "required": ["to_agent", "action", "summary"],
    }

    def __init__(self, manager: TeamManager, sender_id: str) -> None:
        self.manager = manager
        self.sender_id = sender_id

    def execute(self, arguments: dict[str, Any]) -> str:
        return self.manager.create_request(
            sender=self.sender_id,
            recipient=str(arguments.get("to_agent", "")).strip(),
            action=str(arguments.get("action", "approval_request")).strip(),  # type: ignore[arg-type]
            summary=str(arguments.get("summary", "")).strip(),
            content=str(arguments.get("content", "")),
        )


class RespondTeamProtocolTool(BaseTool):
    """更新一条 protocol request 的状态，并在需要时回发 response。"""

    name = "team_respond_protocol"
    description = (
        "更新某条 protocol request 的状态，例如 acknowledged、approved、rejected、completed。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "request_id": {
                "type": "string",
                "description": "要更新的 request_id。",
            },
            "status": {
                "type": "string",
                "enum": [
                    "acknowledged",
                    "approved",
                    "rejected",
                    "changes_requested",
                    "completed",
                    "cancelled",
                    "failed",
                ],
                "description": "新的请求状态。",
            },
            "response_text": {
                "type": "string",
                "description": "给请求方看的响应文本，可选。",
            },
        },
        "required": ["request_id", "status"],
    }

    def __init__(self, manager: TeamManager, sender_id: str) -> None:
        self.manager = manager
        self.sender_id = sender_id

    def execute(self, arguments: dict[str, Any]) -> str:
        return self.manager.respond_request(
            responder=self.sender_id,
            request_id=str(arguments.get("request_id", "")).strip(),
            status=str(arguments.get("status", "")).strip(),  # type: ignore[arg-type]
            response_text=str(arguments.get("response_text", "")),
        )


class GetTeamRequestTool(BaseTool):
    """查看单条 protocol request。"""

    name = "team_get_request"
    description = "查看某条 protocol request 的完整追踪记录。"
    input_schema = {
        "type": "object",
        "properties": {
            "request_id": {
                "type": "string",
                "description": "要查看的 request_id。",
            }
        },
        "required": ["request_id"],
    }

    def __init__(self, manager: TeamManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        return self.manager.get_request(str(arguments.get("request_id", "")).strip())


class ListTeamRequestsTool(BaseTool):
    """列出 protocol requests。"""

    name = "team_list_requests"
    description = "列出当前 protocol requests，可按 agent_id 粗过滤。"
    input_schema = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "可选，只列出与某个 Agent 相关的请求。",
            }
        },
    }

    def __init__(self, manager: TeamManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        agent_id = str(arguments.get("agent_id", "")).strip() or None
        return self.manager.list_requests(agent_id=agent_id)


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
