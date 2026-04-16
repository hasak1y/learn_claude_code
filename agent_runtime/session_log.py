"""最小会话追加日志。

这一层只做一件事：
- 每当新的 ConversationMessage 进入会话历史，就把它追加写入 `.sessions/<session_id>.jsonl`

当前不实现：
- 恢复会话
- 索引
- 数据库
- 搜索
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .types import ConversationMessage, ToolCall


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


def load_latest_parent_history(session_dir: Path) -> tuple[list[ConversationMessage], Path | None]:
    """从最近一次 session log 恢复主 Agent 的历史。

    这是“最小持久记忆恢复”：
    - 只读取 `.sessions/` 下最近修改的一个 jsonl
    - 只恢复 `scope=parent` 的消息
    - 把日志重新还原成 ConversationMessage 列表
    """

    if not session_dir.exists():
        return [], None

    candidates = sorted(
        session_dir.glob("session_*.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return [], None

    latest_path = candidates[0]
    history: list[ConversationMessage] = []

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

        history.append(
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

    return history, latest_path
