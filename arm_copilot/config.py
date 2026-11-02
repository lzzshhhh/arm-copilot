"""配置加载：LLM 接入参数、mock 开关、日志目录。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    api_base: str
    api_key: str
    model: str
    mock: bool = False
    log_dir: Path = field(default_factory=lambda: Path("logs").resolve())
    max_tool_steps: int = 12


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


def load_config(mock: bool = False) -> Config:
    """从 .env 与环境变量读取配置。

    环境变量：
      ARM_COPILOT_API_BASE  OpenAI 兼容接口地址（默认 OpenAI 官方）
      ARM_COPILOT_API_KEY   密钥（缺省回退 OPENAI_API_KEY）
      ARM_COPILOT_MODEL     模型名
      ARM_COPILOT_MOCK      置 1 等效 --mock
    """
    load_dotenv()

    api_base = os.getenv("ARM_COPILOT_API_BASE", "https://api.openai.com/v1").rstrip("/")
    api_key = os.getenv("ARM_COPILOT_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    model = os.getenv("ARM_COPILOT_MODEL", "")
    mock = mock or os.getenv("ARM_COPILOT_MOCK", "").lower() in ("1", "true", "yes")

    if not api_key:
        raise ConfigError(
            "未配置 LLM API 密钥。请在 .env 或环境变量中设置 ARM_COPILOT_API_KEY"
            "（或 OPENAI_API_KEY），参考 .env.example。"
        )
    if not model:
        raise ConfigError(
            "未配置模型名。请设置 ARM_COPILOT_MODEL，例如 glm-4.6 / deepseek-chat /"
            " qwen-plus / gpt-4o-mini（以你的服务商文档为准）。"
        )
    return Config(api_base=api_base, api_key=api_key, model=model, mock=mock)
