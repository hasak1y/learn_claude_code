"""worktree 注册表与候选变更集成工具。"""

from __future__ import annotations

from typing import Any

from ..team import TeamManager
from ..task_graph import TaskGraphManager
from ..worktree import WorktreeManager
from .base import BaseTool


class ListWorktreesTool(BaseTool):
    """列出当前 worktree 注册表。"""

    name = "worktree_list_records"
    description = "列出当前 worktree 注册表，可按状态过滤。"
    input_schema = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "可选，按 worktree 状态过滤，例如 review_pending / approved。",
            }
        },
    }

    def __init__(self, manager: WorktreeManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        status = str(arguments.get("status", "")).strip() or None
        return self.manager.list_records(status=status)


class GetWorktreeRecordTool(BaseTool):
    """查看单条 worktree 记录。"""

    name = "worktree_get_record"
    description = "查看某个 task 对应的 worktree 记录。"
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "任务 id。"},
        },
        "required": ["task_id"],
    }

    def __init__(self, manager: WorktreeManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        try:
            task_id = int(arguments.get("task_id"))
        except (TypeError, ValueError):
            return "错误：task_id 必须是整数"
        return self.manager.get_record_text(task_id)


class GetWorktreeDiffTool(BaseTool):
    """查看候选变更摘要。"""

    name = "worktree_get_diff"
    description = "查看某个候选变更相对于主仓库当前 HEAD 的 diff 摘要。"
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "任务 id。"},
        },
        "required": ["task_id"],
    }

    def __init__(self, manager: WorktreeManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        try:
            task_id = int(arguments.get("task_id"))
        except (TypeError, ValueError):
            return "错误：task_id 必须是整数"
        return self.manager.get_diff_text(task_id)


class SubmitWorktreeForReviewTool(BaseTool):
    """把当前 task 的 worktree 提交成 review 候选。"""

    name = "worktree_submit_for_review"
    description = (
        "把某个 task 的 worktree 标记为 review_pending，并向 lead 发起 integration_request。"
        "这相当于 PR 模型里的“提交候选变更等待审查”。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "任务 id。"},
            "summary": {"type": "string", "description": "候选变更摘要。"},
            "content": {"type": "string", "description": "补充说明，可选。"},
        },
        "required": ["task_id", "summary"],
    }

    def __init__(
        self,
        *,
        worktree_manager: WorktreeManager,
        team_manager: TeamManager,
        task_graph_manager: TaskGraphManager,
        sender_id: str,
    ) -> None:
        self.worktree_manager = worktree_manager
        self.team_manager = team_manager
        self.task_graph_manager = task_graph_manager
        self.sender_id = sender_id

    def execute(self, arguments: dict[str, Any]) -> str:
        try:
            task_id = int(arguments.get("task_id"))
        except (TypeError, ValueError):
            return "错误：task_id 必须是整数"

        summary = str(arguments.get("summary", "")).strip()
        if not summary:
            return "错误：summary 不能为空"
        content = str(arguments.get("content", ""))

        record = self.worktree_manager.get_record(task_id)
        if record is None:
            return f"错误：task {task_id} 还没有 worktree 记录。"
        if record.agent_id != self.sender_id:
            return (
                f"错误：task {task_id} 的 worktree 属于 '{record.agent_id}'，"
                f"当前发送方是 '{self.sender_id}'。"
            )

        try:
            request_record = self.team_manager.create_request_record(
                sender=self.sender_id,
                recipient="lead",
                action="integration_request",
                summary=summary,
                content=content,
            )
        except ValueError as exc:
            return f"错误：{exc}"

        review_result = self.worktree_manager.submit_for_review(
            task_id=task_id,
            request_id=request_record.request_id,
        )
        if not review_result.startswith("已把 task"):
            return review_result

        # 候选变更已提交审查，但尚未进入主线，因此这里只能进入 integration_pending，
        # 绝不能提前标记成 completed。
        try:
            task_node = self.task_graph_manager.get_node(task_id)
        except ValueError as exc:
            return f"{review_result}\n\n警告：无法同步任务状态：{exc}"

        task_result = self.task_graph_manager.update(
            task_id=task_id,
            base_version=task_node.version,
            status="integration_pending",
        )
        request_result = (
            "已创建 protocol request：\n"
            f"- request_id: {request_record.request_id}\n"
            f"- action: integration_request\n"
            f"- from: {self.sender_id}\n"
            "- to: lead\n"
            "- status: pending"
        )
        return f"{review_result}\n\n{request_result}\n\n{task_result}"


