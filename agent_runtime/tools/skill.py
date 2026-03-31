"""按需加载 skill 正文的工具。"""

from __future__ import annotations

from typing import Any

from ..skills import SkillRegistry
from .base import BaseTool


class LoadSkillTool(BaseTool):
    """把某个 skill 的完整说明按需加载进当前上下文。"""

    name = "load_skill"
    description = (
        "按名称加载一个 skill 的完整说明。"
        "适合在你已经知道需要某种工作方法时，再拉取该 skill 的全文。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "要加载的 skill 名称。",
            }
        },
        "required": ["name"],
    }

    def __init__(self, skill_registry: SkillRegistry) -> None:
        self.skill_registry = skill_registry

    def execute(self, arguments: dict[str, Any]) -> str:
        """读取并返回指定 skill 的正文。"""

        name = str(arguments.get("name", "")).strip()
        if not name:
            return "错误：缺少 'name' 参数"

        return self.skill_registry.load_skill_text(name)
