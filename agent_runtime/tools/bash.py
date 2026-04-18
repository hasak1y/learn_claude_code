"""一个最小版的通用 shell 工具。

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

import locale
import os
import subprocess

from .base import BaseTool


class ShellTool(BaseTool):
    """在当前项目目录里执行一条 shell 命令。"""

    name = "shell"
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

    @staticmethod
    def _decode_output(data: bytes) -> str:
        """稳妥解码子进程输出。

        Windows 下很多命令默认按本地代码页输出，但也有工具会直接输出 UTF-8。
        这里先尝试 UTF-8，再回退到系统首选编码，最后用 replace 保底，避免
        `text=True` 在 reader thread 里提前抛出 UnicodeDecodeError。
        """

        if not data:
            return ""

        candidates = ["utf-8", locale.getpreferredencoding(False), "gbk"]
        for encoding in candidates:
            if not encoding:
                continue
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

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
                text=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return f"错误：命令执行超时，已超过 {self.timeout_seconds} 秒"
        except Exception as exc:  # noqa: BLE001
            return f"错误：命令执行失败: {exc}"

        stdout_text = self._decode_output(completed.stdout)
        stderr_text = self._decode_output(completed.stderr)
        output = (stdout_text + stderr_text).strip()
        if not output:
            return "（无输出）"

        # 对超长输出做截断，避免第一版里对话上下文膨胀得太快。
        return output[:50000]
