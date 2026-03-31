#!/usr/bin/env python3
"""最小 Agent 运行时的命令行入口。"""

from __future__ import annotations

import os
import time
from pathlib import Path

from agent_runtime.agent import AgentLoop
from agent_runtime.compaction import ConversationCompactor
from agent_runtime.config import load_openai_compatible_config
from agent_runtime.llm import OpenAICompatibleLLMClient
from agent_runtime.session_log import SessionLogger
from agent_runtime.skills import SkillRegistry
from agent_runtime.subagents import SubagentRunner
from agent_runtime.todo import TodoManager
from agent_runtime.tools.base import ToolRegistry
from agent_runtime.tools.bash import ShellTool
from agent_runtime.tools.compact import CompactTool
from agent_runtime.tools.edit_file import EditFileTool
from agent_runtime.tools.read_file import ReadFileTool
from agent_runtime.tools.skill import LoadSkillTool
from agent_runtime.tools.subagent import TaskTool
from agent_runtime.tools.todo import TodoTool
from agent_runtime.tools.write_file import WriteFileTool
from agent_runtime.types import ConversationMessage


def build_system_prompt(skill_registry: SkillRegistry) -> str:
    """构造父 Agent 的系统提示词。"""

    return (
        f"你是一个在 {os.getcwd()} 中运行的编程 Agent。"
        "需要时使用工具，直接行动，回答保持简洁，任务完成后立刻停止。"
        "当前提供的是通用 shell 工具，不要假设一定是 Unix bash 环境。"
        "读取文件时优先使用 read_file。"
        "创建或整体覆盖文本文件时优先使用 write_file。"
        "修改已有文件时优先使用 edit_file。"
        "复杂或多步骤任务时应主动维护 todo 列表。"
        "todo 列表中同一时刻最多只能有一个 in_progress。"
        "当某个子任务相对独立、适合单独完成并返回总结时，可以使用 task。"
        "task 会同步调用一个 fresh context 的子代理，父 Agent 会等待子代理返回最终文本。"
        "当上下文很长、阶段切换或工具结果累积较多时，可以使用 compact。"
        "如果用户明确要求执行 compact，那么在收集到完成该轮总结所需的最小信息后，应尽快调用 compact，"
        "不要继续进行无关或重复的搜索。"
        "只有在文件类工具不适合时，再退回使用 shell。"
        "\n\n"
        f"{skill_registry.build_prompt_index()}"
    )


def build_subagent_system_prompt(skill_registry: SkillRegistry) -> str:
    """构造子代理使用的系统提示词。"""

    return (
        f"你是一个在 {os.getcwd()} 中运行的子代理。"
        "你拥有 fresh context，只负责当前被委派的单个子任务。"
        "你没有 task 工具，不能继续创建新的子代理。"
        "需要时使用工具，任务完成后返回简洁的最终结论。"
        "读取文件时优先使用 read_file。"
        "创建或整体覆盖文本文件时优先使用 write_file。"
        "修改已有文件时优先使用 edit_file。"
        "必要时可以使用 compact 来收缩上下文。"
        "如果当前任务已经明确要求 compact，在拿到必要信息后应尽快执行，不要继续无关搜索。"
        "只有在文件类工具不适合时，再退回使用 shell。"
        "\n\n"
        f"{skill_registry.build_prompt_index()}"
    )


def build_child_tool_registry(skill_registry: SkillRegistry) -> ToolRegistry:
    """构造子代理使用的工具集合。"""

    return ToolRegistry(
        [
            CompactTool(),
            LoadSkillTool(skill_registry=skill_registry),
            ReadFileTool(cwd=os.getcwd()),
            WriteFileTool(cwd=os.getcwd()),
            EditFileTool(cwd=os.getcwd()),
            ShellTool(cwd=os.getcwd()),
        ]
    )


def build_parent_tool_registry(
    todo_manager: TodoManager,
    subagent_runner: SubagentRunner,
    skill_registry: SkillRegistry,
) -> ToolRegistry:
    """构造父 Agent 使用的工具集合。"""

    return ToolRegistry(
        [
            TodoTool(todo_manager=todo_manager),
            TaskTool(runner=subagent_runner),
            CompactTool(),
            LoadSkillTool(skill_registry=skill_registry),
            ReadFileTool(cwd=os.getcwd()),
            WriteFileTool(cwd=os.getcwd()),
            EditFileTool(cwd=os.getcwd()),
            ShellTool(cwd=os.getcwd()),
        ]
    )


def main() -> None:
    """启动一个交互式命令行会话。"""

    try:
        config = load_openai_compatible_config()
    except KeyError as exc:
        missing_key = exc.args[0]
        print(f"缺少必填环境变量：{missing_key}")
        print("必填变量：LLM_MODEL, LLM_API_KEY")
        print("可选变量：LLM_BASE_URL, LLM_TIMEOUT_SECONDS, LLM_MAX_TOKENS, LLM_TEMPERATURE")
        return

    llm_client = OpenAICompatibleLLMClient(config)
    skill_registry = SkillRegistry(root=os.path.join(os.getcwd(), "skills"))
    todo_manager = TodoManager(reminder_threshold=3)
    session_id = f"session_{int(time.time() * 1000)}"
    session_logger = SessionLogger(
        session_id=session_id,
        path=Path(os.getcwd()) / ".sessions" / f"{session_id}.jsonl",
    )
    compactor = ConversationCompactor(
        transcript_dir=Path(os.getcwd()) / ".transcripts",
        auto_compact_token_threshold=12000,
    )
    subagent_runner = SubagentRunner(
        llm_client_factory=lambda: OpenAICompatibleLLMClient(config),
        child_tool_registry_factory=lambda: build_child_tool_registry(skill_registry),
        child_system_prompt=build_subagent_system_prompt(skill_registry),
        child_max_steps=12,
        compactor=compactor,
        session_logger=session_logger,
    )
    tool_registry = build_parent_tool_registry(
        todo_manager=todo_manager,
        subagent_runner=subagent_runner,
        skill_registry=skill_registry,
    )
    agent = AgentLoop(
        llm_client=llm_client,
        tool_registry=tool_registry,
        system_prompt=build_system_prompt(skill_registry),
        max_steps=12,
        echo_tool_calls=True,
        todo_manager=todo_manager,
        compactor=compactor,
        session_logger=session_logger,
        log_scope="parent",
    )

    history: list[ConversationMessage] = []

    while True:
        try:
            user_input = input("\033[36magent >> \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if user_input.lower() in {"", "q", "quit", "exit"}:
            break

        user_message = ConversationMessage(role="user", content=user_input)
        history.append(user_message)
        session_logger.append_message(user_message, scope="parent")

        try:
            run_result = agent.run(history)
        except RuntimeError as exc:
            print(f"运行失败：{exc}")
            print("请检查 .env 中的 LLM_BASE_URL、LLM_MODEL 和 LLM_API_KEY 配置。")
            print()
            continue

        if run_result.final_text:
            print(run_result.final_text)
        print()


if __name__ == "__main__":
    main()
