"""最近原始对话日志。

这一层和会话恢复是分开的：
- `.sessions/` 记录完整运行日志，供恢复主历史使用。
- `.chat_history/recent_dialogue.jsonl` 只保留最近若干条原始 user/assistant 对话，
  供自动记忆提取使用。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class DialogueRecord:
    """一条原始对话记录。"""

    timestamp: float
    session_id: str
    role: str
    content: str


@dataclass(slots=True)
class RecentDialogueStore:
    """维护最近 N 条未压缩的原始对话。

    这里故意只记录 user / assistant 文本，不记录工具消息。
    这样做有两个好处：
    1. 自动记忆提取时噪音更少。
    2. 文件体积可控，不会被大段工具输出污染。
    """

    path: Path
    max_messages: int = 100
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append_message(self, *, role: str, content: str, session_id: str) -> None:
        """追加一条消息，并裁剪到最近窗口。"""

        if role not in {"user", "assistant"}:
            return

        record = DialogueRecord(
            timestamp=time.time(),
            session_id=session_id,
            role=role,
            content=content,
        )

        with self._lock:
            records = self.load_recent()
            records.append(record)
            records = records[-self.max_messages :]
            with self.path.open("w", encoding="utf-8") as file:
                for item in records:
                    file.write(
                        json.dumps(
                            {
                                "timestamp": item.timestamp,
                                "session_id": item.session_id,
                                "role": item.role,
                                "content": item.content,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )

    def load_recent(self, limit: int | None = None) -> list[DialogueRecord]:
        """读取最近若干条原始对话。"""

        if not self.path.exists():
            return []

        records: list[DialogueRecord] = []
        for raw_line in self.path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue

            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue

            records.append(
                DialogueRecord(
                    timestamp=float(payload.get("timestamp", 0.0)),
                    session_id=str(payload.get("session_id", "")),
                    role=str(payload.get("role", "")),
                    content=str(payload.get("content", "")),
                )
            )

        if limit is None:
            return records
        return records[-limit:]
