"""todo 工具。

这个工具不做增量 patch，而是要求模型每次提交一整份最新的 todo 列表。
这样实现简单、状态清晰，也更容易校验。
"""

from __future__ import annotations

from typing import Any

from ..todo import TodoItem, TodoManager
from .base import BaseTool


class TodoTool(BaseTool):
    """创建或更新当前任务的 todo 列表。"""

    name = "todo"
    description = (
        "创建或更新当前任务的 todo 列表。"
        "适合复杂、多步骤任务。"
        "每次调用都应提交最新的完整列表，且同一时刻最多只能有一个 in_progress。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "当前任务的完整 todo 列表。",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "todo 项内容。",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                            "description": "todo 状态。",
                        },
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["items"],
    }

    def __init__(self, todo_manager: TodoManager) -> None:
        self.todo_manager = todo_manager

    def execute(self, arguments: dict[str, Any]) -> str:
        """更新 todo 列表，并返回新的状态文本。"""

        raw_items = arguments.get("items")
        if raw_items is None:
            return "错误：缺少 'items' 参数"

        if not isinstance(raw_items, list):
            return "错误：'items' 必须是数组"

        try:
            items = self._parse_items(raw_items)
            return self.todo_manager.replace(items)
        except ValueError as exc:
            return f"错误：{exc}"

    @staticmethod
    def _parse_items(raw_items: list[object]) -> list[TodoItem]:
        """把原始参数解析成 TodoItem 列表。"""

        items: list[TodoItem] = []

        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise ValueError("todo 项必须是对象")

            content = str(raw_item.get("content", "")).strip()
            status = str(raw_item.get("status", "")).strip()

            if status not in {"pending", "in_progress", "completed"}:
                raise ValueError(f"非法 todo 状态：{status}")

            items.append(TodoItem(content=content, status=status))

        return items
