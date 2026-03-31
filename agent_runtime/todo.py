"""Todo 状态和提醒逻辑。

这一层不直接和 LLM 或工具协议耦合，专门负责：
- 保存当前 todo 列表
- 校验 todo 状态是否合法
- 保证同一时刻最多只有一个 in_progress
- 统计模型连续多少轮没有更新 todo
- 在需要时生成提醒文本
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


TodoStatus = Literal["pending", "in_progress", "completed"]


@dataclass(slots=True)
class TodoItem:
    """一条 todo 项。"""

    content: str
    status: TodoStatus


class TodoManager:
    """管理 todo 列表和提醒计数。"""

    def __init__(self, reminder_threshold: int = 3) -> None:
        self.items: list[TodoItem] = []
        self.rounds_since_update = 0
        self.reminder_threshold = reminder_threshold

    def replace(self, items: list[TodoItem]) -> str:
        """用一整份新列表替换当前 todo 状态。"""

        self._validate(items)
        self.items = items
        self.rounds_since_update = 0

        if not items:
            return "已清空 todo 列表。"

        return "已更新 todo 列表：\n" + self.render()

    def note_round(self, touched_todo: bool) -> None:
        """记录一轮模型交互是否更新了 todo。"""

        if touched_todo:
            self.rounds_since_update = 0
            return

        self.rounds_since_update += 1

    def should_remind(self) -> bool:
        """判断当前是否应该注入 todo 提醒。"""

        return self.rounds_since_update >= self.reminder_threshold

    def build_reminder(self) -> str:
        """生成注入给模型的提醒文本。"""

        lines = [
            "<reminder>请检查并更新 todo 列表。",
            "复杂或多步骤任务应维护 todo 列表。",
            "同一时刻最多只能有一个 in_progress。</reminder>",
        ]

        if self.items:
            lines.append("当前 todo 状态：")
            lines.append(self.render())
        else:
            lines.append("当前还没有 todo 列表。")

        return "\n".join(lines)

    def render(self) -> str:
        """把当前 todo 列表渲染成纯文本。"""

        if not self.items:
            return "（当前没有 todo）"

        status_mark = {
            "pending": "[ ]",
            "in_progress": "[>]",
            "completed": "[x]",
        }

        return "\n".join(
            f"{status_mark[item.status]} {item.content}"
            for item in self.items
        )

    @staticmethod
    def _validate(items: list[TodoItem]) -> None:
        """校验 todo 列表是否合法。"""

        in_progress_count = 0

        for item in items:
            if not item.content.strip():
                raise ValueError("todo 项内容不能为空")

            if item.status == "in_progress":
                in_progress_count += 1

        if in_progress_count > 1:
            raise ValueError("同一时刻最多只能有一个 in_progress")