class DecideWorktreeReviewTool(BaseTool):
    """lead 对候选变更做 review 决策。"""

    name = "worktree_review_decision"
    description = (
        "lead 对某个候选变更做 review 决策，同时更新 protocol request 和 worktree 状态。"
        "支持 approved / changes_requested / rejected。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "任务 id。"},
            "request_id": {"type": "string", "description": "对应的 integration_request id。"},
            "decision": {
                "type": "string",
                "enum": ["approved", "changes_requested", "rejected"],
                "description": "review 决策。",
            },
            "response_text": {"type": "string", "description": "给 teammate 的 review 反馈。"},
        },
        "required": ["task_id", "request_id", "decision"],
    }

    def __init__(
        self,
        *,
        worktree_manager: WorktreeManager,
        team_manager: TeamManager,
        task_graph_manager: TaskGraphManager,
        sender_id: str,
    ) -> None:
        self.worktree_manager = worktree_manager
        self.team_manager = team_manager
        self.task_graph_manager = task_graph_manager
        self.sender_id = sender_id

    def execute(self, arguments: dict[str, Any]) -> str:
        try:
            task_id = int(arguments.get("task_id"))
        except (TypeError, ValueError):
            return "错误：task_id 必须是整数"

        request_id = str(arguments.get("request_id", "")).strip()
        if not request_id:
            return "错误：request_id 不能为空"

        decision = str(arguments.get("decision", "")).strip()
        response_text = str(arguments.get("response_text", ""))

        protocol_result = self.team_manager.respond_request(
            responder=self.sender_id,
            request_id=request_id,
            status=decision,  # type: ignore[arg-type]
            response_text=response_text,
        )
        if not protocol_result.startswith("已更新 protocol request"):
            return protocol_result

        review_result = self.worktree_manager.apply_review_decision(
            task_id=task_id,
            decision=decision,
        )
        if not review_result.startswith("已更新 task"):
            return review_result

        task_followup = ""
        if decision in {"changes_requested", "rejected"}:
            try:
                task_node = self.task_graph_manager.get_node(task_id)
            except ValueError:
                task_followup = ""
            else:
                # 审查未通过时，任务回到 pending，等待后续重新认领或再次修改。
                task_followup = self.task_graph_manager.update(
                    task_id=task_id,
                    base_version=task_node.version,
                    status="pending",
                )

        return "\n\n".join(
            item for item in [review_result, protocol_result, task_followup] if item
        )


class IntegrateWorktreeTool(BaseTool):
    """把 approved 的候选变更集成回主线。"""

    name = "worktree_integrate"
    description = (
        "把已通过 review 的候选变更 merge 回主仓库当前分支。"
        "只允许对 approved 状态的候选变更执行。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "任务 id。"},
        },
        "required": ["task_id"],
    }

    def __init__(self, *, manager: WorktreeManager, task_graph_manager: TaskGraphManager) -> None:
        self.manager = manager
        self.task_graph_manager = task_graph_manager

    def execute(self, arguments: dict[str, Any]) -> str:
        try:
            task_id = int(arguments.get("task_id"))
        except (TypeError, ValueError):
            return "错误：task_id 必须是整数"
        integrate_result = self.manager.integrate(task_id)
        if not integrate_result.startswith("已把 task"):
            return integrate_result

        try:
            task_node = self.task_graph_manager.get_node(task_id)
        except ValueError as exc:
            return f"{integrate_result}\n\n警告：无法把任务同步标记为 completed：{exc}"

        task_result = self.task_graph_manager.update(
            task_id=task_id,
            base_version=task_node.version,
            status="completed",
        )
        return f"{integrate_result}\n\n{task_result}"
