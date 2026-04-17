"""基于 learnclaude 规则和 auto memory 的记忆系统。

这里实现的是“合理推断”的本地版本。

分层对应关系：
1. 长期规则层：`learnclaude.md` / `learnclaude.local.md`
2. 长期经验层：`.memory/MEMORY.md` + `.memory/topics/*.md`
3. 按需加载层：根据当前用户请求，从索引里挑选少量相关 topic 文件再注入
4. 执行控制层：仍由 settings / hooks / permission 负责，不放在这里
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .dialogue_history import DialogueRecord
from .llm.base import BaseLLMClient
from .types import ConversationMessage


USER_LEARNCLAUDE_PATH = Path.home() / ".learnclaude" / "learnclaude.md"
PROJECT_RULE_FILENAME = "learnclaude.md"
PROJECT_LOCAL_RULE_FILENAME = "learnclaude.local.md"
MEMORY_INDEX_FILENAME = "MEMORY.md"


@dataclass(slots=True)
class LearnClaudeRuleFile:
    """一份被加载进系统提示词的规则文件。"""

    path: Path
    label: str
    content: str


class LearnClaudeContextLoader:
    """加载长期规则层。

    规则来源：
    - managed policy：通过环境变量显式指定
    - user：`~/.learnclaude/learnclaude.md`
    - project / local：从当前目录一路向上查找

    这里采用“拼接而不是覆盖”的策略。
    """

    def __init__(self, cwd: Path, managed_path: Path | None = None) -> None:
        self.cwd = cwd.resolve()
        self.managed_path = managed_path.resolve() if managed_path else None

    def collect(self) -> list[LearnClaudeRuleFile]:
        """收集所有应该注入的规则文件。"""

        collected: list[LearnClaudeRuleFile] = []

        if self.managed_path and self.managed_path.exists():
            collected.append(
                LearnClaudeRuleFile(
                    path=self.managed_path,
                    label="managed",
                    content=self.managed_path.read_text(encoding="utf-8").strip(),
                )
            )

        if USER_LEARNCLAUDE_PATH.exists():
            collected.append(
                LearnClaudeRuleFile(
                    path=USER_LEARNCLAUDE_PATH,
                    label="user",
                    content=USER_LEARNCLAUDE_PATH.read_text(encoding="utf-8").strip(),
                )
            )

        # 从根目录走到当前目录，保证上层规则先出现，下层规则后出现。
        project_dirs = list(reversed([self.cwd, *self.cwd.parents]))
        for directory in project_dirs:
            project_file = directory / PROJECT_RULE_FILENAME
            local_file = directory / PROJECT_LOCAL_RULE_FILENAME

            if project_file.exists():
                collected.append(
                    LearnClaudeRuleFile(
                        path=project_file,
                        label="project",
                        content=project_file.read_text(encoding="utf-8").strip(),
                    )
                )

            # 同目录下 local 规则放在 shared 规则后面，更接近用户局部覆盖的意图。
            if local_file.exists():
                collected.append(
                    LearnClaudeRuleFile(
                        path=local_file,
                        label="local",
                        content=local_file.read_text(encoding="utf-8").strip(),
                    )
                )

        return [item for item in collected if item.content]

    def render_for_system_prompt(self) -> str:
        """把规则层拼成一段稳定的系统提示词附加内容。"""

        files = self.collect()
        if not files:
            return ""

        sections = ["以下是运行时加载的 learnclaude 规则，请将其视为外置上下文而非权重学习结果："]
        for item in files:
            sections.append(f"\n[{item.label}] {item.path}")
            sections.append(item.content)
        return "\n".join(sections).strip()

    def render_path_scoped_for_target(self, target_path: Path) -> str:
        """按访问路径激活子目录规则。

        规则：
        - 只考虑当前工作目录之下、更深层的子目录规则。
        - 当前目录本身的规则已经在启动时注入，这里不重复。
        - 同目录下仍然保持 `learnclaude.md` 在前、`learnclaude.local.md` 在后。
        """

        resolved_target = target_path.resolve()
        target_dir = resolved_target if resolved_target.is_dir() else resolved_target.parent

        try:
            target_dir.relative_to(self.cwd)
        except ValueError:
            return ""

        directories: list[Path] = []
        current = target_dir
        while current != self.cwd and self.cwd in current.parents:
            directories.append(current)
            current = current.parent
        directories.reverse()

        sections: list[str] = []
        for directory in directories:
            project_file = directory / PROJECT_RULE_FILENAME
            local_file = directory / PROJECT_LOCAL_RULE_FILENAME

            if project_file.exists():
                sections.append(f"[path-scoped] {project_file}")
                sections.append(project_file.read_text(encoding="utf-8").strip())

            if local_file.exists():
                sections.append(f"[path-scoped-local] {local_file}")
                sections.append(local_file.read_text(encoding="utf-8").strip())

        if not sections:
            return ""

        return (
            "以下规则因为你刚刚访问了对应路径而被按需激活，请仅在相关文件范围内遵守：\n"
            + "\n".join(section for section in sections if section)
        ).strip()


@dataclass(slots=True)
class MemoryTopic:
    """一条长期经验索引项。"""

    name: str
    description: str
    type: str
    path: str


VALID_MEMORY_TYPES = {"user", "project", "feedback", "reference"}


class AutoMemoryManager:
    """管理 `.memory` 下的跨会话长期经验。

    设计思路：
    - `MEMORY.md` 只做目录和摘要索引。
    - 详细内容按 topic file 存放。
    - 检索时先读索引，再让模型从索引里挑少量相关 topic。
    - 写入时只基于现有索引摘要 + 最近对话做增量更新。
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.topics_dir = self.root / "topics"
        self.index_path = self.root / MEMORY_INDEX_FILENAME
        self.root.mkdir(parents=True, exist_ok=True)
        self.topics_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_index_exists()

    def _ensure_index_exists(self) -> None:
        """初始化索引文件。"""

        if self.index_path.exists():
            return

        self.index_path.write_text(
            "---\n"
            "version: 1\n"
            "kind: auto-memory-index\n"
            "---\n\n"
            "# MEMORY\n\n"
            "该文件是长期经验索引，不直接保存完整经验内容。\n"
            "每条记录使用单行结构，供运行时按需检索。\n",
            encoding="utf-8",
        )

    def load_index(self) -> list[MemoryTopic]:
        """从 `MEMORY.md` 里解析索引项。"""

        if not self.index_path.exists():
            return []

        topics: list[MemoryTopic] = []
        pattern = re.compile(
            r"^- path:\s*(?P<path>.+?)\s*\|\s*name:\s*(?P<name>.+?)\s*\|\s*type:\s*(?P<type>.+?)\s*\|\s*description:\s*(?P<description>.+?)\s*$"
        )

        for raw_line in self.index_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line.startswith("- path:"):
                continue

            match = pattern.match(line)
            if not match:
                continue

            topics.append(
                MemoryTopic(
                    path=match.group("path").strip(),
                    name=match.group("name").strip(),
                    type=match.group("type").strip(),
                    description=match.group("description").strip(),
                )
            )

        return topics

    def render_index_summary(self) -> str:
        """把索引渲染成短摘要，供检索和写入判断使用。"""

        topics = self.load_index()
        if not topics:
            return "当前还没有长期经验索引。"

        lines = ["当前长期经验索引如下："]
        for topic in topics:
            lines.append(
                f"- {topic.path} | {topic.name} | {topic.type} | {topic.description}"
            )
        return "\n".join(lines)

    def render_startup_index_slice(
        self,
        *,
        max_lines: int = 200,
        max_chars: int = 25000,
    ) -> str:
        """返回启动时注入的 MEMORY 索引切片。

        这里故意只给索引，不给所有 topic 正文：
        - 让模型知道“长期经验体系存在”
        - 但不在启动时把全部细节塞进上下文
        """

        if not self.index_path.exists():
            return ""

        lines = self.index_path.read_text(encoding="utf-8").splitlines()[:max_lines]
        text = "\n".join(lines).strip()
        if not text:
            return ""
        return text[:max_chars].strip()

    def retrieve_for_query(
        self,
        *,
        query: str,
        llm_client: BaseLLMClient,
        top_k: int = 5,
    ) -> list[MemoryTopic]:
        """根据当前请求，从长期经验索引里选出最相关的 topic。

        这是“合理推断”的检索实现：
        - 先只给模型索引摘要，而不是把全部 topic 原文塞进去。
        - 再让模型返回少量相关路径。
        - 如果解析失败，退回关键词重叠检索。
        """

        topics = self.load_index()
        if not topics or not query.strip():
            return []

        prompt = (
            "你是长期经验索引检索器。\n"
            "给定用户当前请求和可用 topic 索引，请只返回最相关的路径 JSON。\n"
            "输出格式必须是：{\"paths\": [\"topics/foo.md\", \"topics/bar.md\"]}\n"
            f"最多返回 {top_k} 个路径，不要输出额外解释。\n\n"
            f"用户请求：\n{query}\n\n"
            f"索引：\n{self.render_index_summary()}\n"
        )

        try:
            response = llm_client.generate(
                messages=[ConversationMessage(role="user", content=prompt)],
                tools=[],
                system_prompt="你只负责从索引中挑选最相关的长期经验文件路径。",
            )
            selected_paths = self._parse_retrieval_paths(response.message.content, top_k)
        except Exception:
            selected_paths = []

        if not selected_paths:
            selected_paths = self._fallback_retrieve(query=query, topics=topics, top_k=top_k)

        topic_by_path = {topic.path: topic for topic in topics}
        return [topic_by_path[path] for path in selected_paths if path in topic_by_path]

    def build_request_messages_for_query(
        self,
        *,
        query: str,
        llm_client: BaseLLMClient,
        top_k: int = 5,
    ) -> list[ConversationMessage]:
        """为本轮请求构造按需加载的长期经验消息。"""

        selected_topics = self.retrieve_for_query(
            query=query,
            llm_client=llm_client,
            top_k=top_k,
        )
        if not selected_topics:
            return []

        sections = ["以下是按需加载的长期经验记忆，请仅在相关时参考："]
        for topic in selected_topics:
            topic_path = self.root / topic.path
            if not topic_path.exists():
                continue

            sections.append(
                f"\n[{topic.type}] {topic.name} ({topic.path})\n"
                f"{topic_path.read_text(encoding='utf-8').strip()}"
            )

        if len(sections) == 1:
            return []

        return [ConversationMessage(role="user", content="\n".join(sections).strip())]

    def maybe_update_from_dialogue(
        self,
        *,
        llm_client: BaseLLMClient,
        recent_dialogue: list[DialogueRecord],
    ) -> str | None:
        """根据最近对话决定是否更新长期经验。

        这里不做“每轮都全量浏览所有记忆”：
        - 只给模型现有索引摘要
        - 只给最近 N 条原始对话
        - 最多允许产出少量 upsert 项
        """

        if not recent_dialogue:
            return None

        dialogue_text = "\n".join(
            f"{item.role}: {item.content}" for item in recent_dialogue
        )

        prompt = (
            "你是长期经验提取器。\n"
            "请根据现有长期经验索引和最近对话，判断是否需要把稳定偏好、项目约定、用户纠正、可复用经验写入长期经验。\n"
            "只在信息具有跨会话价值时才写入；一次最多输出 2 条 upsert。\n"
            "输出必须是 JSON，格式如下：\n"
            "{\"items\": [{\"decision\": \"ignore|upsert\", \"name\": \"\", \"description\": \"\", "
            "\"type\": \"user|project|feedback|reference\", \"path\": \"topics/xxx.md\", \"content\": \"\"}]}\n"
            "如果不需要写入，返回 {\"items\": []}。\n\n"
            f"现有索引：\n{self.render_index_summary()}\n\n"
            f"最近对话：\n{dialogue_text}\n"
        )

        try:
            response = llm_client.generate(
                messages=[ConversationMessage(role="user", content=prompt)],
                tools=[],
                system_prompt="你只负责提取跨会话长期经验，并以 JSON 返回。",
            )
            payload = self._safe_json_load(response.message.content)
        except Exception:
            return None

        items = payload.get("items", [])
        if not isinstance(items, list) or not items:
            return None

        applied = 0
        existing_topics = self.load_index()
        for item in items[:2]:
            if not isinstance(item, dict):
                continue
            if str(item.get("decision", "")).strip() != "upsert":
                continue

            normalized = self._normalize_memory_item(item)
            if normalized is None:
                continue

            topic, content = normalized
            merge_target = self._find_merge_target(
                topic=topic,
                content=content,
                existing_topics=existing_topics,
            )
            if merge_target is not None:
                topic = self._merge_topic_metadata(
                    existing_topic=merge_target,
                    incoming_topic=topic,
                )
                content = self._merge_topic_content(
                    existing_content=self._read_topic_body(merge_target.path),
                    incoming_content=content,
                )

            self._upsert_topic(topic=topic, content=content)
            existing_topics = [item for item in existing_topics if item.path != topic.path]
            existing_topics.append(topic)
            applied += 1

        if applied == 0:
            return None
        return f"已更新 {applied} 条长期经验。"

    def _upsert_topic(self, *, topic: MemoryTopic, content: str) -> None:
        """写入或更新 topic 文件，并重建索引。"""

        topic_path = self.root / topic.path
        topic_path.parent.mkdir(parents=True, exist_ok=True)
        topic_path.write_text(
            "---\n"
            f"name: {topic.name}\n"
            f"description: {topic.description}\n"
            f"type: {topic.type}\n"
            "---\n\n"
            f"{content.strip()}\n",
            encoding="utf-8",
        )

        topics = self.load_index()
        kept = [item for item in topics if item.path != topic.path]
        kept.append(topic)
        kept.sort(key=lambda item: item.path)

        lines = [
            "---",
            "version: 1",
            "kind: auto-memory-index",
            "---",
            "",
            "# MEMORY",
            "",
            "该文件是长期经验索引，不直接保存完整经验内容。",
            "每条记录使用单行结构，供运行时按需检索。",
            "",
        ]
        for item in kept:
            lines.append(
                f"- path: {item.path} | name: {item.name} | type: {item.type} | description: {item.description}"
            )
        self.index_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")

    def _normalize_memory_item(
        self,
        raw_item: dict[str, object],
    ) -> tuple[MemoryTopic, str] | None:
        """把模型产出的 memory item 归一化。

        这里的目标不是改写用户语义，而是把可变信息收敛成稳定结构：
        - type 归一到固定枚举
        - name 尽量稳定
        - path 尽量按主题固定
        - description 控制成短摘要，便于索引检索
        """

        raw_content = str(raw_item.get("content", "")).strip()
        if not raw_content:
            return None

        normalized_type = self._normalize_memory_type(str(raw_item.get("type", "")))
        normalized_name = self._normalize_topic_name(
            raw_name=str(raw_item.get("name", "")),
            memory_type=normalized_type,
            content=raw_content,
            description=str(raw_item.get("description", "")),
        )
        normalized_description = self._normalize_topic_description(
            raw_description=str(raw_item.get("description", "")),
            name=normalized_name,
            memory_type=normalized_type,
            content=raw_content,
        )
        normalized_path = self._normalize_memory_path(
            raw_path=str(raw_item.get("path", "")),
            name=normalized_name,
            memory_type=normalized_type,
        )

        return (
            MemoryTopic(
                name=normalized_name,
                description=normalized_description,
                type=normalized_type,
                path=normalized_path,
            ),
            raw_content,
        )

    def _normalize_memory_type(self, raw_type: str) -> str:
        """把任意输入的类型字段收敛到固定枚举。"""

        normalized = raw_type.strip().lower()
        if normalized in VALID_MEMORY_TYPES:
            return normalized
        return "reference"

    def _normalize_topic_name(
        self,
        *,
        raw_name: str,
        memory_type: str,
        content: str,
        description: str,
    ) -> str:
        """稳定化 topic 名称，减少同义 topic 分裂。"""

        raw = raw_name.strip()
        if raw:
            candidate = re.sub(r"\s+", " ", raw).strip()
        else:
            candidate = ""

        haystack = f"{candidate} {description} {content}".lower()
        if any(token in haystack for token in {"回答风格", "回复风格", "response style", "先给结论"}):
            return "User Response Style"
        if any(token in haystack for token in {"编码偏好", "code style", "coding preference"}):
            return "Coding Preferences"
        if any(token in haystack for token in {"hook", "横切", "主循环", "hook boundary"}):
            return "Hook Boundary"
        if any(token in haystack for token in {"memory", "记忆系统", "记忆分层", "memory layer"}):
            return "Memory Layer Design"

        if candidate:
            return candidate[:80]

        default_names = {
            "user": "User Preferences",
            "project": "Project Conventions",
            "feedback": "User Feedback Notes",
            "reference": "Reference Notes",
        }
        return default_names.get(memory_type, "Reference Notes")

    def _normalize_topic_description(
        self,
        *,
        raw_description: str,
        name: str,
        memory_type: str,
        content: str,
    ) -> str:
        """把 description 归一成适合检索的单行摘要。"""

        candidate = re.sub(r"\s+", " ", raw_description).strip()
        if not candidate:
            first_line = content.strip().splitlines()[0].strip() if content.strip() else ""
            candidate = first_line or f"{name} 的长期经验摘要"

        candidate = candidate[:120].strip()
        if candidate:
            return candidate
        return f"{memory_type} 类型长期经验"

    def _normalize_memory_path(
        self,
        *,
        raw_path: str,
        name: str,
        memory_type: str,
    ) -> str:
        """尽量把路径稳定到固定主题，减少反复创建新文件。"""

        normalized_raw = self._normalize_topic_path(raw_path)
        if raw_path.strip():
            return normalized_raw

        preferred = {
            "User Response Style": "topics/user-response-style.md",
            "Coding Preferences": "topics/coding-preferences.md",
            "Hook Boundary": "topics/hook-boundary.md",
            "Memory Layer Design": "topics/memory-layer-design.md",
        }
        if name in preferred:
            return preferred[name]

        return f"topics/{memory_type}-{self._slugify(name)}.md"

    def _find_merge_target(
        self,
        *,
        topic: MemoryTopic,
        content: str,
        existing_topics: list[MemoryTopic],
    ) -> MemoryTopic | None:
        """为新候选寻找最合适的既有 topic，避免同义分裂。"""

        for existing in existing_topics:
            if existing.path == topic.path:
                return existing

        for existing in existing_topics:
            if (
                existing.type == topic.type
                and existing.name.strip().lower() == topic.name.strip().lower()
            ):
                return existing

        incoming_tokens = self._tokenize(
            f"{topic.name} {topic.description} {content[:200]}"
        )
        best_score = 0
        best_topic: MemoryTopic | None = None

        for existing in existing_topics:
            if existing.type != topic.type:
                continue

            existing_tokens = self._tokenize(
                f"{existing.name} {existing.description} {self._read_topic_body(existing.path)[:200]}"
            )
            score = len(incoming_tokens & existing_tokens)
            if score > best_score:
                best_score = score
                best_topic = existing

        if best_score >= 3:
            return best_topic
        return None

    def _merge_topic_metadata(
        self,
        *,
        existing_topic: MemoryTopic,
        incoming_topic: MemoryTopic,
    ) -> MemoryTopic:
        """合并 topic 元数据时，优先保持既有稳定路径。"""

        description = existing_topic.description
        if incoming_topic.description and incoming_topic.description not in description:
            if len(incoming_topic.description) < len(description) or not description:
                description = incoming_topic.description

        return MemoryTopic(
            name=existing_topic.name or incoming_topic.name,
            description=description or incoming_topic.description,
            type=existing_topic.type or incoming_topic.type,
            path=existing_topic.path,
        )

    def _merge_topic_content(
        self,
        *,
        existing_content: str,
        incoming_content: str,
    ) -> str:
        """把新内容并到既有 topic 正文里，同时尽量避免重复。"""

        existing = existing_content.strip()
        incoming = incoming_content.strip()
        if not existing:
            return incoming
        if not incoming:
            return existing
        if incoming in existing:
            return existing
        if existing in incoming:
            return incoming

        return (
            f"{existing}\n\n"
            "## 增量补充\n\n"
            f"{incoming}\n"
        ).strip()

    def _read_topic_body(self, topic_path: str) -> str:
        """读取 topic 正文，不包含 frontmatter。"""

        path = self.root / topic_path
        if not path.exists():
            return ""

        text = path.read_text(encoding="utf-8").strip()
        if not text.startswith("---"):
            return text

        parts = text.split("---", 2)
        if len(parts) < 3:
            return text
        return parts[2].strip()

    def _fallback_retrieve(
        self,
        *,
        query: str,
        topics: list[MemoryTopic],
        top_k: int,
    ) -> list[str]:
        """当 LLM 检索失败时，退回简单关键词打分。"""

        tokens = self._tokenize(query)
        scored: list[tuple[int, str]] = []
        for topic in topics:
            haystack = self._tokenize(
                f"{topic.name} {topic.description} {topic.type} {topic.path}"
            )
            score = len(tokens & haystack)
            if score > 0:
                scored.append((score, topic.path))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [path for _, path in scored[:top_k]]

    def _parse_retrieval_paths(self, content: str, top_k: int) -> list[str]:
        """解析检索器返回的 JSON 路径列表。"""

        payload = self._safe_json_load(content)
        raw_paths = payload.get("paths", [])
        if not isinstance(raw_paths, list):
            return []

        paths: list[str] = []
        for item in raw_paths:
            normalized = self._normalize_topic_path(str(item).strip())
            if normalized and normalized not in paths:
                paths.append(normalized)
            if len(paths) >= top_k:
                break
        return paths

    def _safe_json_load(self, content: str) -> dict:
        """尽量从模型输出里提取 JSON 对象。"""

        text = content.strip()
        if text.startswith("```"):
            parts = text.split("```")
            for part in parts:
                candidate = part.strip()
                if candidate.startswith("json"):
                    candidate = candidate[4:].strip()
                if candidate.startswith("{") and candidate.endswith("}"):
                    return json.loads(candidate)
        if text.startswith("{") and text.endswith("}"):
            return json.loads(text)

        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            return json.loads(match.group(0))
        return {}

    def _normalize_topic_path(self, raw_path: str) -> str:
        """把模型输出的路径规范到 `.memory/topics/*.md`。"""

        candidate = raw_path.replace("\\", "/").strip()
        if not candidate:
            candidate = "topics/memory-note.md"
        if not candidate.startswith("topics/"):
            candidate = f"topics/{candidate.lstrip('./')}"
        if not candidate.endswith(".md"):
            candidate = f"{candidate}.md"
        return candidate

    def _slugify(self, text: str) -> str:
        """把标题转成稳定的 topic 文件名。"""

        ascii_candidate = re.sub(r"[^A-Za-z0-9]+", "-", text.lower()).strip("-")
        if ascii_candidate:
            return ascii_candidate[:80]

        tokens = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", text)
        if not tokens:
            return "memory-note"
        joined = "-".join(tokens).lower()
        return joined[:80]

    def _tokenize(self, text: str) -> set[str]:
        """做一个很轻量的检索分词。"""

        return {token for token in re.findall(r"[A-Za-z0-9_\-\u4e00-\u9fff]+", text.lower()) if len(token) >= 2}
