"""后台任务工具。"""

from __future__ import annotations

from typing import Any

from ..background_jobs import BackgroundJobManager
from .base import BaseTool


class BackgroundShellTool(BaseTool):
    """启动一个后台 shell 任务。"""

    name = "shell_background"
    description = (
        "启动一个后台 shell 命令。"
        "适合 npm install、pytest、docker build 这类长时间运行的独立命令。"
        "任务完成结果会在后续主循环调用模型前自动注入。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要放到后台执行的 shell 命令。",
            }
        },
        "required": ["command"],
    }

    def __init__(self, manager: BackgroundJobManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        command = str(arguments.get("command", "")).strip()
        if not command:
            return "错误：缺少 'command' 参数"
        return self.manager.spawn_shell(command)


class ListBackgroundJobsTool(BaseTool):
    """列出当前后台任务。"""

    name = "background_job_list"
    description = "列出当前所有后台任务的状态。"
    input_schema = {
        "type": "object",
        "properties": {},
    }

    def __init__(self, manager: BackgroundJobManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        return self.manager.list_jobs()


class GetBackgroundJobResultTool(BaseTool):
    """查看单个后台任务结果。"""

    name = "background_job_result"
    description = "查看单个后台任务的状态或最终输出。"
    input_schema = {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "后台任务 id。",
            }
        },
        "required": ["job_id"],
    }

    def __init__(self, manager: BackgroundJobManager) -> None:
        self.manager = manager

    def execute(self, arguments: dict[str, Any]) -> str:
        job_id = str(arguments.get("job_id", "")).strip()
        if not job_id:
            return "错误：缺少 'job_id' 参数"
        return self.manager.get_result(job_id)
