"""Skill 注册与按需加载。

这一层负责把本地 `skills/` 目录中的技能整理成两层信息：

1. 简短索引
   放进 system prompt，告诉模型有哪些 skill 可用，以及各自用途。

2. 完整正文
   通过 `load_skill` 工具按需读取，再作为 tool result 回填给模型。

这样做的目标是：
- 平时不把所有技能正文都塞进上下文，节省 token
- 需要时模型又能主动拉取某个 skill 的完整方法说明
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SkillMeta:
    """单个 skill 的索引信息。"""

    name: str
    description: str
    path: Path


class SkillRegistry:
    """扫描本地 skills 目录，并提供索引与正文读取能力。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self._skills = self._scan_skills()

    def list_skills(self) -> list[SkillMeta]:
        """返回全部 skill 元信息。"""

        return list(self._skills.values())

    def has_skills(self) -> bool:
        """判断当前是否存在可用 skill。"""

        return bool(self._skills)

    def build_prompt_index(self) -> str:
        """生成适合注入 system prompt 的简短 skill 索引。"""

        if not self._skills:
            return "当前没有可用 skill。"

        lines = [
            "可用 skills：",
        ]
        for skill in self.list_skills():
            lines.append(f"- {skill.name}: {skill.description}")
        lines.append("当你需要某个 skill 的完整工作方法时，调用 load_skill(name)。")
        return "\n".join(lines)

    def load_skill_text(self, name: str) -> str:
        """按名称读取 skill 正文，并包装成清晰的结构。"""

        skill = self._skills.get(name)
        if skill is None:
            available = ", ".join(item.name for item in self.list_skills()) or "（无）"
            return f"错误：未找到 skill '{name}'。当前可用 skill：{available}"

        body = self._extract_skill_body(skill.path)
        return (
            f"<skill name=\"{skill.name}\">\n"
            f"<description>{skill.description}</description>\n"
            f"{body}\n"
            f"</skill>"
        )

    def _scan_skills(self) -> dict[str, SkillMeta]:
        """扫描 skills 目录下的 `SKILL.md` 文件。"""

        if not self.root.exists():
            return {}

        skills: dict[str, SkillMeta] = {}
        for skill_file in sorted(self.root.glob("*/SKILL.md")):
            meta = self._parse_skill_file(skill_file)
            skills[meta.name] = meta
        return skills

    @staticmethod
    def _parse_skill_file(path: Path) -> SkillMeta:
        """从 skill 文件里提取名称和简短说明。

        最小格式支持两种：
        1. YAML frontmatter
        2. 没有 frontmatter 时，退回到目录名和正文第一行
        """

        text = path.read_text(encoding="utf-8")
        name = path.parent.name
        description = "未提供描述"

        lines = text.splitlines()
        if len(lines) >= 3 and lines[0].strip() == "---":
            frontmatter_lines: list[str] = []
            for line in lines[1:]:
                if line.strip() == "---":
                    break
                frontmatter_lines.append(line)

            for line in frontmatter_lines:
                stripped = line.strip()
                if stripped.startswith("name:"):
                    name = stripped.split(":", 1)[1].strip() or name
                elif stripped.startswith("description:"):
                    description = stripped.split(":", 1)[1].strip() or description

        if description == "未提供描述":
            for line in lines:
                stripped = line.strip()
                if stripped and stripped != "---" and not stripped.startswith("name:") and not stripped.startswith("description:"):
                    description = stripped.lstrip("#").strip()
                    break

        return SkillMeta(name=name, description=description, path=path)

    @staticmethod
    def _extract_skill_body(path: Path) -> str:
        """提取 skill 正文，自动去掉 frontmatter。"""

        text = path.read_text(encoding="utf-8").strip()
        lines = text.splitlines()

        if len(lines) >= 3 and lines[0].strip() == "---":
            for index, line in enumerate(lines[1:], start=1):
                if line.strip() == "---":
                    return "\n".join(lines[index + 1 :]).strip()

        return text
