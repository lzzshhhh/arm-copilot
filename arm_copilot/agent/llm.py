"""OpenAI 兼容 Chat API 的最小封装。"""

from __future__ import annotations

from typing import Any

from openai import OpenAI

from ..config import Config


class LLMClient:
    """任何 OpenAI 兼容端点（GLM / DeepSeek / Qwen / OpenAI / 本地 vLLM 等）均可。"""

    def __init__(self, config: Config):
        self._config = config
        self._client = OpenAI(base_url=config.api_base, api_key=config.api_key)

    def chat(self, messages: list[dict], tools: list[dict]) -> Any:
        """一轮对话。返回 assistant 消息对象（可能含 tool_calls）。"""
        resp = self._client.chat.completions.create(
            model=self._config.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        return resp.choices[0].message
