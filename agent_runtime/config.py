"""配置相关辅助函数。

这里保持轻量，只负责：
- 读取本地 `.env`
- 从环境变量组装 LLM 配置
- 暴露少量和 runtime 记忆系统相关的配置项
"""

from __future__ import annotations

import os
import json
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
    """从环境变量构造单个 OpenAI-compatible 配置。

    兼容旧调用方：如果配置了多 endpoint，则返回第一项作为主配置。
    """

    return load_openai_compatible_configs()[0]


def load_openai_compatible_configs() -> list[OpenAICompatibleConfig]:
    """从环境变量构造一个或多个 OpenAI-compatible 配置。

    支持两种模式：
    1. 旧模式：LLM_MODEL / LLM_API_KEY / LLM_BASE_URL ...
    2. 新模式：LLM_ENDPOINTS_JSON

    `LLM_ENDPOINTS_JSON` 的示例：
    {
      "endpoints": [
        {
          "model": "gpt-5.3-codex",
          "api_key": "...",
          "base_url": "https://a.example.com/v1"
        },
        {
          "model": "gpt-5.3-codex",
          "api_key": "...",
          "base_url": "https://b.example.com/v1"
        }
      ]
    }
    """

    load_dotenv_if_present()

    endpoints_json = os.getenv("LLM_ENDPOINTS_JSON", "").strip()
    if endpoints_json:
        payload = json.loads(endpoints_json)
        items = payload.get("endpoints", payload) if isinstance(payload, dict) else payload
        if not isinstance(items, list) or not items:
            raise ValueError("LLM_ENDPOINTS_JSON 必须是非空数组，或带 endpoints 字段的对象。")

        defaults = _load_endpoint_defaults()
        configs: list[OpenAICompatibleConfig] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            model = str(item.get("model", defaults["model"]))
            api_key = str(item.get("api_key", defaults["api_key"]))
            if not model or not api_key:
                raise KeyError("LLM_MODEL")
            configs.append(
                OpenAICompatibleConfig(
                    model=model,
                    api_key=api_key,
                    base_url=str(item.get("base_url", defaults["base_url"])),
                    timeout_seconds=int(item.get("timeout_seconds", defaults["timeout_seconds"])),
                    max_tokens=int(item.get("max_tokens", defaults["max_tokens"])),
                    temperature=float(item.get("temperature", defaults["temperature"])),
                    context_window=int(item.get("context_window", defaults["context_window"])),
                )
            )
        if not configs:
            raise ValueError("LLM_ENDPOINTS_JSON 中没有可用 endpoint。")
        return configs

    defaults = _load_endpoint_defaults()
    return [
        OpenAICompatibleConfig(
            model=str(defaults["model"]),
            api_key=str(defaults["api_key"]),
            base_url=str(defaults["base_url"]),
            timeout_seconds=int(defaults["timeout_seconds"]),
            max_tokens=int(defaults["max_tokens"]),
            temperature=float(defaults["temperature"]),
            context_window=int(defaults["context_window"]),
        )
    ]


def _load_endpoint_defaults() -> dict[str, object]:
    """读取单 endpoint 模式下的默认配置。"""

    return {
        "model": os.environ["LLM_MODEL"],
        "api_key": os.environ["LLM_API_KEY"],
        "base_url": os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        "timeout_seconds": int(os.getenv("LLM_TIMEOUT_SECONDS", "120")),
        "max_tokens": int(os.getenv("LLM_MAX_TOKENS", "2000")),
        "temperature": float(os.getenv("LLM_TEMPERATURE", "0")),
        "context_window": int(os.getenv("LLM_CONTEXT_WINDOW", "16000")),
    }
