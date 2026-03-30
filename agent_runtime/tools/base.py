"""工具接口和工具注册表。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseTool(ABC):
    """所有工具都要遵循的基础接口。

    每个工具都需要提供：
    - 对外暴露的工具名
    - 给模型看的文字描述
    - 描述入参的 JSON Schema
    - 真正执行逻辑
    """

    name: str
    description: str
    input_schema: dict[str, Any]

    @abstractmethod
    def execute(self, arguments: dict[str, Any]) -> str:
        """执行工具，并返回纯文本结果。"""

    def to_openai_tool_schema(self) -> dict[str, Any]:
        """把工具暴露成 OpenAI-compatible 的函数工具格式。"""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ToolRegistry:
    """把工具名映射到工具实例上的小型注册表。

    注册表把查找和执行逻辑集中在一起，
    这样循环层就不需要感知具体工具类。
    """

    def __init__(self, tools: list[BaseTool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """按 LLM 适配层需要的格式返回工具定义。"""

        return [tool.to_openai_tool_schema() for tool in self._tools.values()]

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """根据工具名执行工具。"""

        tool = self._tools.get(name)
        if tool is None:
            return f"错误：未知工具 '{name}'"

        return tool.execute(arguments)
