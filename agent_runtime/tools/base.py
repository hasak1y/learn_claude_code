"""工具接口与统一注册表。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..mcp_client import MCPClient
from ..tool_router import ToolRouter, ToolSpec


class BaseTool(ABC):
    """所有本地工具都遵循的基础接口。"""

    name: str
    description: str
    input_schema: dict[str, Any]

    @abstractmethod
    def execute(self, arguments: dict[str, Any]) -> str:
        """执行工具，并返回纯文本结果。"""

    def to_tool_spec(self) -> ToolSpec:
        """把本地工具转换成统一工具描述。"""

        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            source="local",
            display_name=self.name,
            local_tool=self,
        )

    def to_openai_tool_schema(self) -> dict[str, Any]:
        """兼容旧调用方：仍然允许本地工具自己暴露 schema。"""

        return self.to_tool_spec().to_openai_tool_schema()


class ToolRegistry:
    """统一注册本地工具和 MCP 工具的注册表。

    registry 负责“有哪些工具”；
    router 负责“怎么执行工具”。
    """

    def __init__(
        self,
        tools: list[BaseTool],
        *,
        mcp_client: MCPClient | None = None,
        mcp_server_names: list[str] | None = None,
    ) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._router = ToolRouter(mcp_client=mcp_client)
        self._mcp_client = mcp_client
        self._mcp_server_names = mcp_server_names

        for tool in tools:
            spec = tool.to_tool_spec()
            self._specs[spec.name] = spec

        if self._mcp_client is not None and self._mcp_client.has_servers():
            self._register_mcp_tools(server_names=self._mcp_server_names)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """按 LLM 适配层需要的格式返回工具定义。"""

        return [spec.to_openai_tool_schema() for spec in self._specs.values()]

    def get_tool_display_names(self) -> list[str]:
        """返回更适合给用户展示的工具名。"""

        return [spec.shown_name for spec in self._specs.values()]

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """根据工具名执行工具。"""

        spec = self._specs.get(name)
        if spec is None:
            return f"错误：未知工具 '{name}'"

        return self._router.execute(spec, arguments)

    def _register_mcp_tools(self, *, server_names: list[str] | None) -> None:
        assert self._mcp_client is not None
        for tool in self._mcp_client.list_tools(server_names=server_names):
            spec = ToolSpec(
                name=tool.full_name,
                description=tool.description,
                input_schema=tool.input_schema,
                source="mcp",
                display_name=tool.canonical_name,
                mcp_server=tool.server_name,
                mcp_tool_name=tool.tool_name,
            )
            self._specs[spec.name] = spec
