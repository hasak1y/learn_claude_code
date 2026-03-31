"""父 Agent 专属的同步子代理工具。"""

from __future__ import annotations

from typing import Any

from ..subagents import SubagentRunner
from .base import BaseTool


class TaskTool(BaseTool):
    """把一个独立子任务同步委派给 fresh-context 子代理。"""

    name = "task"
    description = (
        "把一个相对独立的子任务分发给 fresh context 的子代理同步执行。"
        "子代理拥有基础文件与 shell 工具，但没有 task 工具，不能递归创建更多子代理。"
        "只在任务边界清晰、适合单独完成并返回总结时使用。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "交给子代理执行的完整任务描述。",
            }
        },
        "required": ["prompt"],
    }

    def __init__(self, runner: SubagentRunner) -> None:
        self.runner = runner

    def execute(self, arguments: dict[str, Any]) -> str:
        """同步运行子代理，并把最终文本结果返回给父 Agent。"""

        prompt = str(arguments.get("prompt", "")).strip()
        if not prompt:
            return "错误：缺少 'prompt' 参数"

        run_result = self.runner.run_subagent(prompt)

        if run_result.status == "completed":
            return run_result.final_text

        if run_result.status == "max_steps":
            return f"子代理达到最大步数后停止：\n{run_result.final_text}"

        if run_result.status == "cancelled":
            return "子代理任务已取消。"

        detail = run_result.error or run_result.final_text or "未知错误"
        return f"子代理运行失败：\n{detail}"
