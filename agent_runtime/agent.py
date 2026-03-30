"""Agent 的核心循环。

这个文件包含了整个项目赖以生长的最小运行时模式：

1. 把对话历史和工具定义发给模型
2. 如果模型请求工具，就执行工具
3. 把工具结果追加回历史消息
4. 再次调用模型
5. 当模型返回不含工具调用的 assistant 消息时停止

真正的生产级 Agent，只是在这个循环外层继续叠加
策略、权限、生命周期和更多能力。
"""

from __future__ import annotations

from dataclasses import dataclass

from .llm.base import BaseLLMClient
from .tools.base import ToolRegistry
from .types import ConversationMessage


@dataclass(slots=True)
class AgentLoop:
    """一个最小但可复用的 Agent 运行时。

    循环层只负责控制流程，不负责具体厂商 API 细节，
    也不直接处理工具实现细节。这两部分会分别委托给：

    - LLM 客户端：负责“去问模型下一步该做什么”
    - 工具注册表：负责“执行模型请求的工具并返回结果”

    这种拆分是项目可扩展的关键，否则很容易退化成一个
    难以维护的大脚本。
    """

    llm_client: BaseLLMClient
    tool_registry: ToolRegistry
    system_prompt: str
    max_steps: int = 8
    echo_tool_calls: bool = True

    def run(self, messages: list[ConversationMessage]) -> ConversationMessage:
        """执行一次完整的 assistant 回合。

        `messages` 是 CLI 和运行时共享的一份可变对话历史。
        这个方法会直接把新的 assistant 消息和 tool 消息追加进去，
        这样下一次用户输入还能沿用同一份上下文。

        循环会在以下条件下停止：
        - 模型本轮不再请求工具
        - 达到配置的最大步数上限
        """

        for step_index in range(1, self.max_steps + 1):
            response = self.llm_client.generate(
                messages=messages,
                tools=self.tool_registry.get_tool_schemas(),
                system_prompt=self.system_prompt,
            )
            messages.append(response.message)

            # 如果这次没有工具调用，说明 assistant 这一回合已经完成。
            if not response.message.tool_calls:
                return response.message

            # 如果模型请求了工具，就逐个执行，并把每个结果作为独立的
            # `tool` 消息追加回历史中，供下一轮模型调用继续消费。
            for tool_call in response.message.tool_calls:
                if self.echo_tool_calls:
                    print(f"\033[33m[{step_index}] $ {tool_call.name} {tool_call.arguments}\033[0m")

                tool_output = self.tool_registry.execute(
                    name=tool_call.name,
                    arguments=tool_call.arguments,
                )

                if self.echo_tool_calls:
                    preview = tool_output[:200]
                    print(preview)
                    if len(tool_output) > 200:
                        print("... [预览已截断]")

                messages.append(
                    ConversationMessage(
                        role="tool",
                        content=tool_output,
                        tool_call_id=tool_call.id,
                        name=tool_call.name,
                    )
                )

        # 即使是最小版 MVP，也必须有一个硬停止条件。
        # 否则模型持续请求工具时，整个循环就可能永远跑不完。
        limit_message = ConversationMessage(
            role="assistant",
            content=(
                f"运行时在达到步数上限（{self.max_steps}）后停止。"
                "如果你提高上限或调整提示词，可以继续执行。"
            ),
        )
        messages.append(limit_message)
        return limit_message
