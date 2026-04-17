#!/usr/bin/env python3
"""最小 Agent 运行时的命令行入口。"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from agent_runtime.agent import AgentLoop
from agent_runtime.background_jobs import BackgroundJobManager
from agent_runtime.compaction import ConversationCompactor
from agent_runtime.config import load_openai_compatible_config
from agent_runtime.dialogue_history import RecentDialogueStore
from agent_runtime.hooks import HookManager
from agent_runtime.llm import OpenAICompatibleLLMClient
from agent_runtime.memory import AutoMemoryManager, LearnClaudeContextLoader
from agent_runtime.runtime_hooks import (
    BackgroundJobHook,
    MemoryRetrievalHook,
    PathScopedRuleHook,
    PermissionHook,
)
from agent_runtime.session_log import SessionLogger, load_latest_parent_history
from agent_runtime.skills import SkillRegistry
from agent_runtime.subagents import SubagentRunner
from agent_runtime.task_graph import TaskGraphManager
from agent_runtime.team import TeamAgentRunner, TeamManager
from agent_runtime.todo import TodoManager
from agent_runtime.permissions import PermissionPolicy
from agent_runtime.tools.background_job import (
    BackgroundShellTool,
    GetBackgroundJobResultTool,
    ListBackgroundJobsTool,
)
from agent_runtime.tools.base import ToolRegistry
from agent_runtime.tools.bash import ShellTool
from agent_runtime.tools.compact import CompactTool
from agent_runtime.tools.edit_file import EditFileTool
from agent_runtime.tools.read_file import ReadFileTool
from agent_runtime.tools.skill import LoadSkillTool
from agent_runtime.tools.subagent import TaskTool
from agent_runtime.tools.task_graph import (
    CreateTaskTool,
    GetTaskTool,
    ListAllTasksTool,
    ListBlockedTasksTool,
    ListCompletedTasksTool,
    ListReadyTasksTool,
    UpdateTaskTool,
)
from agent_runtime.tools.team import (
    GetTeamAgentTool,
    ListTeamAgentsTool,
    PeekTeamInboxTool,
    RunTeamAgentTool,
    SendTeamMessageTool,
    ShutdownTeamAgentTool,
    SpawnTeamAgentTool,
)
from agent_runtime.tools.todo import TodoTool
from agent_runtime.tools.write_file import WriteFileTool
from agent_runtime.types import ConversationMessage


PROMPT_TEXT = "\033[36magent >> \033[0m"
APPROVAL_TIMEOUT_SECONDS = int(os.getenv("APPROVAL_TIMEOUT_SECONDS", "30"))
SESSION_RESUME_MAX_MESSAGES = int(os.getenv("SESSION_RESUME_MAX_MESSAGES", "40"))
RECENT_DIALOGUE_MAX_MESSAGES = int(os.getenv("RECENT_DIALOGUE_MAX_MESSAGES", "100"))
MEMORY_DIALOGUE_LOOKBACK = int(os.getenv("MEMORY_DIALOGUE_LOOKBACK", "24"))


def _input_with_timeout(prompt: str, timeout_seconds: int) -> str | None:
    """带超时的输入，超时返回 None。"""

    if timeout_seconds <= 0:
        return input(prompt)

    import queue
    import threading

    result_queue: queue.Queue[str] = queue.Queue(maxsize=1)

    def _reader() -> None:
        try:
            result_queue.put(input(prompt))
        except Exception:
            result_queue.put("")

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()

    try:
        return result_queue.get(timeout=timeout_seconds)
    except queue.Empty:
        return None


def _prompt_user_approval(tool_call: object, decision: object) -> bool:
    """在终端提示用户确认敏感操作。"""

    try:
        name = getattr(tool_call, "name", "unknown")
        arguments = getattr(tool_call, "arguments", {})
        reason = getattr(decision, "reason", "需要确认")
    except Exception:
        name = "unknown"
        arguments = {}
        reason = "需要确认"

    print("\n[权限确认] 该操作需要用户确认：")
    print(f"- 工具：{name}")
    print(f"- 原因：{reason}")
    print(f"- 参数：{arguments}")
    answer = _input_with_timeout(
        f"是否允许执行？(y/n) [超时 {APPROVAL_TIMEOUT_SECONDS}s 默认拒绝]: ",
        APPROVAL_TIMEOUT_SECONDS,
    )
    if answer is None:
        print("权限确认超时，默认拒绝。")
        return False
    return answer.strip().lower() in {"y", "yes"}


def build_system_prompt(
    skill_registry: SkillRegistry,
) -> str:
    """构造父 Agent 的系统提示词。"""

    base_prompt = (
        f"你是一个在 {os.getcwd()} 中运行的编程 Agent。\n"
        "需要时使用工具，直接行动，回答保持简洁，任务完成后立刻停止。\n"
        "当前提供的是通用 shell 工具，不要假设一定是 Unix bash 环境。\n"
        "读取文件时优先使用 read_file。\n"
        "创建或整体覆盖文本文件时优先使用 write_file。\n"
        "修改已有文件时优先使用 edit_file。\n"
        "简单、线性的短任务可使用 todo。\n"
        "存在依赖、解锁关系或可并行推进的复杂任务，应优先使用 task graph 工具。\n"
        "todo 中同一时刻最多只能有一个 in_progress。\n"
        "如果某个 shell 命令预计会运行很久，例如 npm install、pytest 或 docker build，应优先使用 shell_background 把它放到后台，再继续做别的工作。\n"
        "后台任务完成后，结果会在下一次调用模型前自动注入。\n"
        "当某个子任务相对独立、适合单独完成并返回总结时，可以使用 task。\n"
        "task 会同步调用一个 fresh context 的子代理，父 Agent 会等待子代理返回最终文本。\n"
        "当你需要创建可跨多轮存活、拥有固定身份和独立历史的 teammate 时，使用 team_spawn_agent。\n"
        "当你需要把任务、备注或结果发给 teammate 时，使用 team_send_message。\n"
        "当前开启了权限审查：敏感操作需要用户确认。\n"
        "长期规则层和长期经验层会通过后续上下文消息注入，而不是直接写死在 system prompt 中。\n"
        "这些都属于外置上下文，不代表模型权重已经学会了它们。\n"
        "当上下文很长、阶段切换或工具结果累积较多时，可以使用 compact。\n"
        "如果用户明确要求执行 compact，那么在收集到完成该轮总结所需的最少信息后，应尽快调用 compact。\n"
        "只有在文件类工具不适合时，再退回使用 shell。\n\n"
        "权限审查规则：\n"
        "- shell 非白名单命令需要用户确认。\n"
        "- read_only 模式中 write_file / edit_file 会被拒绝。\n"
        "- dev_safe 模式中 shell_background 仅白名单直接放行，其余需要确认。\n"
    )

    return f"{base_prompt}\n{skill_registry.build_prompt_index()}"


def build_subagent_system_prompt(
    skill_registry: SkillRegistry,
) -> str:
    """构造一次性子代理使用的系统提示词。"""

    base_prompt = (
        f"你是一个在 {os.getcwd()} 中运行的子代理。\n"
        "你拥有 fresh context，只负责当前被委派的单个子任务。\n"
        "你没有 task 工具，也没有 task graph 工具，不能继续创建新的子代理或改动父任务图。\n"
        "需要时使用工具，任务完成后返回简洁的最终结论。\n"
        "读取文件时优先使用 read_file。\n"
        "创建或整体覆盖文本文件时优先使用 write_file。\n"
        "修改已有文件时优先使用 edit_file。\n"
        "必要时可以使用 compact 来收缩上下文。\n"
        "只有在文件类工具不适合时，再退回使用 shell。\n"
    )

    return f"{base_prompt}\n{skill_registry.build_prompt_index()}"


def build_team_agent_base_prompt(
    skill_registry: SkillRegistry,
) -> str:
    """构造持久 teammate 的通用基础提示词。"""

    base_prompt = (
        f"你是一个在 {os.getcwd()} 中运行的持久 teammate。\n"
        "你会跨多轮处理自己的 inbox 任务，并保留自己的历史。\n"
        "需要时使用工具，回答保持简洁。\n"
        "如果需要把结论、阻塞或协作请求发给其他 teammate，请使用 team_send_message。\n"
        "读取文件时优先使用 read_file。\n"
        "创建或整体覆盖文本文件时优先使用 write_file。\n"
        "修改已有文件时优先使用 edit_file。\n"
        "必要时可以使用 compact 来收缩上下文。\n"
        "只有在文件类工具不适合时，再退回使用 shell。\n"
    )

    return f"{base_prompt}\n{skill_registry.build_prompt_index()}"


def build_startup_context_messages(
    *,
    learnclaude_text: str,
    memory_index_slice: str,
) -> list[ConversationMessage]:
    """构造“system prompt 之后”的启动上下文消息。

    这里专门放长期规则层和长期经验层的启动切片，
    避免把它们和 core system prompt 混成一坨。
    """

    messages: list[ConversationMessage] = []
    if learnclaude_text:
        messages.append(
            ConversationMessage(
                role="user",
                content=(
                    "<startup_context type=\"learnclaude\">\n"
                    "以下是运行时加载的长期规则层，请把它视为外置上下文，而不是模型已经学会的知识。\n\n"
                    f"{learnclaude_text}\n"
                    "</startup_context>"
                ),
            )
        )

    if memory_index_slice:
        messages.append(
            ConversationMessage(
                role="user",
                content=(
                    "<startup_context type=\"memory_index\">\n"
                    "以下是长期经验索引的启动切片。它只说明有哪些长期经验主题存在，不代表这些主题正文已经全部加载。\n\n"
                    f"{memory_index_slice}\n"
                    "</startup_context>"
                ),
            )
        )

    return messages


def ensure_startup_context_messages(
    *,
    history: list[ConversationMessage],
    startup_messages: list[ConversationMessage],
    session_logger: SessionLogger,
    scope: str,
) -> None:
    """确保启动上下文只注入一次。"""

    existing = {(item.role, item.content) for item in history}
    for message in startup_messages:
        key = (message.role, message.content)
        if key in existing:
            continue

        copied = ConversationMessage(role=message.role, content=message.content)
        history.append(copied)
        session_logger.append_message(copied, scope=scope)
        existing.add(key)


def build_child_tool_registry(skill_registry: SkillRegistry) -> ToolRegistry:
    """构造一次性子代理使用的工具集合。"""

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


def build_team_agent_tool_registry(
    *,
    skill_registry: SkillRegistry,
    team_manager: TeamManager,
    sender_id: str,
) -> ToolRegistry:
    """构造持久 teammate 运行时使用的工具集合。"""

    return ToolRegistry(
        [
            CompactTool(),
            LoadSkillTool(skill_registry=skill_registry),
            SendTeamMessageTool(manager=team_manager, sender_id=sender_id),
            ListTeamAgentsTool(manager=team_manager),
            GetTeamAgentTool(manager=team_manager),
            ReadFileTool(cwd=os.getcwd()),
            WriteFileTool(cwd=os.getcwd()),
            EditFileTool(cwd=os.getcwd()),
            ShellTool(cwd=os.getcwd()),
        ]
    )


def build_parent_tool_registry(
    *,
    todo_manager: TodoManager,
    subagent_runner: SubagentRunner,
    skill_registry: SkillRegistry,
    task_graph_manager: TaskGraphManager,
    background_job_manager: BackgroundJobManager,
    team_manager: TeamManager,
    team_runner: TeamAgentRunner,
) -> ToolRegistry:
    """构造父 Agent 使用的工具集合。"""

    return ToolRegistry(
        [
            TodoTool(todo_manager=todo_manager),
            CreateTaskTool(manager=task_graph_manager),
            UpdateTaskTool(manager=task_graph_manager),
            GetTaskTool(manager=task_graph_manager),
            ListAllTasksTool(manager=task_graph_manager),
            ListReadyTasksTool(manager=task_graph_manager),
            ListBlockedTasksTool(manager=task_graph_manager),
            ListCompletedTasksTool(manager=task_graph_manager),
            BackgroundShellTool(manager=background_job_manager),
            ListBackgroundJobsTool(manager=background_job_manager),
            GetBackgroundJobResultTool(manager=background_job_manager),
            SpawnTeamAgentTool(manager=team_manager),
            ListTeamAgentsTool(manager=team_manager),
            GetTeamAgentTool(manager=team_manager),
            SendTeamMessageTool(manager=team_manager, sender_id="lead"),
            PeekTeamInboxTool(manager=team_manager),
            RunTeamAgentTool(runner=team_runner),
            ShutdownTeamAgentTool(manager=team_manager),
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
        print(
            "可选变量：LLM_BASE_URL, LLM_TIMEOUT_SECONDS, LLM_MAX_TOKENS, "
            "LLM_TEMPERATURE, LLM_CONTEXT_WINDOW"
        )
        return

    cwd = Path(os.getcwd())
    managed_rules_path_raw = os.getenv("LEARNCLAUDE_MANAGED_PATH", "").strip()
    managed_rules_path = Path(managed_rules_path_raw) if managed_rules_path_raw else None

    llm_client = OpenAICompatibleLLMClient(config)
    permission_mode = os.getenv("PERMISSION_MODE", "dev_safe")
    permission_policy = PermissionPolicy(mode=permission_mode)  # type: ignore[arg-type]
    skill_registry = SkillRegistry(root=str(cwd / "skills"))
    todo_manager = TodoManager(reminder_threshold=3)
    task_graph_manager = TaskGraphManager(tasks_dir=cwd / ".tasks")
    background_job_manager = BackgroundJobManager(cwd=os.getcwd())
    team_manager = TeamManager(root=cwd / ".team")
    sessions_dir = cwd / ".sessions"
    resume_result = load_latest_parent_history(
        sessions_dir,
        max_messages=SESSION_RESUME_MAX_MESSAGES,
        prefer_summary=True,
    )

    # 长期规则层：启动时统一读取 learnclaude 规则，直接拼进系统提示词。
    learnclaude_loader = LearnClaudeContextLoader(
        cwd=cwd,
        managed_path=managed_rules_path,
    )
    learnclaude_text = learnclaude_loader.render_for_system_prompt()

    # 长期经验层：MEMORY.md + topic files。
    memory_manager = AutoMemoryManager(root=cwd / ".memory")
    memory_index_slice = memory_manager.render_startup_index_slice()

    # 原始最近对话层：只保留 user / assistant，不参与会话恢复。
    recent_dialogue_store = RecentDialogueStore(
        path=cwd / ".chat_history" / "recent_dialogue.jsonl",
        max_messages=RECENT_DIALOGUE_MAX_MESSAGES,
    )

    session_id = f"session_{int(time.time() * 1000)}"
    session_logger = SessionLogger(
        session_id=session_id,
        path=sessions_dir / f"{session_id}.jsonl",
    )

    # 接近上下文窗口 85% 时自动 compact。
    auto_compact_threshold = max(1, int(config.context_window * 0.85))
    compactor = ConversationCompactor(
        transcript_dir=cwd / ".transcripts",
        auto_compact_token_threshold=auto_compact_threshold,
    )

    subagent_runner = SubagentRunner(
        llm_client_factory=lambda: OpenAICompatibleLLMClient(config),
        child_tool_registry_factory=lambda: build_child_tool_registry(skill_registry),
        child_system_prompt=build_subagent_system_prompt(skill_registry),
        child_startup_messages=build_startup_context_messages(
            learnclaude_text=learnclaude_text,
            memory_index_slice=memory_index_slice,
        ),
        child_max_steps=12,
        compactor=compactor,
        session_logger=session_logger,
    )

    team_runner = TeamAgentRunner(
        team_manager=team_manager,
        llm_client_factory=lambda: OpenAICompatibleLLMClient(config),
        tool_registry_factory=lambda agent_id: build_team_agent_tool_registry(
            skill_registry=skill_registry,
            team_manager=team_manager,
            sender_id=agent_id,
        ),
        base_system_prompt_builder=lambda _agent_id: build_team_agent_base_prompt(skill_registry),
        startup_messages=build_startup_context_messages(
            learnclaude_text=learnclaude_text,
            memory_index_slice=memory_index_slice,
        ),
        max_steps=12,
        compactor=compactor,
    )

    tool_registry = build_parent_tool_registry(
        todo_manager=todo_manager,
        subagent_runner=subagent_runner,
        skill_registry=skill_registry,
        task_graph_manager=task_graph_manager,
        background_job_manager=background_job_manager,
        team_manager=team_manager,
        team_runner=team_runner,
    )

    hook_manager = HookManager(
        {
            "before_llm_request": [
                BackgroundJobHook(background_job_manager),
                MemoryRetrievalHook(memory_manager, llm_client),
            ],
            "before_tool_execute": [
                PermissionHook(permission_policy, _prompt_user_approval)
            ],
            "after_tool_execute": [
                PathScopedRuleHook(learnclaude_loader, cwd)
            ],
        }
    )

    agent = AgentLoop(
        llm_client=llm_client,
        tool_registry=tool_registry,
        system_prompt=build_system_prompt(skill_registry),
        max_steps=12,
        echo_tool_calls=True,
        todo_manager=todo_manager,
        task_graph_manager=task_graph_manager,
        background_job_manager=background_job_manager,
        compactor=compactor,
        session_logger=session_logger,
        log_scope="parent",
        hook_manager=hook_manager,
    )

    history: list[ConversationMessage] = list(resume_result.history)
    startup_context_messages = build_startup_context_messages(
        learnclaude_text=learnclaude_text,
        memory_index_slice=memory_index_slice,
    )
    ensure_startup_context_messages(
        history=history,
        startup_messages=startup_context_messages,
        session_logger=session_logger,
        scope="parent:startup",
    )
    if resume_result.path is not None and history:
        print(
            "[session_resume] "
            f"已从 {resume_result.path.name} 恢复主历史，"
            f"模式={resume_result.mode}，"
            f"共 {len(history)} 条消息。"
        )
    if learnclaude_text:
        print("[learnclaude] 已加载长期规则层。")
    print(
        "[memory] "
        f"自动 compact 阈值={auto_compact_threshold}，"
        "长期经验将按需检索。"
    )

    while True:
        try:
            sys.stdout.write(PROMPT_TEXT)
            sys.stdout.flush()
            user_input = input().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue

        if user_input.lower() in {"q", "quit", "exit"}:
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

        # 最近原始对话单独落盘，供跨会话自动记忆提取使用。
        recent_dialogue_store.append_message(
            role="user",
            content=user_input,
            session_id=session_id,
        )

        if run_result.final_text:
            recent_dialogue_store.append_message(
                role="assistant",
                content=run_result.final_text,
                session_id=session_id,
            )
            print(run_result.final_text)

        auto_memory_message = memory_manager.maybe_update_from_dialogue(
            llm_client=llm_client,
            recent_dialogue=recent_dialogue_store.load_recent(MEMORY_DIALOGUE_LOOKBACK),
        )
        if auto_memory_message:
            print(f"[auto_memory] {auto_memory_message}")

        print()


if __name__ == "__main__":
    main()
