"""一个最小版的文件写入工具。

这个工具只做一件事：
- 按给定路径写入完整文件内容

第一版故意不做复杂编辑语义，比如：
- 局部替换
- diff / patch
- 多段增量修改

如果模型想修改文件，最稳妥的方式是直接提供完整内容，
交给这个工具一次性落盘。
"""

from __future__ import annotations

from pathlib import Path

from .base import BaseTool
from .path_utils import display_workspace_path, resolve_workspace_path


class WriteFileTool(BaseTool):
    """把完整内容写入指定文件。"""

    name = "write_file"
    description = (
        "把完整文本内容写入一个文件。"
        "适合创建新文件，或用完整内容覆盖已有文件。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要写入的文件路径。相对路径会相对于当前项目目录解析。",
            },
            "content": {
                "type": "string",
                "description": "要写入文件的完整文本内容。",
            },
        },
        "required": ["path", "content"],
    }

    def __init__(self, cwd: str) -> None:
        self.cwd = Path(cwd).resolve()

    def execute(self, arguments: dict[str, object]) -> str:
        """执行文件写入，并返回简短结果。"""

        raw_path = str(arguments.get("path", "")).strip()
        if not raw_path:
            return "错误：缺少 'path' 参数"

        if "content" not in arguments:
            return "错误：缺少 'content' 参数"

        content = str(arguments["content"])
        try:
            target_path = resolve_workspace_path(self.cwd, raw_path)
        except ValueError as exc:
            return f"错误：{exc}"

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return f"错误：写入文件失败: {exc}"

        display_path = display_workspace_path(self.cwd, target_path)

        return (
            f"已写入文件：{display_path}，"
            f"共 {len(content)} 个字符，"
            f"{len(content.encode('utf-8'))} 个字节。"
        )
