"""OpenAI-compatible LLM 适配器。

这个适配器面向 `chat/completions` 风格的接口，
并使用 OpenAI-compatible 的工具调用格式。
很多模型服务都会暴露兼容接口，所以它很适合作为项目第一版的适配器。
"""

from __future__ import annotations

import http.client
import json
import time
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from ..config import OpenAICompatibleConfig
from ..types import ConversationMessage, LLMResponse, ToolCall
from .base import BaseLLMClient


DEFAULT_HTTP_HEADERS = {
    # 这里对齐 `api_test` 里已经验证可用的默认请求头。
    # 某些中转服务会根据请求特征做拦截，默认 Python 请求头不一定能通过。
    "User-Agent": "api-client/3.0",
    "Accept": "application/json",
}


class OpenAICompatibleLLMClient(BaseLLMClient):
    """一个面向 `/chat/completions` 工具调用接口的小型客户端。

    这个类主要负责三件事：
    - 把内部消息格式转换成接口请求体
    - 发送 HTTP 请求
    - 把模型提供方的响应再转换回内部统一格式

    上层的 Agent 循环不应该直接接触原始 HTTP 或厂商私有 JSON。
    """

    def __init__(self, config: OpenAICompatibleConfig) -> None:
        self.config = config

    def generate(
        self,
        messages: list[ConversationMessage],
        tools: list[dict[str, Any]],
        system_prompt: str,
    ) -> LLMResponse:
        """发送一次生成请求，并把结果转换成统一结构。"""

        payload = {
            "model": self.config.model,
            "messages": self._build_api_messages(messages, system_prompt),
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            # 默认开启流式，便于更快拿到响应，同时保持工具调用兼容
            "stream": True,
        }

        # 只有在确实存在工具定义时，才把工具相关字段发给服务端。
        # 某些中转服务对 `"tools": []` 的兼容性很差，普通聊天反而会因此失败。
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        url = self._build_api_url("/v1/chat/completions")
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }
        headers.update(DEFAULT_HTTP_HEADERS)

        http_request = request.Request(url=url, data=body, headers=headers, method="POST")

        return self._generate_via_stream(http_request=http_request, url=url)

    def _generate_via_stream(self, http_request: request.Request, url: str) -> LLMResponse:
        """使用流式输出读取结果，并整理成统一响应结构。"""

        raw_response = self._post_with_retries(http_request=http_request, url=url)
        data = self._parse_stream_response(raw_response)
        return self._parse_api_response(data)

    def _post_with_retries(self, http_request: request.Request, url: str) -> str:
        """发送请求，并对短暂的网络断连做有限重试。"""

        last_error: Exception | None = None
        max_attempts = 3

        for attempt in range(1, max_attempts + 1):
            try:
                with request.urlopen(http_request, timeout=self.config.timeout_seconds) as response:
                    return response.read().decode("utf-8")
            except HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="replace")
                error_message = (
                    f"LLM 请求失败，HTTP {exc.code}，请求地址：{url}，响应内容：{error_body}"
                )

                # 403 往往不是程序结构问题，而是接口地址、权限策略、
                # 账号权限或代理服务本身拒绝了这次请求。
                if exc.code == 403:
                    error_message += (
                        "。这通常表示当前接口拒绝访问。"
                        "请优先检查 LLM_BASE_URL 是否正确、API Key 是否可用，"
                        "以及当前服务是否允许你的来源 IP 或账号访问。"
                    )

                raise RuntimeError(error_message) from exc
            except (
                URLError,
                TimeoutError,
                ConnectionResetError,
                http.client.RemoteDisconnected,
            ) as exc:
                last_error = exc
                if attempt < max_attempts:
                    time.sleep(0.8 * attempt)
                    continue

                raise RuntimeError(
                    "LLM 请求失败，连接在服务端响应前被中断。"
                    f"请求地址：{url}；原始错误：{exc}"
                ) from exc
            except OSError as exc:
                last_error = exc
                if attempt < max_attempts:
                    time.sleep(0.8 * attempt)
                    continue

                raise RuntimeError(
                    f"LLM 请求失败，请检查网络或代理连接。请求地址：{url}；原始错误：{exc}"
                ) from exc

        raise RuntimeError(f"LLM 请求失败：{last_error}")

    def _build_api_url(self, path: str) -> str:
        """根据 base_url 拼出最终接口地址。

        这里兼容两种常见写法：
        - `LLM_BASE_URL=https://api.openai.com/v1`
        - `LLM_BASE_URL=https://some-proxy.example.com`

        如果 base_url 已经以 `/v1` 结尾，就直接拼接去掉前缀后的路径。
        如果没有，就自动补上 `/v1`，和 `api_test` 项目的行为保持一致。
        """

        base_url = self.config.base_url.rstrip("/")
        normalized_path = path.strip()

        if not normalized_path.startswith("/"):
            normalized_path = f"/{normalized_path}"

        if base_url.endswith("/v1") and normalized_path.startswith("/v1/"):
            return f"{base_url}{normalized_path[3:]}"

        if base_url.endswith("/v1"):
            return f"{base_url}{normalized_path}"

        return f"{base_url}{normalized_path}"

    def _build_api_messages(
        self,
        messages: list[ConversationMessage],
        system_prompt: str,
    ) -> list[dict[str, Any]]:
        """把内部消息结构转换成 OpenAI-compatible 消息格式。"""

        api_messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

        for message in messages:
            if message.role == "user":
                api_messages.append({"role": "user", "content": message.content})
                continue

            if message.role == "assistant":
                api_message: dict[str, Any] = {
                    "role": "assistant",
                    "content": message.content or "",
                }
                if message.tool_calls:
                    api_message["tool_calls"] = [
                        {
                            "id": tool_call.id,
                            "type": "function",
                            "function": {
                                "name": tool_call.name,
                                "arguments": json.dumps(tool_call.arguments, ensure_ascii=False),
                            },
                        }
                        for tool_call in message.tool_calls
                    ]
                api_messages.append(api_message)
                continue

            if message.role == "tool":
                api_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        "content": message.content,
                    }
                )
                continue

        return api_messages

    def _parse_api_response(self, data: dict[str, Any]) -> LLMResponse:
        """把模型返回的第一个 choice 标准化。"""

        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"LLM 返回结构不符合预期: {data}") from exc

        content = self._normalize_message_content(message.get("content"))
        tool_calls: list[ToolCall] = []

        raw_tool_calls = message.get("tool_calls") or []

        for raw_tool_call in raw_tool_calls:
            function_block = raw_tool_call.get("function", {})
            raw_arguments = function_block.get("arguments", "{}")

            try:
                parsed_arguments = json.loads(raw_arguments) if raw_arguments else {}
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"模型返回了非法 JSON 工具参数: {raw_arguments}"
                ) from exc

            tool_calls.append(
                ToolCall(
                    id=raw_tool_call["id"],
                    name=function_block["name"],
                    arguments=parsed_arguments,
                )
            )

        return LLMResponse(
            message=ConversationMessage(
                role="assistant",
                content=content,
                tool_calls=tool_calls,
            )
        )

    def _parse_stream_response(self, raw_response: str) -> dict[str, Any]:
        """解析 OpenAI-compatible 的 SSE 流式内容。"""

        content_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        for line in raw_response.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if not stripped.startswith("data:"):
                continue

            payload = stripped[5:].strip()
            if payload == "[DONE]":
                break

            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                # 忽略格式不完整的片段，等待后续完整事件
                continue

            delta = event.get("choices", [{}])[0].get("delta", {})
            if not delta:
                continue

            delta_content = delta.get("content")
            if isinstance(delta_content, str):
                content_parts.append(delta_content)

            delta_tool_calls = delta.get("tool_calls")
            if isinstance(delta_tool_calls, list):
                for tool_chunk in delta_tool_calls:
                    self._merge_stream_tool_calls(tool_calls, tool_chunk)

        return {
            "choices": [
                {
                    "message": {
                        "content": "".join(content_parts),
                        "tool_calls": self._finalize_stream_tool_calls(tool_calls),
                    }
                }
            ]
        }

    @staticmethod
    def _merge_stream_tool_calls(
        tool_calls: list[dict[str, Any]], tool_chunk: dict[str, Any]
    ) -> None:
        """把流式 tool_calls 的增量片段合并起来。"""

        index = tool_chunk.get("index")
        if index is None:
            return

        while len(tool_calls) <= index:
            tool_calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})

        target = tool_calls[index]
        if tool_chunk.get("id"):
            target["id"] = tool_chunk["id"]

        function_chunk = tool_chunk.get("function") or {}
        if function_chunk.get("name"):
            target["function"]["name"] = function_chunk["name"]

        if "arguments" in function_chunk and function_chunk["arguments"]:
            target["function"]["arguments"] += function_chunk["arguments"]

    @staticmethod
    def _finalize_stream_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """清理流式工具调用，保证参数字段存在。"""

        finalized: list[dict[str, Any]] = []
        for tool_call in tool_calls:
            function_block = tool_call.get("function") or {}
            arguments = function_block.get("arguments") or ""
            if arguments.strip() == "":
                arguments = "{}"
            finalized.append(
                {
                    "id": tool_call.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": function_block.get("name", ""),
                        "arguments": arguments,
                    },
                }
            )
        return finalized

    @staticmethod
    def _normalize_message_content(content: Any) -> str:
        """把厂商特定的内容结构整理成纯文本。

        大多数 `chat/completions` 接口在这里都会直接返回字符串。
        也有一些兼容接口会返回更复杂的内容列表。
        第一版先在这里做一次兼容性兜底。
        """

        if content is None:
            return ""

        if isinstance(content, str):
            return content

        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(str(item.get("text", "")))
                else:
                    text_parts.append(str(item))
            return "\n".join(part for part in text_parts if part)

        return str(content)
