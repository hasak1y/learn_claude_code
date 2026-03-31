"""对话压缩逻辑。

这一层实现三种压缩：

1. micro_compact
   每次请求模型前，对将要发送的消息视图做轻量压缩。
   只压缩较旧的 tool 消息，不破坏原始历史。

2. auto_compact
   当会话估算 token 超过阈值时，先把完整历史落盘，再让模型生成结构化摘要，
   最后用“摘要 + 最近若干条真实消息”替换活跃历史。

3. manual compact
   通过 `compact` 工具显式触发，与 auto_compact 复用同一套底层逻辑。

核心原则：
- 原始历史只在真正 compact 时才会被替换
- 平时的 micro_compact 只作用于请求视图
- transcript 会落盘，避免完整历史真正丢失
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .llm.base import BaseLLMClient
from .session_log import SessionLogger
from .todo import TodoManager
from .types import ConversationMessage


@dataclass(slots=True)
class ConversationCompactor:
    """会话压缩器。"""

    transcript_dir: Path
    keep_recent_tool_messages: int = 4
    micro_compact_char_threshold: int = 120
    auto_compact_token_threshold: int = 50000
    preserve_recent_messages: int = 6
    summary_input_char_limit: int = 80000

    def build_request_messages(self, messages: list[ConversationMessage]) -> list[ConversationMessage]:
        """构造发给模型的轻量压缩视图。

        这一层不会修改原始历史，只会在请求前生成一个替代视图。
        较旧且较长的 tool 结果会被替换成短摘要。
        """

        tool_indices = [index for index, msg in enumerate(messages) if msg.role == "tool"]
        if len(tool_indices) <= self.keep_recent_tool_messages:
            return list(messages)

        keep_indices = set(tool_indices[-self.keep_recent_tool_messages :])
        compacted: list[ConversationMessage] = []

        for index, message in enumerate(messages):
            if (
                message.role == "tool"
                and index not in keep_indices
                and len(message.content) > self.micro_compact_char_threshold
            ):
                compacted.append(
                    replace(
                        message,
                        content=self._build_tool_placeholder(message),
                    )
                )
            else:
                compacted.append(message)

        return compacted

    def should_auto_compact(self, messages: list[ConversationMessage], system_prompt: str) -> bool:
        """判断当前是否需要触发自动压缩。"""

        return self.estimate_tokens(messages, system_prompt) > self.auto_compact_token_threshold

    def estimate_tokens(self, messages: list[ConversationMessage], system_prompt: str) -> int:
        """粗略估算当前上下文 token。

        第一版不引入专门 tokenizer，先用字符长度做近似估算。
        对工具调用参数也做一点额外计数，避免明显低估。
        """

        total_chars = len(system_prompt)

        for message in messages:
            total_chars += len(message.role) + len(message.content)
            if message.name:
                total_chars += len(message.name)
            if message.tool_call_id:
                total_chars += len(message.tool_call_id)
            for tool_call in message.tool_calls:
                total_chars += len(tool_call.name)
                total_chars += len(json.dumps(tool_call.arguments, ensure_ascii=False))

        return max(1, total_chars // 4)

    def compact_history(
        self,
        messages: list[ConversationMessage],
        llm_client: BaseLLMClient,
        system_prompt: str,
        todo_manager: TodoManager | None,
        reason: str,
        session_logger: SessionLogger | None = None,
        log_scope: str = "parent",
        task_graph_summary: str | None = None,
    ) -> str:
        """执行一次真正的历史压缩，并原地替换活跃消息列表。"""

        if not messages:
            return "当前没有可压缩的历史。"

        original_messages = list(messages)
        transcript_path = self._save_transcript(original_messages, reason=reason)
        summary_text = self._build_structured_summary(
            messages=original_messages,
            llm_client=llm_client,
            todo_manager=todo_manager,
            transcript_path=transcript_path,
            task_graph_summary=task_graph_summary,
        )

        recent_messages = original_messages[-self.preserve_recent_messages :]
        summary_message = ConversationMessage(
            role="assistant",
            content=(
                "[上下文压缩摘要]\n"
                f"压缩原因：{reason}\n"
                f"完整转储：{transcript_path}\n\n"
                f"{summary_text}"
            ),
        )

        messages[:] = [summary_message, *recent_messages]
        if session_logger is not None:
            session_logger.append_message(summary_message, scope=log_scope)

        return (
            f"已完成上下文压缩。"
            f"原始消息数：{len(original_messages)}，"
            f"压缩后保留：{len(messages)}。"
            f"完整历史已保存到：{transcript_path}"
        )

    def _build_structured_summary(
        self,
        messages: list[ConversationMessage],
        llm_client: BaseLLMClient,
        todo_manager: TodoManager | None,
        transcript_path: Path,
        task_graph_summary: str | None = None,
    ) -> str:
        """让模型生成结构化续航摘要。"""

        serialized_history = self._serialize_messages(messages)
        truncated_history = serialized_history[: self.summary_input_char_limit]
        loaded_skills = self._collect_loaded_skills(messages)
        todo_text = todo_manager.render() if todo_manager is not None else "（无 todo 管理器）"

        prompt = (
            "请把下面这段 Agent 会话整理成一份“后续可继续工作的结构化摘要”。\n"
            "只输出中文摘要，不要输出额外解释。\n"
            "必须严格包含下面这些标题，并在每个标题下用简短条目整理信息：\n"
            "## 当前目标\n"
            "## 已完成事项\n"
            "## 未完成事项\n"
            "## 当前 Todo 状态\n"
            "## 当前任务图状态\n"
            "## 已加载 Skills\n"
            "## 关键文件与修改\n"
            "## 关键工具结果\n"
            "## 重要约束与风险\n"
            "## 最近一次用户要求\n\n"
            f"当前 todo 状态：\n{todo_text}\n\n"
            f"当前任务图状态：\n{task_graph_summary or '（当前没有任务图任务）'}\n\n"
            f"当前已加载 skills：{', '.join(loaded_skills) if loaded_skills else '（无）'}\n\n"
            f"完整 transcript 已保存到：{transcript_path}\n"
            "下面是待压缩的消息记录（可能已截断，请优先抓住关键信息）：\n\n"
            f"{truncated_history}"
        )

        response = llm_client.generate(
            messages=[ConversationMessage(role="user", content=prompt)],
            tools=[],
            system_prompt=(
                "你是一个专门负责上下文压缩的总结器。"
                "你的摘要必须为后续 Agent 提供连续性，不能空泛。"
            ),
        )
        return response.message.content or "（压缩摘要为空）"

    def _save_transcript(self, messages: list[ConversationMessage], reason: str) -> Path:
        """把压缩前的完整历史保存到磁盘。"""

        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = self.transcript_dir / f"transcript_{int(time.time() * 1000)}_{reason}.jsonl"

        with transcript_path.open("w", encoding="utf-8") as file:
            for message in messages:
                file.write(
                    json.dumps(self._message_to_dict(message), ensure_ascii=False) + "\n"
                )

        return transcript_path

    @staticmethod
    def _message_to_dict(message: ConversationMessage) -> dict[str, object]:
        """把内部消息结构转成可序列化字典。"""

        return {
            "role": message.role,
            "content": message.content,
            "tool_call_id": message.tool_call_id,
            "name": message.name,
            "tool_calls": [
                {
                    "id": tool_call.id,
                    "name": tool_call.name,
                    "arguments": tool_call.arguments,
                }
                for tool_call in message.tool_calls
            ],
        }

    @staticmethod
    def _serialize_messages(messages: list[ConversationMessage]) -> str:
        """把消息列表整理成更适合摘要器阅读的文本。"""

        chunks: list[str] = []
        for index, message in enumerate(messages, start=1):
            header = f"[{index}] role={message.role}"
            if message.name:
                header += f" name={message.name}"
            if message.tool_call_id:
                header += f" tool_call_id={message.tool_call_id}"

            chunks.append(header)
            if message.tool_calls:
                chunks.append(
                    "tool_calls="
                    + json.dumps(
                        [
                            {
                                "id": tool_call.id,
                                "name": tool_call.name,
                                "arguments": tool_call.arguments,
                            }
                            for tool_call in message.tool_calls
                        ],
                        ensure_ascii=False,
                    )
                )
            chunks.append(message.content)
            chunks.append("")

        return "\n".join(chunks)

    @staticmethod
    def _collect_loaded_skills(messages: list[ConversationMessage]) -> list[str]:
        """从历史里提取已经加载过的 skill 名称。"""

        pattern = re.compile(r"<skill name=\"([^\"]+)\">")
        loaded: list[str] = []

        for message in messages:
            if message.role != "tool" or message.name != "load_skill":
                continue

            match = pattern.search(message.content)
            if not match:
                continue

            skill_name = match.group(1)
            if skill_name not in loaded:
                loaded.append(skill_name)

        return loaded

    @staticmethod
    def _build_tool_placeholder(message: ConversationMessage) -> str:
        """构造旧 tool 结果的占位摘要。"""

        first_line = message.content.strip().splitlines()[0] if message.content.strip() else "（空结果）"
        first_line = first_line[:80]
        tool_name = message.name or "unknown_tool"
        status = "failed" if "错误" in message.content.lower() or "error" in message.content.lower() else "success"
        return f"[Earlier tool result: {tool_name}, {status}, {first_line}]"
