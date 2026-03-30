"""OpenAI-compatible LLM 适配器。

这个适配器面向 `chat/completions` 风格的接口，
并使用 OpenAI-compatible 的工具调用格式。
很多模型服务都会暴露兼容接口，所以它很适合作为项目第一版的适配器。
"""

from __future__ import annotations

import json
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from ..config import OpenAICompatibleConfig
from ..types import ConversationMessage, LLMResponse, ToolCall
from .base import BaseLLMClient


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
            "tools": tools,
            "tool_choice": "auto",
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }

        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }

        http_request = request.Request(url=url, data=body, headers=headers, method="POST")

        try:
            with request.urlopen(http_request, timeout=self.config.timeout_seconds) as response:
                raw_response = response.read().decode("utf-8")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM 请求失败，HTTP {exc.code}: {error_body}") from exc
        except URLError as exc:
            raise RuntimeError(f"LLM 请求失败: {exc}") from exc

        data = json.loads(raw_response)
        return self._parse_api_response(data)

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

        for raw_tool_call in message.get("tool_calls", []):
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
