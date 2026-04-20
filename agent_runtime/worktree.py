"""受控 worktree 注册表与候选变更集成层。

这一层只负责“代码执行面”和“候选变更”：

1. 某个任务应该在哪个独立工作区里执行
2. 这个工作区属于哪个 teammate / 哪个 task
3. 这个工作区里的 candidate change 当前处于什么 review / integration 状态
4. lead 如何查看 candidate change 并把它 integrate 回主线

注意：
- task graph 负责“做什么、谁在做、状态如何”
- worktree registry 负责“在哪做、目录在哪、对应哪个任务”
- PR 式 candidate change / review / integrate 流程挂在这里，而不是塞进 task graph
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


WorktreeStatus = Literal[
    "active",
    "review_pending",
    "changes_requested",
    "approved",
    "integrated",
    "abandoned",
]


@dataclass(slots=True)
class WorktreeRecord:
    """单条 worktree 分配记录。"""

    task_id: int
    agent_id: str
    branch: str
    path: str
    status: WorktreeStatus
    review_request_id: str | None
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, object]:
        return {
            "taskId": self.task_id,
            "agentId": self.agent_id,
            "branch": self.branch,
            "path": self.path,
            "status": self.status,
            "reviewRequestId": self.review_request_id,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "WorktreeRecord":
        return cls(
            task_id=int(data.get("taskId", 0)),
            agent_id=str(data.get("agentId", "")),
            branch=str(data.get("branch", "")),
            path=str(data.get("path", "")),
            status=str(data.get("status", "active")),  # type: ignore[arg-type]
            review_request_id=(
                str(data.get("reviewRequestId"))
                if data.get("reviewRequestId") is not None
                else None
            ),
            created_at=float(data.get("createdAt", time.time())),
            updated_at=float(data.get("updatedAt", time.time())),
        )


class WorktreeManager:
    """管理受控 git worktree、candidate change 状态与最终 integration。"""

    def __init__(
        self,
        *,
        repo_root: Path,
        registry_root: Path,
        worktree_base_dir: Path,
    ) -> None:
        self.repo_root = repo_root
        self.registry_root = registry_root
        self.registry_root.mkdir(parents=True, exist_ok=True)
        self.records_dir = self.registry_root / "registry"
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir = self.registry_root / "locks"
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        self.worktree_base_dir = worktree_base_dir
        self.worktree_base_dir.mkdir(parents=True, exist_ok=True)

    def ensure_worktree(
        self,
        *,
        task_id: int,
        agent_id: str,
        task_subject: str = "",
    ) -> WorktreeRecord:
        """确保某个 task / agent 对应的 worktree 已经存在。"""

        record_path = self._record_path(task_id)
        with self._lock_scope(f"task_{task_id}"):
            existing = self.get_record(task_id)
            if existing is not None:
                # 允许同一 agent 复用同一个 worktree；
                # 如果同一 task 已经被别的 agent 绑定，则说明控制面出现了冲突。
                if existing.agent_id != agent_id:
                    raise RuntimeError(
                        f"任务 {task_id} 的 worktree 已绑定给 agent '{existing.agent_id}'，"
                        f"不能再分配给 '{agent_id}'。"
                    )
                if self._is_valid_worktree_path(Path(existing.path)):
                    return existing

            branch = self._build_branch_name(task_id=task_id, agent_id=agent_id)
            worktree_path = self._build_worktree_path(
                task_id=task_id,
                agent_id=agent_id,
                task_subject=task_subject,
            )

            if not self._is_valid_worktree_path(worktree_path):
                self._create_git_worktree(path=worktree_path, branch=branch)

            now = time.time()
            record = WorktreeRecord(
                task_id=task_id,
                agent_id=agent_id,
                branch=branch,
                path=str(worktree_path),
                status="active",
                review_request_id=existing.review_request_id if existing is not None else None,
                created_at=existing.created_at if existing is not None else now,
                updated_at=now,
            )
            record_path.write_text(
                json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return record

    def get_record(self, task_id: int) -> WorktreeRecord | None:
        """读取某个任务对应的 worktree 记录。"""

        path = self._record_path(task_id)
        if not path.exists():
            return None
        return WorktreeRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def get_record_for_agent(self, agent_id: str) -> WorktreeRecord | None:
        """读取某个 agent 当前活动 worktree。"""

        for path in sorted(self.records_dir.glob("task_*.json")):
            record = WorktreeRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            if record.agent_id == agent_id and record.status not in {"integrated", "abandoned"}:
                return record
        return None

    def list_records(self, status: str | None = None) -> str:
        """列出当前 worktree 记录。"""

        records = self._all_records()
        if status:
            records = [record for record in records if record.status == status]
        if not records:
            return "当前没有匹配的 worktree 记录。"

        lines = ["当前 worktree 注册表："]
        for record in records:
            lines.append(
                f"- task {record.task_id} -> {record.agent_id} | {record.branch} | {record.status}"
            )
        return "\n".join(lines)

    def snapshot_records(self, status: str | None = None) -> list[dict[str, object]]:
        """返回适合 UI / API 直接消费的 worktree 快照。"""

        records = self._all_records()
        if status is not None:
            records = [record for record in records if record.status == status]
        return [record.to_dict() for record in records]

    def get_record_text(self, task_id: int) -> str:
        """格式化查看单条 worktree 记录。"""

        record = self.get_record(task_id)
        if record is None:
            return f"错误：任务 {task_id} 还没有 worktree 记录。"
        return json.dumps(record.to_dict(), ensure_ascii=False, indent=2)

    def submit_for_review(
        self,
        *,
        task_id: int,
        request_id: str,
    ) -> str:
        """把 worktree 标记为 review_pending，并绑定 review request。

        这个动作对应 PR 模型里的“提交 candidate change，等待 lead review”。
        """

        with self._lock_scope(f"task_{task_id}"):
            record = self.get_record(task_id)
            if record is None:
                return f"错误：任务 {task_id} 还没有 worktree 记录。"
            if record.status == "integrated":
                return f"错误：任务 {task_id} 已经集成，不能再次提交 review。"

            record.status = "review_pending"
            record.review_request_id = request_id
            record.updated_at = time.time()
            self._save_record(record)
            return (
                f"已把 task {task_id} 的候选变更提交为 review_pending。\n"
                f"- request_id: {request_id}\n"
                f"- branch: {record.branch}\n"
                f"- path: {record.path}"
            )

    def apply_review_decision(
        self,
        *,
        task_id: int,
        decision: str,
    ) -> str:
        """把 lead 的 review 决策同步到 candidate change 状态。"""

        decision_map: dict[str, WorktreeStatus] = {
            "approved": "approved",
            "changes_requested": "changes_requested",
            "rejected": "abandoned",
        }
        if decision not in decision_map:
            return f"错误：不支持的 review 决策：{decision}"

        with self._lock_scope(f"task_{task_id}"):
            record = self.get_record(task_id)
            if record is None:
                return f"错误：任务 {task_id} 还没有 worktree 记录。"

            record.status = decision_map[decision]
            record.updated_at = time.time()
            self._save_record(record)
            return (
                f"已更新 task {task_id} 的 review 状态：{record.status}\n"
                f"- branch: {record.branch}\n"
                f"- path: {record.path}"
            )

    def get_diff_text(self, task_id: int) -> str:
        """查看某个候选变更相对于主仓库当前 HEAD 的 diff 摘要。"""

        record = self.get_record(task_id)
        if record is None:
            return f"错误：任务 {task_id} 还没有 worktree 记录。"

        log_result = subprocess.run(
            ["git", "-C", str(self.repo_root), "log", "--oneline", f"HEAD..{record.branch}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        stat_result = subprocess.run(
            ["git", "-C", str(self.repo_root), "diff", "--stat", f"HEAD...{record.branch}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        name_result = subprocess.run(
            ["git", "-C", str(self.repo_root), "diff", "--name-only", f"HEAD...{record.branch}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        if log_result.returncode != 0 or stat_result.returncode != 0 or name_result.returncode != 0:
            return (
                f"错误：读取 task {task_id} 候选变更 diff 失败。\n"
                f"log: {log_result.stderr.strip()}\n"
                f"stat: {stat_result.stderr.strip()}\n"
                f"name: {name_result.stderr.strip()}"
            )

        return (
            f"task {task_id} 候选变更摘要：\n"
            f"- branch: {record.branch}\n"
            f"- path: {record.path}\n"
            f"- status: {record.status}\n"
            f"- request_id: {record.review_request_id or '（无）'}\n\n"
            "[commits]\n"
            f"{log_result.stdout.strip() or '（无）'}\n\n"
            "[changed_files]\n"
            f"{name_result.stdout.strip() or '（无）'}\n\n"
            "[diff_stat]\n"
            f"{stat_result.stdout.strip() or '（无）'}"
        )

    def integrate(self, task_id: int) -> str:
        """把已通过 review 的候选变更 merge 回主仓库当前分支。"""

        with self._lock_scope(f"task_{task_id}"):
            record = self.get_record(task_id)
            if record is None:
                return f"错误：任务 {task_id} 还没有 worktree 记录。"
            if record.status != "approved":
                return (
                    f"错误：task {task_id} 当前状态为 {record.status}，"
                    "只有 approved 的候选变更才能被集成。"
                )

            merge_result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.repo_root),
                    "merge",
                    "--no-ff",
                    record.branch,
                    "-m",
                    f"Merge task {task_id} from {record.agent_id}",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if merge_result.returncode != 0:
                return (
                    f"错误：集成 task {task_id} 失败。\n"
                    f"{merge_result.stderr.strip() or merge_result.stdout.strip()}"
                )

            record.status = "integrated"
            record.updated_at = time.time()
            self._save_record(record)
            return (
                f"已把 task {task_id} 的候选变更集成到主仓库当前分支。\n"
                f"- branch: {record.branch}\n"
                f"- path: {record.path}"
            )

    def render_summary(self) -> str:
        """生成简短注册表摘要。"""

        records = self._all_records()
        if not records:
            return "（当前没有已分配的 worktree）"

        lines = ["当前 worktree 注册表："]
        for record in records:
            lines.append(
                f"- task {record.task_id} -> {record.agent_id} | {record.path} | {record.status}"
            )
        return "\n".join(lines)

    def _all_records(self) -> list[WorktreeRecord]:
        records: list[WorktreeRecord] = []
        for path in sorted(self.records_dir.glob("task_*.json")):
            records.append(
                WorktreeRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            )
        return records

    def _save_record(self, record: WorktreeRecord) -> None:
        self._record_path(record.task_id).write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _record_path(self, task_id: int) -> Path:
        return self.records_dir / f"task_{task_id}.json"

    @staticmethod
    def _slugify(value: str) -> str:
        filtered = [
            char.lower()
            for char in value
            if char.isalnum() or char in {"-", "_"}
        ]
        slug = "".join(filtered).strip("-_")
        return slug or "task"

    def _build_branch_name(self, *, task_id: int, agent_id: str) -> str:
        safe_agent = self._slugify(agent_id)
        return f"codex/task-{task_id}-{safe_agent}"

    def _build_worktree_path(self, *, task_id: int, agent_id: str, task_subject: str) -> Path:
        subject_slug = self._slugify(task_subject)[:24]
        safe_agent = self._slugify(agent_id)
        name = f"task-{task_id}-{safe_agent}"
        if subject_slug:
            name += f"-{subject_slug}"
        return self.worktree_base_dir / name

    @staticmethod
    def _is_valid_worktree_path(path: Path) -> bool:
        """判断目录是否看起来已经是一个有效 git worktree。"""

        return path.exists() and (path / ".git").exists()

    def _create_git_worktree(self, *, path: Path, branch: str) -> None:
        """实际创建 git worktree。"""

        path.parent.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo_root),
                "worktree",
                "add",
                "-b",
                branch,
                str(path),
                "HEAD",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode == 0:
            return

        fallback = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo_root),
                "worktree",
                "add",
                str(path),
                branch,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if fallback.returncode == 0:
            return

        raise RuntimeError(
            "创建 worktree 失败："
            f"{result.stderr.strip() or result.stdout.strip() or fallback.stderr.strip()}"
        )

    @contextmanager
    def _lock_scope(self, name: str):
        """基于锁文件的最小互斥。"""

        path = self.locks_dir / f"{name}.lock"
        start_time = time.time()
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                if time.time() - start_time > 5:
                    raise RuntimeError(f"获取 worktree 锁超时：{name}")
                time.sleep(0.05)

        try:
            yield
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
