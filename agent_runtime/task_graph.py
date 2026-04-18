"""持久化任务图。

这一层把简单 todo 之外的“有依赖关系的任务”落到磁盘：

- 每个任务一个 JSON 文件
- `blockedBy` 表示静态依赖边，不会因为前置任务完成而被删除
- `ready` / `blocked` 是运行时派生状态，不单独落盘

这样运行时可以稳定回答：
- 什么可以做
- 什么被卡住
- 什么做完了
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


TaskStatus = Literal["pending", "in_progress", "completed"]


@dataclass(slots=True)
class TaskNode:
    """单个任务节点。"""

    id: int
    subject: str
    description: str
    status: TaskStatus
    blocked_by: list[int]
    owner: str
    claimed_by: str
    claimed_at: float | None
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, object]:
        """转成可序列化字典。"""

        return {
            "id": self.id,
            "subject": self.subject,
            "description": self.description,
            "status": self.status,
            "blockedBy": self.blocked_by,
            "owner": self.owner,
            "claimedBy": self.claimed_by,
            "claimedAt": self.claimed_at,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "TaskNode":
        """从字典恢复任务节点。"""

        return cls(
            id=int(data["id"]),
            subject=str(data.get("subject", "")).strip(),
            description=str(data.get("description", "")),
            status=str(data.get("status", "pending")),  # type: ignore[arg-type]
            blocked_by=[int(item) for item in data.get("blockedBy", [])],  # type: ignore[arg-type]
            owner=str(data.get("owner", "")),
            claimed_by=str(data.get("claimedBy", "")),
            claimed_at=(
                float(data.get("claimedAt")) if data.get("claimedAt") is not None else None
            ),
            created_at=float(data.get("createdAt", time.time())),
            updated_at=float(data.get("updatedAt", time.time())),
        )


class TaskGraphManager:
    """管理 `.tasks/` 目录下的持久化任务图。"""

    def __init__(self, tasks_dir: Path) -> None:
        self.dir = tasks_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._next_id = self._max_id() + 1

    def create(
        self,
        subject: str,
        description: str = "",
        blocked_by: list[int] | None = None,
        owner: str = "",
    ) -> str:
        """创建一个新任务。"""

        clean_subject = subject.strip()
        if not clean_subject:
            return "错误：subject 不能为空"

        blocked_by = self._normalize_dependency_list(blocked_by or [])
        now = time.time()
        task = TaskNode(
            id=self._next_id,
            subject=clean_subject,
            description=description,
            status="pending",
            blocked_by=blocked_by,
            owner=owner,
            claimed_by="",
            claimed_at=None,
            created_at=now,
            updated_at=now,
        )

        try:
            self._validate_node(task)
        except ValueError as exc:
            return f"错误：{exc}"

        self._save(task)
        self._next_id += 1
        return "已创建任务：\n" + self._format_task(task)

    def update(
        self,
        task_id: int,
        status: str | None = None,
        add_blocked_by: list[int] | None = None,
        remove_blocked_by: list[int] | None = None,
        subject: str | None = None,
        description: str | None = None,
        owner: str | None = None,
    ) -> str:
        """更新任务状态或依赖关系。"""

        try:
            task = self._load(task_id)
        except ValueError as exc:
            return f"错误：{exc}"

        previous_status = task.status

        if subject is not None:
            clean_subject = subject.strip()
            if not clean_subject:
                return "错误：subject 不能为空"
            task.subject = clean_subject

        if description is not None:
            task.description = description

        if owner is not None:
            task.owner = owner

        blocked_by = list(task.blocked_by)
        if add_blocked_by:
            blocked_by.extend(add_blocked_by)
        if remove_blocked_by:
            remove_set = set(remove_blocked_by)
            blocked_by = [item for item in blocked_by if item not in remove_set]
        task.blocked_by = self._normalize_dependency_list(blocked_by)

        if status is not None:
            normalized_status = str(status).strip()
            if normalized_status not in {"pending", "in_progress", "completed"}:
                return f"错误：非法状态：{status}"
            try:
                self._validate_status_transition(task, normalized_status)
            except ValueError as exc:
                return f"错误：{exc}"
            task.status = normalized_status  # type: ignore[assignment]
            if normalized_status in {"pending", "completed"}:
                # 退回 pending 或进入 completed 时，自动清理 claim。
                task.claimed_by = ""
                task.claimed_at = None

        task.updated_at = time.time()

        try:
            self._validate_node(task)
        except ValueError as exc:
            return f"错误：{exc}"

        self._save(task)

        lines = ["已更新任务：", self._format_task(task)]
        if previous_status != "completed" and task.status == "completed":
            newly_ready = self._find_newly_ready_dependents(task.id)
            if newly_ready:
                lines.append("")
                lines.append("本次完成后已解锁任务：")
                for item in newly_ready:
                    lines.append(f"- task {item.id}: {item.subject}")

        return "\n".join(lines)

    def claim_next_for_agent(self, agent_id: str, agent_role: str) -> TaskNode | None:
        """为某个持久 teammate 认领一项可执行任务。

        这里刻意做成“受限拉活”：
        - 只看 ready 任务
        - 只看尚未被其他 Agent 认领的任务
        - 只认领 owner 为空、或 owner 匹配 agent_id / role 的任务
        """

        for task in self._all_tasks():
            if self._effective_status(task) != "ready":
                continue
            if task.claimed_by:
                continue
            if not self._is_claimable_by(task=task, agent_id=agent_id, agent_role=agent_role):
                continue

            task.status = "in_progress"
            task.claimed_by = agent_id
            task.claimed_at = time.time()
            task.updated_at = time.time()
            self._save(task)
            return task

        return None

    def get(self, task_id: int) -> str:
        """查看单个任务。"""

        try:
            task = self._load(task_id)
        except ValueError as exc:
            return f"错误：{exc}"

        return self._format_task(task)

    def list_ready(self) -> str:
        """列出当前可开始的任务。"""

        ready = [task for task in self._all_tasks() if self._effective_status(task) == "ready"]
        if not ready:
            return "当前没有 ready 任务。"

        lines = ["当前可开始的任务："]
        for task in ready:
            lines.append(f"- task {task.id}: {task.subject}")
        return "\n".join(lines)

    def list_blocked(self) -> str:
        """列出当前被依赖阻塞的任务。"""

        blocked = [task for task in self._all_tasks() if self._effective_status(task) == "blocked"]
        if not blocked:
            return "当前没有 blocked 任务。"

        lines = ["当前被卡住的任务："]
        for task in blocked:
            unmet = self._unmet_dependencies(task)
            lines.append(f"- task {task.id}: {task.subject} | 等待 {unmet}")
        return "\n".join(lines)

    def list_completed(self) -> str:
        """列出当前已完成任务。"""

        completed = [task for task in self._all_tasks() if task.status == "completed"]
        if not completed:
            return "当前没有 completed 任务。"

        lines = ["当前已完成的任务："]
        for task in completed:
            lines.append(f"- task {task.id}: {task.subject}")
        return "\n".join(lines)

    def list_all(self) -> str:
        """按分组列出全部任务。"""

        tasks = self._all_tasks()
        if not tasks:
            return "当前没有任务图任务。"

        groups = {
            "ready": [task for task in tasks if self._effective_status(task) == "ready"],
            "blocked": [task for task in tasks if self._effective_status(task) == "blocked"],
            "in_progress": [task for task in tasks if task.status == "in_progress"],
            "completed": [task for task in tasks if task.status == "completed"],
        }

        lines = ["当前任务图："]
        for group_name, group_tasks in groups.items():
            lines.append(f"[{group_name}]")
            if not group_tasks:
                lines.append("- （无）")
                continue
            for task in group_tasks:
                suffix = ""
                if group_name == "blocked":
                    suffix = f" | 等待 {self._unmet_dependencies(task)}"
                lines.append(f"- task {task.id}: {task.subject}{suffix}")
        return "\n".join(lines)

    def render_summary(self) -> str:
        """生成适合提醒和摘要的任务图摘要。"""

        tasks = self._all_tasks()
        if not tasks:
            return "（当前没有任务图任务）"

        ready = [f"task {task.id}" for task in tasks if self._effective_status(task) == "ready"]
        blocked = [f"task {task.id}" for task in tasks if self._effective_status(task) == "blocked"]
        in_progress = [f"task {task.id}" for task in tasks if task.status == "in_progress"]
        completed = [f"task {task.id}" for task in tasks if task.status == "completed"]

        return (
            f"ready: {', '.join(ready) if ready else '（无）'}\n"
            f"blocked: {', '.join(blocked) if blocked else '（无）'}\n"
            f"in_progress: {', '.join(in_progress) if in_progress else '（无）'}\n"
            f"completed: {', '.join(completed) if completed else '（无）'}"
        )

    def has_tasks(self) -> bool:
        """判断当前是否已有任务图任务。"""

        return any(self.dir.glob("task_*.json"))

    def _max_id(self) -> int:
        """扫描当前最大任务 id。"""

        max_id = 0
        for path in self.dir.glob("task_*.json"):
            try:
                max_id = max(max_id, int(path.stem.split("_", 1)[1]))
            except (IndexError, ValueError):
                continue
        return max_id

    def _task_path(self, task_id: int) -> Path:
        """返回任务文件路径。"""

        return self.dir / f"task_{task_id}.json"

    def _save(self, task: TaskNode) -> None:
        """保存任务文件。"""

        self._task_path(task.id).write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load(self, task_id: int) -> TaskNode:
        """读取单个任务。"""

        path = self._task_path(task_id)
        if not path.exists():
            raise ValueError(f"不存在任务 {task_id}")

        data = json.loads(path.read_text(encoding="utf-8"))
        return TaskNode.from_dict(data)

    def _all_tasks(self) -> list[TaskNode]:
        """读取全部任务。"""

        tasks = [self._load(int(path.stem.split("_", 1)[1])) for path in self.dir.glob("task_*.json")]
        tasks.sort(key=lambda item: item.id)
        return tasks

    def _validate_node(self, task: TaskNode) -> None:
        """校验任务节点是否合法。"""

        if not task.subject.strip():
            raise ValueError("任务 subject 不能为空")

        if task.id in task.blocked_by:
            raise ValueError("任务不能依赖自己")

        existing_ids = {item.id for item in self._all_tasks() if item.id != task.id}
        for dependency_id in task.blocked_by:
            if dependency_id not in existing_ids:
                raise ValueError(f"依赖任务不存在：{dependency_id}")

        self._ensure_acyclic(task)

    def _ensure_acyclic(self, candidate: TaskNode) -> None:
        """检查加入或修改后是否形成环。"""

        dependency_map: dict[int, list[int]] = {
            task.id: list(task.blocked_by)
            for task in self._all_tasks()
            if task.id != candidate.id
        }
        dependency_map[candidate.id] = list(candidate.blocked_by)

        visiting: set[int] = set()
        visited: set[int] = set()

        def dfs(task_id: int) -> None:
            if task_id in visiting:
                raise ValueError("任务依赖图不能形成环")
            if task_id in visited:
                return

            visiting.add(task_id)
            for dependency_id in dependency_map.get(task_id, []):
                dfs(dependency_id)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in dependency_map:
            dfs(task_id)

    def _validate_status_transition(self, task: TaskNode, new_status: str) -> None:
        """校验状态流转是否合法。"""

        if new_status == task.status:
            return

        allowed = {
            "pending": {"in_progress", "completed"},
            "in_progress": {"completed", "pending"},
            "completed": {"pending"},
        }
        if new_status not in allowed[task.status]:
            raise ValueError(f"不允许从 {task.status} 变更到 {new_status}")

        if new_status == "in_progress" and self._unmet_dependency_ids(task):
            raise ValueError("当前任务仍被依赖阻塞，不能开始")

    def _effective_status(self, task: TaskNode) -> str:
        """计算派生状态。"""

        if task.status == "pending":
            return "blocked" if self._unmet_dependency_ids(task) else "ready"
        return task.status

    @staticmethod
    def _is_claimable_by(task: TaskNode, agent_id: str, agent_role: str) -> bool:
        """判断某个 teammate 是否允许认领该任务。"""

        owner = task.owner.strip()
        if not owner:
            return True
        return owner in {agent_id, agent_role}

    def _unmet_dependencies(self, task: TaskNode) -> str:
        """返回未完成依赖的文本摘要。"""

        unmet = self._unmet_dependency_ids(task)
        return ", ".join(f"task {item}" for item in unmet) if unmet else "（无）"

    def _unmet_dependency_ids(self, task: TaskNode) -> list[int]:
        """返回未完成依赖任务 id。"""

        status_by_id = {item.id: item.status for item in self._all_tasks()}
        return [
            dependency_id
            for dependency_id in task.blocked_by
            if status_by_id.get(dependency_id) != "completed"
        ]

    def _find_newly_ready_dependents(self, completed_id: int) -> list[TaskNode]:
        """找出因为某个任务完成而变成 ready 的直接后继任务。"""

        dependents: list[TaskNode] = []
        for task in self._all_tasks():
            if completed_id not in task.blocked_by:
                continue
            if self._effective_status(task) == "ready":
                dependents.append(task)
        return dependents

    @staticmethod
    def _normalize_dependency_list(items: list[int]) -> list[int]:
        """清洗依赖列表并去重排序。"""

        normalized = sorted({int(item) for item in items})
        return normalized

    def _format_task(self, task: TaskNode) -> str:
        """格式化单个任务。"""

        data = task.to_dict()
        data["effectiveStatus"] = self._effective_status(task)
        return json.dumps(data, ensure_ascii=False, indent=2)
