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

from .types import ConversationMessage


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
