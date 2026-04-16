"""最小会话追加日志与主会话恢复。"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .types import ConversationMessage, ToolCall


SUMMARY_MESSAGE_PREFIX = "[上下文压缩摘要]"


@dataclass(slots=True)
class SessionLogger:
    """把会话消息持续追加写入 jsonl 文件。"""

    session_id: str
    path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append_message(self, message: ConversationMessage, scope: str = "parent") -> None:
        """把一条消息追加写入会话日志。"""

        payload = {
            "timestamp": time.time(),
            "session_id": self.session_id,
            "scope": scope,
            "role": message.role,
            "content": message.content,
            "tool_call_id": message.tool_call_id,
            "name": message.name,
            "tool_calls": [
                {
                    "id": tool_call.id,
                    "name": tool_call.name,
                    "arguments": tool_call.arguments,
                }
                for tool_call in message.tool_calls
            ],
        }

        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as file:
                file.write(line + "\n")


@dataclass(slots=True)
class SessionResumeResult:
    """主 Agent 会话恢复结果。"""

    history: list[ConversationMessage]
    path: Path | None
    mode: str = "empty"


def load_latest_parent_history(
    session_dir: Path,
    *,
    max_messages: int = 40,
    prefer_summary: bool = True,
) -> SessionResumeResult:
    """从最近一次 session log 恢复主 Agent 的历史。

    恢复策略：
    - 只读取 `.sessions/` 下最近修改的一个 jsonl
    - 只恢复 `scope=parent` 的消息
    - 默认只恢复最近窗口，而不是整段历史
    - 如果存在最近一次 compact 摘要，优先保留“摘要 + 最近消息”
    """

    if not session_dir.exists():
        return SessionResumeResult(history=[], path=None, mode="empty")

    candidates = sorted(
        session_dir.glob("session_*.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return SessionResumeResult(history=[], path=None, mode="empty")

    latest_path = candidates[0]
    full_history: list[ConversationMessage] = []

    for raw_line in latest_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue

        if payload.get("scope") != "parent":
            continue

        tool_calls = [
            ToolCall(
                id=str(item.get("id", "")),
                name=str(item.get("name", "")),
                arguments=dict(item.get("arguments", {})),  # type: ignore[arg-type]
            )
            for item in payload.get("tool_calls", [])  # type: ignore[arg-type]
        ]

        full_history.append(
            ConversationMessage(
                role=str(payload.get("role", "user")),  # type: ignore[arg-type]
                content=str(payload.get("content", "")),
                tool_call_id=(
                    str(payload["tool_call_id"])
                    if payload.get("tool_call_id") is not None
                    else None
                ),
                name=str(payload["name"]) if payload.get("name") is not None else None,
                tool_calls=tool_calls,
            )
        )

    if not full_history:
        return SessionResumeResult(history=[], path=latest_path, mode="empty")

    max_messages = max(1, max_messages)

    if prefer_summary:
        summary_index = _find_last_summary_index(full_history)
        if summary_index is not None:
            summary_message = full_history[summary_index]
            tail_messages = full_history[summary_index + 1 :]

            restored = [summary_message]
            restored.extend(tail_messages[-(max_messages - 1) :])
            return SessionResumeResult(
                history=restored,
                path=latest_path,
                mode="summary_plus_recent",
            )

    return SessionResumeResult(
        history=full_history[-max_messages:],
        path=latest_path,
        mode="recent_window",
    )


def _find_last_summary_index(messages: list[ConversationMessage]) -> int | None:
    """查找最近一次 compact 摘要消息的位置。"""

    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if (
            message.role == "assistant"
            and message.content.startswith(SUMMARY_MESSAGE_PREFIX)
        ):
            return index
    return None
