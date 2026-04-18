"""统一工具路由层。

router 只做一件事：
- 本地工具 -> 本地 handler
- MCP 工具 -> MCP client
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .mcp_client import MCPClient

class LocalToolHandler(Protocol):
    def execute(self, arguments: dict[str, Any]) -> str: ...


ToolSource = Literal["local", "mcp"]


@dataclass(slots=True)
class ToolSpec:
    """统一工具描述。

    工具描述和工具执行分离以后，后续可以更自然地支持：
    - 本地工具
    - MCP 工具
    - 其他远端工具来源
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    source: ToolSource
    display_name: str | None = None
    local_tool: LocalToolHandler | None = None
    mcp_server: str | None = None
    mcp_tool_name: str | None = None

    @property
    def shown_name(self) -> str:
        return self.display_name or self.name

    def to_openai_tool_schema(self) -> dict[str, Any]:
        description = self.description
        if self.display_name and self.display_name != self.name:
            description = f"{description}\n逻辑名: {self.display_name}".strip()
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,
                "parameters": self.input_schema,
            },
        }


class ToolRouter:
    """按工具来源把执行分发到本地或 MCP。"""

    def __init__(self, mcp_client: MCPClient | None = None) -> None:
        self._mcp_client = mcp_client

    def execute(self, spec: ToolSpec, arguments: dict[str, Any]) -> str:
        if spec.source == "local":
            if spec.local_tool is None:
                return f"错误：本地工具 '{spec.name}' 缺少 handler"
            return spec.local_tool.execute(arguments)

        if spec.source == "mcp":
            if self._mcp_client is None:
                return f"错误：MCP client 未初始化，无法执行 '{spec.name}'"
            if spec.mcp_server is None or spec.mcp_tool_name is None:
                return f"错误：MCP 工具 '{spec.name}' 缺少 server 或 tool_name"
            return self._mcp_client.call_tool(
                server_name=spec.mcp_server,
                tool_name=spec.mcp_tool_name,
                arguments=arguments,
            )

        return f"错误：未知工具来源 '{spec.source}'"
