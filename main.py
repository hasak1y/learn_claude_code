#!/usr/bin/env python3
"""最小 Agent 运行时的命令行入口。

这个文件会把以下几部分串起来：
- 运行时循环
- OpenAI-compatible LLM 适配器
- 工具注册表
- 一个简单的交互式 REPL

结构会故意保持直接，因为这个项目既要方便理解，
也要作为后续继续加功能的基础。
"""

from __future__ import annotations

import os

from agent_runtime.agent import AgentLoop
from agent_runtime.config import load_openai_compatible_config
from agent_runtime.llm import OpenAICompatibleLLMClient
from agent_runtime.tools import BashTool, ToolRegistry
from agent_runtime.types import ConversationMessage


def build_system_prompt() -> str:
    """构造初始系统提示词。

    把提示词放进单独函数里，后续就更容易演进。
    例如之后可以在这里继续注入策略、项目元数据、
    或运行时额外能力说明。
    """

    return (
        f"你是一个在 {os.getcwd()} 中运行的编程 Agent。"
        "需要时使用工具，直接行动，回答保持简洁，任务完成后立即停止。"
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
    tool_registry = ToolRegistry([BashTool(cwd=os.getcwd())])
    agent = AgentLoop(
        llm_client=llm_client,
        tool_registry=tool_registry,
        system_prompt=build_system_prompt(),
        max_steps=8,
        echo_tool_calls=True,
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

        history.append(ConversationMessage(role="user", content=user_input))

        final_message = agent.run(history)
        if final_message.content:
            print(final_message.content)
        print()


if __name__ == "__main__":
    main()
