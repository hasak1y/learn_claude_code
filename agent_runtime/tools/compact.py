"""显式触发上下文压缩的工具。"""

from __future__ import annotations

from typing import Any

from .base import BaseTool


class CompactTool(BaseTool):
    """让模型在合适的时候显式请求压缩上下文。"""

    name = "compact"
    description = (
        "显式压缩当前上下文。"
        "适合在任务阶段切换、上下文变长或已经积累了大量工具结果时使用。"
        "调用后运行时会保留结构化摘要和最近若干条真实消息。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "为什么现在需要压缩，可选但建议填写。",
            }
        },
    }

    def execute(self, arguments: dict[str, Any]) -> str:
        """真正的压缩动作由运行时接管。"""

        return "上下文压缩由运行时执行。"
