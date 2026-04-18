"""用于本地验证 MCP client 链路的最小 mock server。

这个模块不是业务运行时依赖，而是开发期自测用的。
"""

from __future__ import annotations

import json
import sys
from typing import Any


TOOLS = [
    {
        "name": "echo",
        "description": "返回输入的 message，用于验证 MCP 路由和调用链路。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "要回显的文本。"},
            },
            "required": ["message"],
        },
    }
]


def _read_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line == b"\r\n":
            break
        decoded = line.decode("ascii", errors="ignore").strip()
        if ":" in decoded:
            key, value = decoded.split(":", 1)
            headers[key.strip().lower()] = value.strip()

    content_length = int(headers.get("content-length", "0"))
    if content_length <= 0:
        return None

    body = sys.stdin.buffer.read(content_length)
    return json.loads(body.decode("utf-8"))


def _write_message(payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    sys.stdout.buffer.write(header + body)
    sys.stdout.buffer.flush()


def main() -> None:
    while True:
        message = _read_message()
        if message is None:
            return

        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}

        if method == "initialize":
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "mock", "version": "0.1"},
                        "capabilities": {"tools": {}},
                    },
                }
            )
            continue

        if method == "notifications/initialized":
            continue

        if method == "tools/list":
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"tools": TOOLS},
                }
            )
            continue

        if method == "tools/call":
            tool_name = str(params.get("name", "")).strip()
            arguments = params.get("arguments") or {}
            if tool_name == "echo":
                text = str(arguments.get("message", ""))
                _write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [{"type": "text", "text": f"echo: {text}"}],
                        },
                    }
                )
            else:
                _write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32601,
                            "message": f"unknown tool: {tool_name}",
                        },
                    }
                )
            continue

        if request_id is not None:
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"unknown method: {method}",
                    },
                }
            )


if __name__ == "__main__":
    main()
