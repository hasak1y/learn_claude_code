"""一个最小版的 `bash` 风格 shell 工具。

第一版会故意保持简单：
- 只执行一条命令
- 捕获 stdout 和 stderr
- 对特别长的输出做截断

当前已知限制也是有意保留的：
- 还没有持久工作目录
- 还没有真正可靠的沙箱或权限模型
- 还没有高级流式输出

这些都值得后续再加，但不是证明核心循环成立的前置条件。
"""

from __future__ import annotations

import os
import subprocess

from .base import BaseTool


class BashTool(BaseTool):
    """在当前项目目录里执行一条 shell 命令。"""

    name = "bash"
    description = "在当前工作目录执行一条 shell 命令。"
    input_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令。",
            }
        },
        "required": ["command"],
    }

    def __init__(self, cwd: str | None = None, timeout_seconds: int = 120) -> None:
        self.cwd = cwd or os.getcwd()
        self.timeout_seconds = timeout_seconds

    def execute(self, arguments: dict[str, object]) -> str:
        """执行命令，并返回合并后的 stdout/stderr 文本。"""

        command = str(arguments.get("command", "")).strip()
        if not command:
            return "错误：缺少 'command' 参数"

        # 这个黑名单会故意保持很小，而且并不可靠。
        # 它只是 MVP 阶段的临时护栏，不是真正的安全模型。
        dangerous_fragments = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
        if any(fragment in command for fragment in dangerous_fragments):
            return "错误：危险命令已被拦截"

        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return f"错误：命令执行超时，已超过 {self.timeout_seconds} 秒"
        except Exception as exc:  # noqa: BLE001
            return f"错误：命令执行失败: {exc}"

        output = (completed.stdout + completed.stderr).strip()
        if not output:
            return "（无输出）"

        # 对超长输出做截断，避免第一版里对话上下文膨胀得太快。
        return output[:50000]
