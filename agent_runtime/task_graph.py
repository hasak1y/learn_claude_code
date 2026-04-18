"""持久化任务图。

这一层把简单 todo 之外的“有依赖关系的任务”落到磁盘：

- 每个任务一个 JSON 文件
- `blockedBy` 表示静态依赖边，不会因为前置任务完成而被删除
- `ready` / `blocked` 是运行时派生状态，不单独落盘
- `version` 用于乐观并发控制，避免多个 Agent 相互覆盖更新
- 锁文件 + 原子写回用于降低 claim / update / create 的竞争风险
- `createdInSession` / `taskBatchId` 用于表达任务来自哪个会话批次
- 新会话启动时，旧会话遗留的活跃任务默认会被转成 `abandoned`

这样运行时可以稳定回答：
- 什么可以做
- 什么被卡住
- 什么已经完成
- 什么被用户取消
- 什么只是旧会话遗留、当前默认不继续
- 这条任务有没有被别人先改过
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


TaskStatus = Literal[
    "pending",
    "in_progress",
    "integration_pending",
    "completed",
    "cancelled",
    "abandoned",
]


@dataclass(slots=True)
class TaskNode:
    """单个任务节点。"""

    id: int
    subject: str
    description: str
    status: TaskStatus
    blocked_by: list[int]
    owner: str
    version: int
    claimed_by: str
    claimed_at: float | None
    created_in_session: str
    task_batch_id: str
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
            "version": self.version,
            "claimedBy": self.claimed_by,
            "claimedAt": self.claimed_at,
            "createdInSession": self.created_in_session,
            "taskBatchId": self.task_batch_id,
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
            version=int(data.get("version", 1)),
            claimed_by=str(data.get("claimedBy", "")),
            claimed_at=(
                float(data.get("claimedAt")) if data.get("claimedAt") is not None else None
            ),
            created_in_session=str(data.get("createdInSession", "")),
            task_batch_id=str(data.get("taskBatchId", "")),
            created_at=float(data.get("createdAt", time.time())),
            updated_at=float(data.get("updatedAt", time.time())),
        )


class TaskGraphManager:
    """管理 `.tasks/` 目录下的持久化任务图。

    这层既负责任务依赖和 claim，也负责跨会话的最小生命周期语义：
    - 活跃任务：pending / in_progress / integration_pending
    - 终态任务：completed / cancelled / abandoned
    """

    def __init__(self, tasks_dir: Path) -> None:
        self.dir = tasks_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._locks_dir = self.dir / ".locks"
        self._locks_dir.mkdir(parents=True, exist_ok=True)
        self.current_session_id = ""
        self.current_task_batch_id = ""

    def set_runtime_context(self, *, session_id: str, task_batch_id: str | None = None) -> None:
        """设置当前运行时上下文，供后续新建任务打上来源标签。

        `session_id` 用来判断“这是不是旧会话遗留任务”，
        `task_batch_id` 预留给后续更细粒度的一组任务批次恢复。
        """

        self.current_session_id = session_id
        self.current_task_batch_id = task_batch_id or session_id

    def create(
        self,
        subject: str,
        description: str = "",
        blocked_by: list[int] | None = None,
        owner: str = "",
    ) -> str:
        """创建一个新任务。

        这里对 create 单独加全局锁，原因是 task id 的分配属于共享资源。
        如果两个 Agent 几乎同时创建任务，而没有锁，就可能拿到相同 id。
        """

        clean_subject = subject.strip()
        if not clean_subject:
            return "错误：subject 不能为空"

        blocked_by = self._normalize_dependency_list(blocked_by or [])
        with self._lock_scope("create"):
            now = time.time()
            task = TaskNode(
                id=self._max_id() + 1,
                subject=clean_subject,
                description=description,
                status="pending",
                blocked_by=blocked_by,
                owner=owner,
                version=1,
                claimed_by="",
                claimed_at=None,
                created_in_session=self.current_session_id,
                task_batch_id=self.current_task_batch_id or self.current_session_id,
                created_at=now,
                updated_at=now,
            )

            try:
                self._validate_node(task)
            except ValueError as exc:
                return f"错误：{exc}"

            self._save(task)
            return "已创建任务：\n" + self._format_task(task)

    def update(
        self,
        task_id: int,
        base_version: int | None = None,
        status: str | None = None,
        add_blocked_by: list[int] | None = None,
        remove_blocked_by: list[int] | None = None,
        subject: str | None = None,
        description: str | None = None,
        owner: str | None = None,
    ) -> str:
        """更新任务状态或依赖关系。

        这里的关键不是“读出来改一改再写回”，而是：
        1. 先锁住该任务
        2. 在锁内重读最新版本
        3. 检查调用方带来的 `base_version`
        4. 版本一致才允许写回

        这样可以把“别人已经改过，但我还在拿旧快照更新”的问题显式暴露出来。
        """

        with self._lock_task(task_id):
            try:
                task = self._load(task_id)
            except ValueError as exc:
                return f"错误：{exc}"

            if base_version is not None and task.version != base_version:
                return (
                    "错误：任务版本不一致，说明这条任务已经被其他 Agent 更新过。"
                    f"当前版本为 {task.version}，但本次更新基于版本 {base_version}。"
                    "请先重新读取任务，再基于最新版本重试。"
                )

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
                if normalized_status not in {
                    "pending",
                    "in_progress",
                    "integration_pending",
                    "completed",
                    "cancelled",
                    "abandoned",
                }:
                    return f"错误：非法状态：{status}"
                try:
                    self._validate_status_transition(task, normalized_status)
                except ValueError as exc:
                    return f"错误：{exc}"
                task.status = normalized_status  # type: ignore[assignment]
                if normalized_status in {"pending", "integration_pending", "completed", "cancelled", "abandoned"}:
                    # 退回 pending、进入 review 待集成或真正完成时，自动清理 claim。
                    task.claimed_by = ""
                    task.claimed_at = None

            task.version += 1
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

        并且会在锁内重读任务，避免两个 teammate 同时 claim 同一条任务。
        """

        for task in self._all_tasks():
            if self._effective_status(task) != "ready":
                continue
            if task.claimed_by:
                continue
            if not self._is_claimable_by(task=task, agent_id=agent_id, agent_role=agent_role):
                continue

            with self._lock_task(task.id):
                try:
                    latest = self._load(task.id)
                except ValueError:
                    continue

                if self._effective_status(latest) != "ready":
                    continue
                if latest.claimed_by:
                    continue
                if not self._is_claimable_by(
                    task=latest,
                    agent_id=agent_id,
                    agent_role=agent_role,
                ):
                    continue

                latest.status = "in_progress"
                latest.claimed_by = agent_id
                latest.claimed_at = time.time()
                latest.version += 1
                latest.updated_at = time.time()
                self._save(latest)
                return latest

        return None

    def get(self, task_id: int) -> str:
        """查看单个任务。"""

        try:
            task = self._load(task_id)
        except ValueError as exc:
            return f"错误：{exc}"

        return self._format_task(task)

    def get_node(self, task_id: int) -> TaskNode:
        """返回结构化任务节点，供内部控制层读取最新版本。"""

        return self._load(task_id)

    def abandon_stale_tasks(self, *, current_session_id: str) -> str:
        """把旧会话遗留的活跃任务标记为 abandoned。

        这层解决的是“上次跑到一半，这次默认不要自动续跑”。
        真正需要继续时，应由显式恢复动作把任务拉回 pending / in_progress。
        """

        changed: list[str] = []
        for task in self._all_tasks():
            if task.status not in {"pending", "in_progress", "integration_pending"}:
                continue
            if task.created_in_session == current_session_id:
                continue

            with self._lock_task(task.id):
                latest = self._load(task.id)
                if latest.status not in {"pending", "in_progress", "integration_pending"}:
                    continue
                if latest.created_in_session == current_session_id:
                    continue

                latest.status = "abandoned"
                latest.claimed_by = ""
                latest.claimed_at = None
                latest.version += 1
                latest.updated_at = time.time()
                self._save(latest)
                changed.append(f"task {latest.id}: {latest.subject}")

        if not changed:
            return "没有需要失活的旧任务。"
        return "已将旧会话遗留任务标记为 abandoned：\n- " + "\n- ".join(changed)

    def restore_tasks(
        self,
        *,
        task_ids: list[int],
        status: str = "pending",
    ) -> str:
        """显式恢复 abandoned / cancelled 任务。"""

        normalized_status = status.strip()
        if normalized_status not in {"pending", "in_progress"}:
            return "错误：恢复后的状态只能是 pending 或 in_progress"

        results: list[str] = []
        for task_id in task_ids:
            with self._lock_task(task_id):
                try:
                    task = self._load(task_id)
                except ValueError as exc:
                    results.append(f"task {task_id}: {exc}")
                    continue

                if task.status not in {"abandoned", "cancelled"}:
                    results.append(f"task {task_id}: 当前状态为 {task.status}，不能恢复")
                    continue

                if normalized_status == "in_progress" and self._unmet_dependency_ids(task):
                    results.append(f"task {task_id}: 仍被依赖阻塞，不能直接恢复为 in_progress")
                    continue

                task.status = normalized_status  # type: ignore[assignment]
                task.claimed_by = ""
                task.claimed_at = None
                task.version += 1
                task.updated_at = time.time()
                self._save(task)
                results.append(f"task {task_id}: 已恢复为 {normalized_status}")

        return "恢复结果：\n- " + "\n- ".join(results) if results else "没有恢复任何任务。"

    def list_ready(self) -> str:
        """列出当前可开始的任务。"""

        ready = [task for task in self._all_tasks() if self._effective_status(task) == "ready"]
        if not ready:
            return "当前没有 ready 任务。"

        lines = ["当前可开始的任务："]
        for task in ready:
            lines.append(f"- task {task.id}: {task.subject} | version {task.version}")
        return "\n".join(lines)

    def list_blocked(self) -> str:
        """列出当前被依赖阻塞的任务。"""

        blocked = [task for task in self._all_tasks() if self._effective_status(task) == "blocked"]
        if not blocked:
            return "当前没有 blocked 任务。"

        lines = ["当前被卡住的任务："]
        for task in blocked:
            unmet = self._unmet_dependencies(task)
            lines.append(
                f"- task {task.id}: {task.subject} | version {task.version} | 等待 {unmet}"
            )
        return "\n".join(lines)

    def list_completed(self) -> str:
        """列出当前已完成任务。"""

        completed = [task for task in self._all_tasks() if task.status == "completed"]
        if not completed:
            return "当前没有 completed 任务。"

        lines = ["当前已完成的任务："]
        for task in completed:
            lines.append(f"- task {task.id}: {task.subject} | version {task.version}")
        return "\n".join(lines)

    def list_abandoned(self) -> str:
        """列出当前 abandoned 任务。"""

        abandoned = [task for task in self._all_tasks() if task.status == "abandoned"]
        if not abandoned:
            return "当前没有 abandoned 任务。"

        lines = ["当前 abandoned 任务："]
        for task in abandoned:
            lines.append(f"- task {task.id}: {task.subject} | version {task.version}")
        return "\n".join(lines)

    def list_all(self) -> str:
        """按分组列出全部任务。

        这里不仅展示活跃任务，也展示：
        - `cancelled`：用户明确终止
        - `abandoned`：旧会话遗留，当前默认不继续
        """

        tasks = self._all_tasks()
        if not tasks:
            return "当前没有任务图任务。"

        groups = {
            "ready": [task for task in tasks if self._effective_status(task) == "ready"],
            "blocked": [task for task in tasks if self._effective_status(task) == "blocked"],
            "in_progress": [task for task in tasks if task.status == "in_progress"],
            "integration_pending": [task for task in tasks if task.status == "integration_pending"],
            "completed": [task for task in tasks if task.status == "completed"],
            "cancelled": [task for task in tasks if task.status == "cancelled"],
            "abandoned": [task for task in tasks if task.status == "abandoned"],
        }

        lines = ["当前任务图："]
        for group_name, group_tasks in groups.items():
            lines.append(f"[{group_name}]")
            if not group_tasks:
                lines.append("- （无）")
                continue
            for task in group_tasks:
                suffix = f" | version {task.version}"
                if group_name == "blocked":
                    suffix += f" | 等待 {self._unmet_dependencies(task)}"
                lines.append(f"- task {task.id}: {task.subject}{suffix}")
        return "\n".join(lines)

    def get_claimed_task_for_agent(self, agent_id: str) -> TaskNode | None:
        """返回某个 Agent 当前正在处理的任务。

        这里的含义是：
        - 任务状态为 in_progress
        - 且 claimed_by 就是这个 agent

        worktree 分配会用它来判断：
        这个 teammate 当前有没有已经绑定的活动任务。
        """

        for task in self._all_tasks():
            if task.status == "in_progress" and task.claimed_by == agent_id:
                return task
        return None

    def render_summary(self) -> str:
        """生成适合提醒和摘要的任务图摘要。"""

        tasks = self._all_tasks()
        if not tasks:
            return "（当前没有任务图任务）"

        ready = [f"task {task.id}" for task in tasks if self._effective_status(task) == "ready"]
        blocked = [f"task {task.id}" for task in tasks if self._effective_status(task) == "blocked"]
        in_progress = [f"task {task.id}" for task in tasks if task.status == "in_progress"]
        integration_pending = [
            f"task {task.id}" for task in tasks if task.status == "integration_pending"
        ]
        completed = [f"task {task.id}" for task in tasks if task.status == "completed"]
        abandoned = [f"task {task.id}" for task in tasks if task.status == "abandoned"]

        return (
            f"ready: {', '.join(ready) if ready else '（无）'}\n"
            f"blocked: {', '.join(blocked) if blocked else '（无）'}\n"
            f"in_progress: {', '.join(in_progress) if in_progress else '（无）'}\n"
            f"integration_pending: {', '.join(integration_pending) if integration_pending else '（无）'}\n"
            f"completed: {', '.join(completed) if completed else '（无）'}\n"
            f"abandoned: {', '.join(abandoned) if abandoned else '（无）'}"
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

    def _lock_path(self, name: str) -> Path:
        """返回锁文件路径。"""

        return self._locks_dir / f"{name}.lock"

    @contextmanager
    def _lock_scope(self, name: str):
        """基于锁文件的最小互斥机制。

        这里不追求复杂的跨机器分布式锁，只解决当前本地 runtime 中：
        - 多个 Agent 同时 create
        - 多个 teammate 同时 claim
        - 多个调用同时 update

        这些“读-改-写”冲突。
        """

        path = self._lock_path(name)
        start_time = time.time()
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                if time.time() - start_time > 5:
                    raise RuntimeError(f"获取任务锁超时：{name}")
                time.sleep(0.05)

        try:
            yield
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    @contextmanager
    def _lock_task(self, task_id: int):
        """锁住单个任务文件，保证更新和认领在锁内重读重写。"""

        with self._lock_scope(f"task_{task_id}"):
            yield

    def _save(self, task: TaskNode) -> None:
        """保存任务文件。

        这里先写临时文件，再 `os.replace(...)` 原子替换，避免留下半截 JSON。
        """

        path = self._task_path(task.id)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)

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
            "pending": {"in_progress", "completed", "cancelled", "abandoned"},
            "in_progress": {"integration_pending", "completed", "pending", "cancelled", "abandoned"},
            "integration_pending": {"in_progress", "completed", "pending", "cancelled", "abandoned"},
            "completed": {"pending"},
            "cancelled": {"pending"},
            "abandoned": {"pending", "in_progress"},
        }
        if new_status not in allowed[task.status]:
            raise ValueError(f"不允许从 {task.status} 变更到 {new_status}")

        if new_status == "in_progress" and self._unmet_dependency_ids(task):
            raise ValueError("当前任务仍被依赖阻塞，不能开始")

    def _effective_status(self, task: TaskNode) -> str:
        """计算派生状态。

        只有 `pending` 会进一步派生为 `ready / blocked`；
        其余状态都直接按持久化状态返回。
        """

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
