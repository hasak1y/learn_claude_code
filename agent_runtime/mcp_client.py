"""最小 MCP client。

这一层只负责三件事：
1. 连接 MCP server
2. 拉取工具列表
3. 调用远端工具

当前支持两类 transport：
- stdio：本地命令进程，通过 stdin/stdout 走 JSON-RPC
- http：远端 URL，通过 HTTP POST + JSON-RPC，兼容 JSON 与 SSE 响应

路由决策不放在这里，路由由 tool router 负责。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal, Protocol


MCPTransport = Literal["stdio", "http"]


@dataclass(slots=True)
class MCPServerConfig:
    """单个 MCP server 的连接配置。"""

    name: str
    transport: MCPTransport = "stdio"
    command: str | None = None
    args: list[str] | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    tool_patterns: tuple[str, ...] = ()


@dataclass(slots=True)
class MCPToolDescriptor:
    """从 MCP server 拉回来的工具描述。"""

    server_name: str
    tool_name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def full_name(self) -> str:
        """返回真正注册给模型的安全工具名。

        OpenAI-compatible tools 的 name 只能包含字母、数字、下划线和短横线，
        不能直接使用点号，所以这里把逻辑上的 mcp.<server>.<tool>
        映射成 mcp__<server>__<tool>。
        """

        return f"mcp__{self.server_name}__{self.tool_name}"

    @property
    def canonical_name(self) -> str:
        """返回更适合文档、权限和日志使用的逻辑工具名。"""

        return f"mcp.{self.server_name}.{self.tool_name}"


class _JsonRpcPeer(Protocol):
    """统一 transport 的最小 JSON-RPC 对外接口。"""

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        ...

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        ...


def load_mcp_server_configs(
    *,
    repo_root: Path,
    env_var: str = "MCP_SERVERS_JSON",
) -> list[MCPServerConfig]:
    """从环境变量或 .mcp/servers.json 加载 MCP server 配置。"""

    raw = os.getenv(env_var, "").strip()
    payload: object | None = None

    if raw:
        payload = json.loads(raw)
    else:
        config_path = repo_root / ".mcp" / "servers.json"
        if config_path.exists():
            payload = json.loads(config_path.read_text(encoding="utf-8"))

    if payload is None:
        return []

    if isinstance(payload, dict) and "servers" in payload:
        payload = payload["servers"]

    configs: list[MCPServerConfig] = []
    if not isinstance(payload, dict):
        raise ValueError('MCP server 配置必须是对象，格式如 {"servers": {...}}')

    for name, item in payload.items():
        if not isinstance(name, str) or not isinstance(item, dict):
            continue

        transport = str(item.get("transport", "stdio")).strip().lower() or "stdio"
        if transport not in {"stdio", "http"}:
            raise ValueError(f"MCP server '{name}' 使用了未知 transport: {transport}")

        headers_value = item.get("headers")
        headers = (
            {str(k): str(v) for k, v in headers_value.items()}
            if isinstance(headers_value, dict)
            else None
        )
        tool_patterns = _normalize_tool_patterns(item.get("tools"))

        if transport == "http":
            url = str(item.get("url", "")).strip()
            if not url:
                raise ValueError(f"MCP http server '{name}' 缺少 url")
            configs.append(
                MCPServerConfig(
                    name=name,
                    transport="http",
                    url=url,
                    headers=headers,
                    tool_patterns=tool_patterns,
                )
            )
            continue

        command = str(item.get("command", "")).strip()
        if not command:
            raise ValueError(f"MCP stdio server '{name}' 缺少 command")

        args_value = item.get("args", [])
        if isinstance(args_value, list):
            args = [str(arg) for arg in args_value]
        else:
            args = []

        cwd_value = item.get("cwd")
        cwd = str(cwd_value) if cwd_value else None

        env_value = item.get("env")
        env = (
            {str(k): str(v) for k, v in env_value.items()}
            if isinstance(env_value, dict)
            else None
        )

        configs.append(
            MCPServerConfig(
                name=name,
                transport="stdio",
                command=command,
                args=args,
                cwd=cwd,
                env=env,
                headers=headers,
                tool_patterns=tool_patterns,
            )
        )

    return configs


class _StdioJsonRpcPeer:
    """基于 stdio + Content-Length 的最小 JSON-RPC 2.0 通信层。"""

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._process: subprocess.Popen[bytes] | None = None
        self._id_lock = threading.Lock()
        self._next_id = 1

    def start(self) -> None:
        if self._process is not None:
            return

        if not self._config.command:
            raise RuntimeError(f"MCP server '{self._config.name}' 缺少 command")

        env = os.environ.copy()
        if self._config.env:
            env.update(self._config.env)

        self._process = subprocess.Popen(
            [self._config.command, *(self._config.args or [])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self._config.cwd,
            env=env,
        )

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.start()
        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError(f"MCP server '{self._config.name}' 未成功启动")

        with self._id_lock:
            request_id = self._next_id
            self._next_id += 1

        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self._process.stdin.write(header + body)
        self._process.stdin.flush()

        response = self._read_message()
        if response.get("id") != request_id:
            raise RuntimeError(
                f"MCP server '{self._config.name}' 返回了错乱的响应 id: {response.get('id')}"
            )
        if "error" in response:
            raise RuntimeError(
                f"MCP server '{self._config.name}' 调用 {method} 失败: {response['error']}"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(
                f"MCP server '{self._config.name}' 调用 {method} 返回了非法结果: {result!r}"
            )
        return result

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.start()
        if self._process is None or self._process.stdin is None:
            raise RuntimeError(f"MCP server '{self._config.name}' 未成功启动")

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self._process.stdin.write(header + body)
        self._process.stdin.flush()

    def _read_message(self) -> dict[str, Any]:
        assert self._process is not None and self._process.stdout is not None
        headers: dict[str, str] = {}
        while True:
            line = self._process.stdout.readline()
            if not line:
                stderr_text = self._read_stderr_tail()
                suffix = f" stderr={stderr_text}" if stderr_text else ""
                raise RuntimeError(f"MCP server '{self._config.name}' 提前断开连接。{suffix}")
            if line == b"\r\n":
                break
            decoded = line.decode("ascii", errors="ignore").strip()
            if ":" in decoded:
                key, value = decoded.split(":", 1)
                headers[key.strip().lower()] = value.strip()

        content_length = int(headers.get("content-length", "0"))
        if content_length <= 0:
            raise RuntimeError(f"MCP server '{self._config.name}' 返回了空消息")

        body = self._process.stdout.read(content_length)
        return json.loads(body.decode("utf-8"))

    def _read_stderr_tail(self) -> str:
        if self._process is None or self._process.stderr is None:
            return ""
        try:
            data = self._process.stderr.read1(2048)
        except Exception:
            return ""
        if not data:
            return ""
        return data.decode("utf-8", errors="replace").strip()


class _HttpJsonRpcPeer:
    """基于 HTTP POST 的最小 MCP transport。

    兼容两类响应：
    - application/json
    - text/event-stream / SSE 风格的 data: {...}

    同时支持 MCP 的 mcp-session-id，会在 initialize 后自动保存并带到后续请求里。
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._id_lock = threading.Lock()
        self._next_id = 1
        self._session_id: str | None = None

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._id_lock:
            request_id = self._next_id
            self._next_id += 1

        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        response = self._post(payload=payload, expect_id=request_id)
        if "error" in response:
            raise RuntimeError(
                f"MCP server '{self._config.name}' 调用 {method} 失败: {response['error']}"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(
                f"MCP server '{self._config.name}' 调用 {method} 返回了非法结果: {result!r}"
            )
        return result

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        self._post(payload=payload, expect_id=None, allow_empty=True)

    def _post(
        self,
        *,
        payload: dict[str, Any],
        expect_id: int | None,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        if not self._config.url:
            raise RuntimeError(f"MCP http server '{self._config.name}' 缺少 url")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._config.headers:
            headers.update(self._config.headers)
        if self._session_id:
            headers["mcp-session-id"] = self._session_id

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._config.url,
            data=data,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw_headers = dict(response.headers.items())
                session_id = raw_headers.get("mcp-session-id")
                if session_id:
                    self._session_id = session_id
                raw_body = response.read().decode("utf-8", errors="replace")
                content_type = response.headers.get_content_type()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"MCP server '{self._config.name}' HTTP {exc.code}: {body or exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"MCP server '{self._config.name}' 网络请求失败: {exc.reason}"
            ) from exc

        if not raw_body.strip():
            if allow_empty:
                return {}
            raise RuntimeError(f"MCP server '{self._config.name}' 返回空响应")

        message = self._parse_http_response(
            raw_body=raw_body,
            content_type=content_type,
            expect_id=expect_id,
        )
        if message is None:
            if allow_empty:
                return {}
            raise RuntimeError(f"MCP server '{self._config.name}' 返回了不可解析的响应")
        return message

    @staticmethod
    def _parse_http_response(
        *,
        raw_body: str,
        content_type: str,
        expect_id: int | None,
    ) -> dict[str, Any] | None:
        if content_type == "application/json":
            payload = json.loads(raw_body)
            if isinstance(payload, dict):
                return payload
            return None

        # 兼容 text/event-stream 以及服务端返回 SSE 文本但 content-type 不稳定的情况。
        event_payloads = _extract_sse_json_messages(raw_body)
        if not event_payloads:
            # 有些服务会直接返回 JSON 文本但 content-type 不标准，最后再试一次。
            try:
                payload = json.loads(raw_body)
            except json.JSONDecodeError:
                return None
            return payload if isinstance(payload, dict) else None

        if expect_id is None:
            return event_payloads[-1]

        for item in event_payloads:
            if item.get("id") == expect_id:
                return item
        return event_payloads[-1]


def _extract_sse_json_messages(raw_body: str) -> list[dict[str, Any]]:
    """从 SSE 文本里提取 data: 对应的 JSON-RPC 消息。"""

    messages: list[dict[str, Any]] = []
    current_data_lines: list[str] = []

    def flush() -> None:
        if not current_data_lines:
            return
        payload = "\n".join(current_data_lines).strip()
        current_data_lines.clear()
        if not payload:
            return
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            return
        if isinstance(decoded, dict):
            messages.append(decoded)

    for raw_line in raw_body.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            flush()
            continue
        if line.startswith("data:"):
            current_data_lines.append(line[5:].lstrip())

    flush()
    return messages


class MCPClient:
    """管理多个 MCP server 连接，并提供工具发现与调用。"""

    def __init__(self, server_configs: list[MCPServerConfig]) -> None:
        self._configs = {config.name: config for config in server_configs}
        self._peers: dict[str, _JsonRpcPeer] = {}
        self._initialized: set[str] = set()

    def has_servers(self) -> bool:
        return bool(self._configs)

    def list_tools(self, server_names: list[str] | None = None) -> list[MCPToolDescriptor]:
        names = server_names or list(self._configs.keys())
        tools: list[MCPToolDescriptor] = []

        for server_name in names:
            peer = self._get_peer(server_name)
            self._ensure_initialized(server_name, peer)
            result = peer.request("tools/list")
            raw_tools = result.get("tools", [])
            if not isinstance(raw_tools, list):
                continue
            config = self._configs[server_name]

            for item in raw_tools:
                if not isinstance(item, dict):
                    continue
                tool_name = str(item.get("name", "")).strip()
                if not tool_name:
                    continue
                if config.tool_patterns and not any(
                    fnmatchcase(tool_name, pattern) for pattern in config.tool_patterns
                ):
                    continue
                description = str(item.get("description", "")).strip()
                input_schema = item.get("inputSchema", {})
                if not isinstance(input_schema, dict):
                    input_schema = {}
                tools.append(
                    MCPToolDescriptor(
                        server_name=server_name,
                        tool_name=tool_name,
                        description=description,
                        input_schema=input_schema,
                    )
                )

        return tools

    def call_tool(self, *, server_name: str, tool_name: str, arguments: dict[str, Any]) -> str:
        peer = self._get_peer(server_name)
        self._ensure_initialized(server_name, peer)
        result = peer.request(
            "tools/call",
            {
                "name": tool_name,
                "arguments": arguments,
            },
        )
        return self._render_call_result(result)

    def _get_peer(self, server_name: str) -> _JsonRpcPeer:
        if server_name not in self._configs:
            raise RuntimeError(f"未知 MCP server: {server_name}")
        if server_name not in self._peers:
            config = self._configs[server_name]
            if config.transport == "http":
                self._peers[server_name] = _HttpJsonRpcPeer(config)
            else:
                self._peers[server_name] = _StdioJsonRpcPeer(config)
        return self._peers[server_name]

    def _ensure_initialized(self, server_name: str, peer: _JsonRpcPeer) -> None:
        if server_name in self._initialized:
            return

        peer.request(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "clientInfo": {
                    "name": "learn-claude-code",
                    "version": "0.1",
                },
            },
        )
        peer.notify("notifications/initialized")
        self._initialized.add(server_name)

    @staticmethod
    def _render_call_result(result: dict[str, Any]) -> str:
        content = result.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type", "")).strip()
                if item_type == "text":
                    parts.append(str(item.get("text", "")))
                elif item_type:
                    parts.append(json.dumps(item, ensure_ascii=False))
            if parts:
                return "\n".join(parts)

        structured = result.get("structuredContent")
        if structured is not None:
            return json.dumps(structured, ensure_ascii=False, indent=2)

        if result.get("isError"):
            return f"错误：MCP 工具调用失败：{json.dumps(result, ensure_ascii=False)}"

        return json.dumps(result, ensure_ascii=False, indent=2)


def _normalize_tool_patterns(raw_value: object) -> tuple[str, ...]:
    """把 server 级工具过滤配置统一成通配符元组。"""

    if raw_value is None:
        return ()
    if isinstance(raw_value, str):
        values = [item.strip() for item in raw_value.split(",")]
    elif isinstance(raw_value, list):
        values = [str(item).strip() for item in raw_value]
    else:
        return ()
    return tuple(value for value in values if value)
