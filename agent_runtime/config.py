"""配置相关辅助函数。

这里保持轻量，只负责：
- 读取本地 `.env`
- 从环境变量组装 LLM 配置
- 暴露少量和 runtime 记忆系统相关的配置项
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class OpenAICompatibleConfig:
    """OpenAI-compatible `chat/completions` 接口配置。"""

    model: str
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: int = 120
    max_tokens: int = 2000
    temperature: float = 0.0
    context_window: int = 16000


def load_dotenv_if_present(dotenv_path: str = ".env") -> None:
    """如果存在 `.env`，就读取其中简单的 `KEY=VALUE` 配置。"""

    path = Path(dotenv_path)
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value


def load_openai_compatible_config() -> OpenAICompatibleConfig:
    """从环境变量构造 OpenAI-compatible 配置。"""

    load_dotenv_if_present()

    model = os.environ["LLM_MODEL"]
    api_key = os.environ["LLM_API_KEY"]
    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    timeout_seconds = int(os.getenv("LLM_TIMEOUT_SECONDS", "120"))
    max_tokens = int(os.getenv("LLM_MAX_TOKENS", "2000"))
    temperature = float(os.getenv("LLM_TEMPERATURE", "0"))
    context_window = int(os.getenv("LLM_CONTEXT_WINDOW", "16000"))

    return OpenAICompatibleConfig(
        model=model,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        max_tokens=max_tokens,
        temperature=temperature,
        context_window=context_window,
    )
