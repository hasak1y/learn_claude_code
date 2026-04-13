"""持久化 Agent team 的最小实现。

这一层解决三件事：

1. Agent 身份与生命周期管理
   - 每个 teammate 都有固定的 `agent_id`
   - 生命周期是 `idle -> working -> idle`，并支持 `shutdown`

2. Agent 之间的文件式通信
   - 每个 Agent 一个 inbox 文件
   - 发送消息时只做 append
   - 运行某个 Agent 时统一 drain 自己的 inbox

3. 跨多轮会话存活
   - 每个 Agent 的历史单独持久化到磁盘
   - 下次再运行时会继续沿用这份历史

这套设计故意不直接改 AgentLoop。
AgentLoop 仍然只负责“单个 Agent 的一次循环”，
team 层则把“这个 Agent 是谁、收到什么消息、处理完怎么回信”包在外面。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from .agent import AgentLoop
from .compaction import ConversationCompactor
from .llm.base import BaseLLMClient
from .session_log import SessionLogger
from .tools.base import ToolRegistry
from .types import AgentRunResult, ConversationMessage, ToolCall


TeamAgentStatus = Literal["idle", "working", "shutdown"]
TeamMessageType = Literal["task", "note", "result"]


@dataclass(slots=True)
class TeamAgentRecord:
    """单个 teammate 的持久化元数据。"""

    agent_id: str
    role: str
    description: str
    system_prompt: str
    status: TeamAgentStatus
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, object]:
        """转成可写入 JSON 的结构。"""

        return {
            "agentId": self.agent_id,
            "role": self.role,
            "description": self.description,
            "systemPrompt": self.system_prompt,
            "status": self.status,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "TeamAgentRecord":
        """从 JSON 数据恢复 teammate 元数据。"""

        return cls(
            agent_id=str(data.get("agentId", "")),
            role=str(data.get("role", "")),
            description=str(data.get("description", "")),
            system_prompt=str(data.get("systemPrompt", "")),
            status=str(data.get("status", "idle")),  # type: ignore[arg-type]
            created_at=float(data.get("createdAt", time.time())),
            updated_at=float(data.get("updatedAt", time.time())),
        )


@dataclass(slots=True)
class TeamMessage:
    """Agent 之间传递的一条消息。"""

    message_id: str
    sender: str
    recipient: str
    message_type: TeamMessageType
    content: str
    created_at: float

    def to_dict(self) -> dict[str, object]:
        """转成可写入 JSONL 的结构。"""

        return {
            "messageId": self.message_id,
            "from": self.sender,
            "to": self.recipient,
            "type": self.message_type,
            "content": self.content,
            "createdAt": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "TeamMessage":
        """从 JSON 数据恢复消息。"""

        return cls(
            message_id=str(data.get("messageId", "")),
            sender=str(data.get("from", "")),
            recipient=str(data.get("to", "")),
            message_type=str(data.get("type", "note")),  # type: ignore[arg-type]
            content=str(data.get("content", "")),
            created_at=float(data.get("createdAt", time.time())),
        )


class MessageBus:
    """基于 `.team/inbox/*.jsonl` 的最小消息总线。"""

    def __init__(self, inbox_dir: Path) -> None:
        self.inbox_dir = inbox_dir
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self._next_message_id = self._max_message_id() + 1

    def send_message(
        self,
        sender: str,
        recipient: str,
        message_type: TeamMessageType,
        content: str,
    ) -> TeamMessage:
        """往目标 Agent 的 inbox 追加一条消息。"""

        message = TeamMessage(
            message_id=f"msg-{self._next_message_id:04d}",
            sender=sender,
            recipient=recipient,
            message_type=message_type,
            content=content,
            created_at=time.time(),
        )
        self._next_message_id += 1

        path = self._inbox_path(recipient)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(message.to_dict(), ensure_ascii=False) + "\n")

        return message

    def peek_inbox(self, agent_id: str) -> list[TeamMessage]:
        """读取 inbox，但不清空。"""

        path = self._inbox_path(agent_id)
        if not path.exists():
            return []

        messages: list[TeamMessage] = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            messages.append(TeamMessage.from_dict(json.loads(line)))
        return messages

    def drain_inbox(self, agent_id: str) -> list[TeamMessage]:
        """读取并清空 inbox。"""

        messages = self.peek_inbox(agent_id)
        path = self._inbox_path(agent_id)
        if path.exists():
            path.write_text("", encoding="utf-8")
        return messages

    def count_pending(self, agent_id: str) -> int:
        """返回某个 Agent 当前未处理消息数。"""

        return len(self.peek_inbox(agent_id))

    def _inbox_path(self, agent_id: str) -> Path:
        """返回某个 Agent 的 inbox 路径。"""

        return self.inbox_dir / f"{agent_id}.jsonl"

    def _max_message_id(self) -> int:
        """扫描现有 inbox 中最大的 message id。"""

        max_id = 0
        for path in self.inbox_dir.glob("*.jsonl"):
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                    message_id = str(payload.get("messageId", ""))
                    max_id = max(max_id, int(message_id.split("-", 1)[1]))
                except (ValueError, IndexError, json.JSONDecodeError):
                    continue
        return max_id


class TeamManager:
    """管理 `.team/` 目录下的 roster、Agent 元数据、历史与消息总线。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.agents_dir = self.root / "agents"
        self.inbox_dir = self.root / "inbox"
        self.history_dir = self.root / "history"
        self.sessions_dir = self.root / "sessions"
        self.config_path = self.root / "config.json"

        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        self.bus = MessageBus(self.inbox_dir)
        self._ensure_config()
        self._ensure_lead_agent()

    def create_agent(
        self,
        agent_id: str,
        role: str,
        description: str = "",
        system_prompt: str = "",
    ) -> str:
        """创建一个新的持久 teammate。"""

        clean_agent_id = agent_id.strip()
        if not self._is_valid_agent_id(clean_agent_id):
            return "错误：agent_id 只能包含字母、数字、下划线和中横线"
        if self._agent_path(clean_agent_id).exists():
            return f"错误：Agent '{clean_agent_id}' 已存在"

        clean_role = role.strip()
        if not clean_role:
            return "错误：role 不能为空"

        now = time.time()
        record = TeamAgentRecord(
            agent_id=clean_agent_id,
            role=clean_role,
            description=description,
            system_prompt=system_prompt,
            status="idle",
            created_at=now,
            updated_at=now,
        )
        self._save_agent(record)
        self._save_history(clean_agent_id, [])
        self._write_config()
        return "已创建 teammate：\n" + self._format_agent(record)

    def list_agents(self) -> str:
        """列出当前 team roster。"""

        agents = self._all_agents()
        if not agents:
            return "当前没有 teammate。"

        lines = ["当前 team roster："]
        for agent in agents:
            pending = self.bus.count_pending(agent.agent_id)
            lines.append(
                f"- {agent.agent_id} | role={agent.role} | status={agent.status} | pending_messages={pending}"
            )
        return "\n".join(lines)

    def get_agent(self, agent_id: str) -> str:
        """查看单个 Agent 的元数据。"""

        try:
            agent = self._load_agent(agent_id)
        except ValueError as exc:
            return f"错误：{exc}"

        return self._format_agent(agent)

    def shutdown_agent(self, agent_id: str) -> str:
        """将某个 Agent 标记为 shutdown。"""

        try:
            agent = self._load_agent(agent_id)
        except ValueError as exc:
            return f"错误：{exc}"

        if agent.agent_id == "lead":
            return "错误：内建 lead 不能被 shutdown"

        agent.status = "shutdown"
        agent.updated_at = time.time()
        self._save_agent(agent)
        self._write_config()
        return f"已将 teammate '{agent_id}' 标记为 shutdown。"

    def set_status(self, agent_id: str, status: TeamAgentStatus) -> None:
        """更新某个 Agent 的生命周期状态。"""

        agent = self._load_agent(agent_id)
        agent.status = status
        agent.updated_at = time.time()
        self._save_agent(agent)
        self._write_config()

    def send_message(
        self,
        sender: str,
        recipient: str,
        message_type: TeamMessageType,
        content: str,
    ) -> str:
        """向 teammate 发送一条消息。"""

        if not self._agent_path(recipient).exists():
            return f"错误：目标 Agent '{recipient}' 不存在"

        if not self._agent_path(sender).exists():
            return f"错误：发送方 Agent '{sender}' 不存在"

        recipient_agent = self._load_agent(recipient)
        if recipient_agent.status == "shutdown":
            return f"错误：目标 Agent '{recipient}' 已 shutdown"

        message = self.bus.send_message(
            sender=sender,
            recipient=recipient,
            message_type=message_type,
            content=content,
        )
        self._write_config()
        return (
            "已发送 team 消息：\n"
            f"- message_id: {message.message_id}\n"
            f"- from: {sender}\n"
            f"- to: {recipient}\n"
            f"- type: {message.message_type}"
        )

    def list_inbox(self, agent_id: str) -> str:
        """查看某个 Agent 当前 inbox 中的待处理消息。"""

        try:
            self._load_agent(agent_id)
        except ValueError as exc:
            return f"错误：{exc}"

        messages = self.bus.peek_inbox(agent_id)
        if not messages:
            return f"Agent '{agent_id}' 的 inbox 为空。"

        lines = [f"Agent '{agent_id}' 当前 inbox："]
        for message in messages:
            lines.append(
                f"- {message.message_id} | from={message.sender} | type={message.message_type} | content={message.content[:80]}"
            )
        return "\n".join(lines)

    def drain_inbox(self, agent_id: str) -> list[TeamMessage]:
        """供运行器读取并清空 inbox。"""

        return self.bus.drain_inbox(agent_id)

    def load_history(self, agent_id: str) -> list[ConversationMessage]:
        """读取某个 Agent 的持久化历史。"""

        path = self.history_dir / f"{agent_id}.json"
        if not path.exists():
            return []

        payload = json.loads(path.read_text(encoding="utf-8"))
        return [self._deserialize_message(item) for item in payload]

    def save_history(self, agent_id: str, messages: list[ConversationMessage]) -> None:
        """保存某个 Agent 的完整历史。"""

        self._save_history(agent_id, messages)

    def build_session_logger(self, agent_id: str) -> SessionLogger:
        """为某个 teammate 构建独立 session log。"""

        return SessionLogger(
            session_id=f"team_{agent_id}",
            path=self.sessions_dir / f"{agent_id}.jsonl",
        )

    def build_agent_system_prompt(self, agent_id: str, base_prompt: str) -> str:
        """为某个 teammate 生成带身份信息的 system prompt。"""

        agent = self._load_agent(agent_id)
        identity_prompt = (
            "你是一个持久 teammate，不是主交互 Agent。\n"
            f"你的 agent_id 是：{agent.agent_id}\n"
            f"你的角色是：{agent.role}\n"
            f"你的职责描述：{agent.description or '（未填写）'}\n"
            "你会保留自己的历史和身份，来自 inbox 的 team 消息会被追加进你的上下文。\n"
            "如果需要把结论、阻塞或请求发给其他 Agent，请使用 team_send_message。\n"
            "不要尝试创建、关闭或直接运行其他 teammate，除非外层显式提供了那类工具。\n"
        )
        if agent.system_prompt.strip():
            identity_prompt += f"\n额外个人说明：\n{agent.system_prompt.strip()}\n"
        return identity_prompt + "\n" + base_prompt

    def _ensure_config(self) -> None:
        """确保 config.json 至少存在一个空骨架。"""

        if not self.config_path.exists():
            self.config_path.write_text(
                json.dumps({"version": 1, "agents": {}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _ensure_lead_agent(self) -> None:
        """确保内建 lead 总是存在。"""

        if self._agent_path("lead").exists():
            self._write_config()
            return

        now = time.time()
        lead = TeamAgentRecord(
            agent_id="lead",
            role="lead",
            description="主交互 Agent，用来创建 teammate、分发消息和读取结果。",
            system_prompt="",
            status="idle",
            created_at=now,
            updated_at=now,
        )
        self._save_agent(lead)
        self._save_history("lead", [])
        self._write_config()

    def _save_history(self, agent_id: str, messages: list[ConversationMessage]) -> None:
        """把消息历史完整写回磁盘。"""

        payload = [self._serialize_message(message) for message in messages]
        (self.history_dir / f"{agent_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_config(self) -> None:
        """把当前 roster 摘要写入 `.team/config.json`。"""

        payload = {
            "version": 1,
            "agents": {
                agent.agent_id: {
                    "role": agent.role,
                    "description": agent.description,
                    "status": agent.status,
                    "pendingMessages": self.bus.count_pending(agent.agent_id),
                    "updatedAt": agent.updated_at,
                }
                for agent in self._all_agents()
            },
        }
        self.config_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _all_agents(self) -> list[TeamAgentRecord]:
        """读取全部 teammate。"""

        agents = [
            TeamAgentRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            for path in self.agents_dir.glob("*.json")
        ]
        agents.sort(key=lambda item: item.agent_id)
        return agents

    def _load_agent(self, agent_id: str) -> TeamAgentRecord:
        """读取单个 teammate。"""

        path = self._agent_path(agent_id)
        if not path.exists():
            raise ValueError(f"Agent '{agent_id}' 不存在")
        return TeamAgentRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _save_agent(self, agent: TeamAgentRecord) -> None:
        """保存单个 teammate 元数据。"""

        self._agent_path(agent.agent_id).write_text(
            json.dumps(agent.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _agent_path(self, agent_id: str) -> Path:
        """返回单个 teammate 元数据文件路径。"""

        return self.agents_dir / f"{agent_id}.json"

    @staticmethod
    def _serialize_message(message: ConversationMessage) -> dict[str, object]:
        """把对话消息写成 JSON 结构。"""

        return {
            "role": message.role,
            "content": message.content,
            "toolCallId": message.tool_call_id,
            "name": message.name,
            "toolCalls": [
                {
                    "id": tool_call.id,
                    "name": tool_call.name,
                    "arguments": tool_call.arguments,
                }
                for tool_call in message.tool_calls
            ],
        }

    @staticmethod
    def _deserialize_message(data: dict[str, object]) -> ConversationMessage:
        """从 JSON 结构恢复对话消息。"""

        return ConversationMessage(
            role=str(data.get("role", "user")),  # type: ignore[arg-type]
            content=str(data.get("content", "")),
            tool_call_id=str(data["toolCallId"]) if data.get("toolCallId") is not None else None,
            name=str(data["name"]) if data.get("name") is not None else None,
            tool_calls=[
                ToolCall(
                    id=str(item.get("id", "")),
                    name=str(item.get("name", "")),
                    arguments=dict(item.get("arguments", {})),  # type: ignore[arg-type]
                )
                for item in data.get("toolCalls", [])  # type: ignore[arg-type]
            ],
        )

    @staticmethod
    def _format_agent(agent: TeamAgentRecord) -> str:
        """格式化单个 teammate。"""

        return json.dumps(agent.to_dict(), ensure_ascii=False, indent=2)

    @staticmethod
    def _is_valid_agent_id(agent_id: str) -> bool:
        """限制 Agent 标识符字符集，避免文件名和消息路由歧义。"""

        if not agent_id:
            return False
        return all(char.isalnum() or char in {"_", "-"} for char in agent_id)


@dataclass(slots=True)
class TeamAgentRunner:
    """读取 teammate inbox、运行其循环并自动回信的执行器。"""

    team_manager: TeamManager
    llm_client_factory: Callable[[], BaseLLMClient]
    tool_registry_factory: Callable[[str], ToolRegistry]
    base_system_prompt_builder: Callable[[str], str]
    max_steps: int = 8
    compactor: ConversationCompactor | None = None

    def run_once(self, agent_id: str) -> str:
        """让某个 teammate 处理自己当前 inbox 中的消息。"""

        try:
            agent = self.team_manager._load_agent(agent_id)
        except ValueError as exc:
            return f"错误：{exc}"

        if agent.status == "shutdown":
            return f"错误：Agent '{agent_id}' 已 shutdown"
        if agent.status == "working":
            return f"错误：Agent '{agent_id}' 当前正在 working"

        inbox_messages = self.team_manager.drain_inbox(agent_id)
        self.team_manager._write_config()
        if not inbox_messages:
            return f"Agent '{agent_id}' 的 inbox 为空，无需运行。"

        history = self.team_manager.load_history(agent_id)
        # 清理历史里的孤立 tool 消息，避免服务端报
        # "No tool call found for function call output"。
        history = self._sanitize_history_for_tools(history)
        session_logger = self.team_manager.build_session_logger(agent_id)

        for inbound in inbox_messages:
            user_message = ConversationMessage(
                role="user",
                content=(
                    "<team_message>\n"
                    f"from: {inbound.sender}\n"
                    f"type: {inbound.message_type}\n"
                    f"content:\n{inbound.content}\n"
                    "</team_message>"
                ),
            )
            history.append(user_message)
            session_logger.append_message(user_message, scope=f"team:{agent_id}:inbox")

        self.team_manager.set_status(agent_id, "working")
        try:
            system_prompt = self.team_manager.build_agent_system_prompt(
                agent_id=agent_id,
                base_prompt=self.base_system_prompt_builder(agent_id),
            )
            run_result = AgentLoop(
                llm_client=self.llm_client_factory(),
                tool_registry=self.tool_registry_factory(agent_id),
                system_prompt=system_prompt,
                max_steps=self.max_steps,
                echo_tool_calls=False,
                compactor=self.compactor,
                session_logger=session_logger,
                log_scope=f"team:{agent_id}",
            ).run(history)
        except RuntimeError as exc:
            run_result = AgentRunResult(
                status="failed",
                final_text=f"teammate '{agent_id}' 运行失败：{exc}",
                steps=0,
                last_message=None,
                error=str(exc),
            )
        finally:
            # 无论本轮执行成功还是失败，都把最新历史和生命周期状态落回磁盘，
            # 避免 teammate 因中途异常而永久卡在 working。
            self.team_manager.save_history(agent_id, history)
            self.team_manager.set_status(agent_id, "idle")

        reply_senders = {
            message.sender
            for message in inbox_messages
            if message.sender != agent_id and message.message_type != "result"
        }
        for sender in sorted(reply_senders):
            self.team_manager.send_message(
                sender=agent_id,
                recipient=sender,
                message_type="result",
                content=run_result.final_text,
            )

        if reply_senders:
            recipients = ", ".join(sorted(reply_senders))
            return (
                f"已运行 teammate '{agent_id}'，处理消息 {len(inbox_messages)} 条。\n"
                f"运行状态：{run_result.status}\n"
                f"已自动把结果回发给：{recipients}\n"
                f"结果摘要：\n{run_result.final_text}"
            )

        return (
            f"已运行 teammate '{agent_id}'，处理消息 {len(inbox_messages)} 条。\n"
            f"运行状态：{run_result.status}\n"
            f"结果摘要：\n{run_result.final_text}"
        )

    @staticmethod
    def _sanitize_history_for_tools(
        messages: list[ConversationMessage],
    ) -> list[ConversationMessage]:
        """移除没有匹配 tool_calls 的孤立 tool 消息。"""

        sanitized: list[ConversationMessage] = []
        open_tool_calls: set[str] = set()

        for message in messages:
            if message.role == "assistant" and message.tool_calls:
                for tool_call in message.tool_calls:
                    open_tool_calls.add(tool_call.id)
                sanitized.append(message)
                continue

            if message.role == "tool":
                if message.tool_call_id and message.tool_call_id in open_tool_calls:
                    open_tool_calls.remove(message.tool_call_id)
                    sanitized.append(message)
                # 没有匹配的 tool_call_id 就丢弃，避免请求不一致
                continue

            sanitized.append(message)

        return sanitized
