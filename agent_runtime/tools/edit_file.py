"""一个最小版的文件编辑工具。

第一版只支持最稳妥的一种编辑方式：
- 在目标文件里精确查找一段旧文本
- 把它替换成一段新文本

这样做的好处是行为边界清晰，也更容易定位失败原因。
"""

from __future__ import annotations

from pathlib import Path

from .base import BaseTool
from .path_utils import display_workspace_path, resolve_workspace_path


class EditFileTool(BaseTool):
    """对文件做一次精确文本替换。"""

    name = "edit_file"
    description = (
        "在一个已有文件中，把 old_text 精确替换为 new_text。"
        "仅适合明确知道要替换哪一段文本时使用。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要编辑的文件路径。相对路径会相对于当前项目目录解析。",
            },
            "old_text": {
                "type": "string",
                "description": "文件中要被替换的原始文本。必须精确匹配。",
            },
            "new_text": {
                "type": "string",
                "description": "替换后的新文本。",
            },
        },
        "required": ["path", "old_text", "new_text"],
    }

    def __init__(self, cwd: str) -> None:
        self.cwd = Path(cwd).resolve()

    def execute(self, arguments: dict[str, object]) -> str:
        """执行一次精确替换。"""

        raw_path = str(arguments.get("path", "")).strip()
        if not raw_path:
            return "错误：缺少 'path' 参数"

        if "old_text" not in arguments:
            return "错误：缺少 'old_text' 参数"

        if "new_text" not in arguments:
            return "错误：缺少 'new_text' 参数"

        old_text = str(arguments["old_text"])
        new_text = str(arguments["new_text"])

        if old_text == "":
            return "错误：'old_text' 不能为空"

        try:
            target_path = resolve_workspace_path(self.cwd, raw_path)
        except ValueError as exc:
            return f"错误：{exc}"

        if not target_path.exists():
            return f"错误：文件不存在：{raw_path}"

        if not target_path.is_file():
            return f"错误：目标不是文件：{raw_path}"

        try:
            content = target_path.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return f"错误：读取文件失败: {exc}"

        match_count = content.count(old_text)
        if match_count == 0:
            return "错误：未找到要替换的 old_text"

        if match_count > 1:
            return f"错误：old_text 在文件中出现了 {match_count} 次，当前工具只允许精确替换一次"

        updated_content = content.replace(old_text, new_text, 1)

        try:
            target_path.write_text(updated_content, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return f"错误：写入文件失败: {exc}"

        display_path = display_workspace_path(self.cwd, target_path)
        return (
            f"已编辑文件：{display_path}，"
            f"完成 1 处精确替换，"
            f"旧文本 {len(old_text)} 个字符，"
            f"新文本 {len(new_text)} 个字符。"
        )
