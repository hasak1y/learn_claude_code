"""父 Agent 和 teammate 共用的任务图工具。"""

from __future__ import annotations

from typing import Any

from ..task_graph import TaskGraphManager
from .base import BaseTool


class CreateTaskTool(BaseTool):
    """创建一个持久化任务图节点。"""

    name = "task_create"
    description = (
        "创建一个持久化任务图任务。"
        "适合存在前置依赖、后续解锁关系或可并行推进的复杂任务。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "任务标题。",
            },
            "description": {
                "type": "string",
                "description": "任务补充说明，可选。",
            },
            "blocked_by": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "前置依赖任务 id 列表，可选。",
            },
            "owner": {
                "type": "string",
                "description": "任务 owner，可选。",
            },
        },
        "required": ["subject"],
    }

    def __init__(self, manager: TaskGraphManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        subject = str(arguments.get("subject", "")).strip()
        description = str(arguments.get("description", ""))
        owner = str(arguments.get("owner", ""))
        blocked_by = arguments.get("blocked_by", [])

        if blocked_by is None:
            blocked_by = []
        if not isinstance(blocked_by, list):
            return "错误：blocked_by 必须是数组"

        try:
            dependency_ids = [int(item) for item in blocked_by]
        except (TypeError, ValueError):
            return "错误：blocked_by 中的依赖 id 必须是整数"

        return self.manager.create(
            subject=subject,
            description=description,
            blocked_by=dependency_ids,
            owner=owner,
        )


class UpdateTaskTool(BaseTool):
    """更新任务状态或依赖。"""

    name = "task_update"
    description = (
        "更新任务图中的任务。"
        "可修改状态、增加依赖、移除依赖或更新标题说明。"
        "如果提供 base_version，就会启用乐观并发控制："
        "只有任务版本仍然匹配时才会写入。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "integer",
                "description": "任务 id。",
            },
            "base_version": {
                "type": "integer",
                "description": "本次更新基于的任务版本号，可选。建议先 task_get 再带着 version 更新。",
            },
            "status": {
                "type": "string",
                "enum": [
                    "pending",
                    "in_progress",
                    "integration_pending",
                    "completed",
                    "cancelled",
                    "abandoned",
                ],
                "description": "任务状态，可选。",
            },
            "add_blocked_by": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "新增依赖任务 id 列表，可选。",
            },
            "remove_blocked_by": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "移除依赖任务 id 列表，可选。",
            },
            "subject": {
                "type": "string",
                "description": "新的任务标题，可选。",
            },
            "description": {
                "type": "string",
                "description": "新的任务说明，可选。",
            },
            "owner": {
                "type": "string",
                "description": "新的 owner，可选。",
            },
        },
        "required": ["task_id"],
    }

    def __init__(self, manager: TaskGraphManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        raw_task_id = arguments.get("task_id")
        if raw_task_id is None:
            return "错误：缺少 'task_id' 参数"

        try:
            task_id = int(raw_task_id)
        except (TypeError, ValueError):
            return "错误：task_id 必须是整数"

        raw_base_version = arguments.get("base_version")
        if raw_base_version is None:
            base_version = None
        else:
            try:
                base_version = int(raw_base_version)
            except (TypeError, ValueError):
                return "错误：base_version 必须是整数"

        add_blocked_by = arguments.get("add_blocked_by")
        remove_blocked_by = arguments.get("remove_blocked_by")

        try:
            add_dependency_ids = (
                [int(item) for item in add_blocked_by]
                if isinstance(add_blocked_by, list)
                else None
            )
            remove_dependency_ids = (
                [int(item) for item in remove_blocked_by]
                if isinstance(remove_blocked_by, list)
                else None
            )
        except (TypeError, ValueError):
            return "错误：依赖 id 必须是整数"

        if add_blocked_by is not None and not isinstance(add_blocked_by, list):
            return "错误：add_blocked_by 必须是数组"
        if remove_blocked_by is not None and not isinstance(remove_blocked_by, list):
            return "错误：remove_blocked_by 必须是数组"

        return self.manager.update(
            task_id=task_id,
            base_version=base_version,
            status=(
                str(arguments["status"]).strip()
                if "status" in arguments and arguments.get("status") is not None
                else None
            ),
            add_blocked_by=add_dependency_ids,
            remove_blocked_by=remove_dependency_ids,
            subject=(
                str(arguments["subject"])
                if "subject" in arguments and arguments.get("subject") is not None
                else None
            ),
            description=(
                str(arguments["description"])
                if "description" in arguments and arguments.get("description") is not None
                else None
            ),
            owner=(
                str(arguments["owner"])
                if "owner" in arguments and arguments.get("owner") is not None
                else None
            ),
        )


class GetTaskTool(BaseTool):
    """查看单个任务。"""

    name = "task_get"
    description = "查看一个任务图任务的完整信息。"
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "integer",
                "description": "任务 id。",
            }
        },
        "required": ["task_id"],
    }

    def __init__(self, manager: TaskGraphManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        raw_task_id = arguments.get("task_id")
        if raw_task_id is None:
            return "错误：缺少 'task_id' 参数"

        try:
            task_id = int(raw_task_id)
        except (TypeError, ValueError):
            return "错误：task_id 必须是整数"

        return self.manager.get(task_id)


class ListAllTasksTool(BaseTool):
    """查看全部任务分组。"""

    name = "task_list_all"
    description = (
        "按 ready、blocked、in_progress、integration_pending、completed、"
        "cancelled、abandoned 分组查看全部任务。"
    )
    input_schema = {
        "type": "object",
        "properties": {},
    }

    def __init__(self, manager: TaskGraphManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        return self.manager.list_all()


class ListReadyTasksTool(BaseTool):
    """查看当前 ready 任务。"""

    name = "task_list_ready"
    description = "查看当前可以开始的 ready 任务。"
    input_schema = {
        "type": "object",
        "properties": {},
    }

    def __init__(self, manager: TaskGraphManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        return self.manager.list_ready()


class ListBlockedTasksTool(BaseTool):
    """查看当前 blocked 任务。"""

    name = "task_list_blocked"
    description = "查看当前被依赖阻塞的任务以及阻塞原因。"
    input_schema = {
        "type": "object",
        "properties": {},
    }

    def __init__(self, manager: TaskGraphManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        return self.manager.list_blocked()


class ListCompletedTasksTool(BaseTool):
    """查看当前 completed 任务。"""

    name = "task_list_completed"
    description = "查看当前已完成的任务。"
    input_schema = {
        "type": "object",
        "properties": {},
    }

    def __init__(self, manager: TaskGraphManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        return self.manager.list_completed()


class ListAbandonedTasksTool(BaseTool):
    """查看当前 abandoned 任务。"""

    name = "task_list_abandoned"
    description = "查看当前 abandoned 任务。它们默认不会在新会话中继续执行。"
    input_schema = {
        "type": "object",
        "properties": {},
    }

    def __init__(self, manager: TaskGraphManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        return self.manager.list_abandoned()


class RestoreTasksTool(BaseTool):
    """显式恢复 abandoned / cancelled 任务。"""

    name = "task_restore"
    description = (
        "显式恢复 abandoned 或 cancelled 任务。"
        "适合用户明确要求“继续上次任务”时使用。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "要恢复的任务 id 列表。",
            },
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress"],
                "description": "恢复后的目标状态，默认 pending。",
            },
        },
        "required": ["task_ids"],
    }

    def __init__(self, manager: TaskGraphManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        raw_ids = arguments.get("task_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return "错误：task_ids 必须是非空数组"

        try:
            task_ids = [int(item) for item in raw_ids]
        except (TypeError, ValueError):
            return "错误：task_ids 中的元素必须是整数"

        status = str(arguments.get("status", "pending")).strip() or "pending"
        return self.manager.restore_tasks(task_ids=task_ids, status=status)
