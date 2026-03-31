"""Todo 状态和任务跟踪提醒逻辑。"""

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
    """管理简单 todo 列表，并承担通用任务跟踪提醒计数。"""

    def __init__(self, reminder_threshold: int = 3) -> None:
        self.items: list[TodoItem] = []
        self.rounds_since_tracking_update = 0
        self.reminder_threshold = reminder_threshold

    def replace(self, items: list[TodoItem]) -> str:
        """用一整份新列表替换当前 todo 状态。"""

        self._validate(items)
        self.items = items
        self.rounds_since_tracking_update = 0

        if not items:
            return "已清空 todo 列表。"

        return "已更新 todo 列表：\n" + self.render()

    def note_round(self, touched_tracking: bool) -> None:
        """记录一轮模型交互是否更新了任务跟踪信息。"""

        if touched_tracking:
            self.rounds_since_tracking_update = 0
            return

        self.rounds_since_tracking_update += 1

    def should_remind(self) -> bool:
        """判断当前是否应该注入任务跟踪提醒。"""

        return self.rounds_since_tracking_update >= self.reminder_threshold

    def build_reminder(self, task_graph_summary: str | None = None) -> str:
        """生成注入给模型的提醒文本。

        简单任务可以继续使用 todo。
        如果任务存在依赖、解锁关系或并行结构，则应使用持久化任务图工具。
        """

        lines = [
            "<reminder>请检查并更新任务跟踪信息。",
            "简单任务可使用 todo 列表。",
            "存在依赖、解锁关系或可并行推进的复杂任务，应使用 task graph 工具。",
            "todo 中同一时刻最多只能有一个 in_progress。",
            "</reminder>",
        ]

        if self.items:
            lines.append("当前 todo 状态：")
            lines.append(self.render())
        else:
            lines.append("当前还没有 todo 列表。")

        if task_graph_summary:
            lines.append("当前任务图摘要：")
            lines.append(task_graph_summary)
        else:
            lines.append("当前任务图摘要：")
            lines.append("（当前没有任务图任务）")

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
