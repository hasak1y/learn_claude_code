"""一个最小版的文件读取工具。

这个工具专门用来读取工作区内的文本文件。
第一版保持简单，只支持：
- 指定路径
- 可选限制返回的行数

读取结果会做长度截断，避免上下文膨胀太快。
"""

from __future__ import annotations

from pathlib import Path

from .base import BaseTool
from .path_utils import display_workspace_path, resolve_workspace_path


class ReadFileTool(BaseTool):
    """读取工作区中的文本文件。"""

    name = "read_file"
    description = (
        "读取一个文本文件的内容。"
        "适合在修改前查看文件，或检查当前文件状态。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要读取的文件路径。相对路径会相对于当前项目目录解析。",
            },
            "limit_lines": {
                "type": "integer",
                "description": "可选。最多返回多少行内容。",
            },
        },
        "required": ["path"],
    }

    def __init__(self, cwd: str) -> None:
        self.cwd = Path(cwd).resolve()

    def execute(self, arguments: dict[str, object]) -> str:
        """读取文件并返回文本内容。"""

        raw_path = str(arguments.get("path", "")).strip()
        if not raw_path:
            return "错误：缺少 'path' 参数"

        limit_lines = arguments.get("limit_lines")
        if limit_lines is not None:
            try:
                limit_lines = int(limit_lines)
            except (TypeError, ValueError):
                return "错误：'limit_lines' 必须是整数"

            if limit_lines <= 0:
                return "错误：'limit_lines' 必须大于 0"

        try:
            target_path = resolve_workspace_path(self.cwd, raw_path)
        except ValueError as exc:
            return f"错误：{exc}"

        if not target_path.exists():
            return f"错误：文件不存在：{raw_path}"

        if not target_path.is_file():
            return f"错误：目标不是文件：{raw_path}"

        try:
            text = target_path.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return f"错误：读取文件失败: {exc}"

        lines = text.splitlines()
        was_truncated_by_lines = False

        if limit_lines is not None and limit_lines < len(lines):
            lines = lines[:limit_lines]
            was_truncated_by_lines = True

        result = "\n".join(lines)
        if text.endswith("\n") and result and not result.endswith("\n"):
            result += "\n"

        was_truncated_by_chars = len(result) > 50000
        result = result[:50000]

        display_path = display_workspace_path(self.cwd, target_path)
        prefix = f"# 文件：{display_path}\n"

        if was_truncated_by_lines:
            prefix += f"# 结果已按行数截断，仅返回前 {limit_lines} 行\n"

        if was_truncated_by_chars:
            prefix += "# 结果已按字符数截断，仅返回前 50000 个字符\n"

        return prefix + result
