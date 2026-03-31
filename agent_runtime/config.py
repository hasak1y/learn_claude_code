"""配置相关辅助函数。

这个模块会故意保持轻量：
- 读取一个可选的本地 `.env`
- 读取少量必要环境变量
- 组装 LLM 适配器使用的配置对象

第一版先不为了加载配置额外引入依赖，
用一个很小的本地加载器就足够了。
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


def load_dotenv_if_present(dotenv_path: str = ".env") -> None:
    """如果本地存在 `.env`，就读取其中简单的 `KEY=VALUE` 配置。

    这个加载器会故意保持克制：
    - 文件不存在时直接跳过
    - 已有环境变量优先，不会被覆盖
    - 注释和空行会被忽略

    对第一版本地开发来说，这样已经够用，
    不需要为此额外引入新的库。
    """

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
    """从环境变量构造 OpenAI-compatible 配置。

    必填项：
    - `LLM_MODEL`
    - `LLM_API_KEY`

    可选项：
    - `LLM_BASE_URL`
    - `LLM_TIMEOUT_SECONDS`
    - `LLM_MAX_TOKENS`
    - `LLM_TEMPERATURE`
    """

    load_dotenv_if_present()

    model = os.environ["LLM_MODEL"]
    api_key = os.environ["LLM_API_KEY"]
    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    timeout_seconds = int(os.getenv("LLM_TIMEOUT_SECONDS", "120"))
    max_tokens = int(os.getenv("LLM_MAX_TOKENS", "2000"))
    temperature = float(os.getenv("LLM_TEMPERATURE", "0"))

    return OpenAICompatibleConfig(
        model=model,
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        max_tokens=max_tokens,
        temperature=temperature,
    )
