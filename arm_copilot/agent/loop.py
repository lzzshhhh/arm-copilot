"""从 0 实现的 tool-calling 主循环。

流程：用户输入 → LLM（可返回 tool_calls）→ 本地执行工具 → 结果回填 →
再次请求 LLM，直到模型给出纯文本回复或达到步数上限。
"""

from __future__ import annotations

from typing import Any, Callable

from ..tools.base import ToolRegistry
from .llm import LLMClient

EventCb = Callable[[str, dict], None]


class Agent:
    def __init__(self, llm: LLMClient, registry: ToolRegistry, max_steps: int = 12):
        self.llm = llm
        self.registry = registry
        self.max_steps = max_steps

    def handle(self, user_text: str, history: list[dict], on_event: EventCb | None = None) -> str:
        """处理一轮用户输入，返回最终回复文本。

        history 会被就地追加（user / assistant / tool 消息），由调用方持有跨轮上下文。
        on_event(kind, data) 用于 UI 展示与日志：kind 为 tool_start / tool_end。
        """
        history.append({"role": "user", "content": user_text})
        final = ""
        for _ in range(self.max_steps):
            message = self.llm.chat(history, self.registry.schemas())
            entry: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
            tool_calls = list(getattr(message, "tool_calls", None) or [])
            if tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in tool_calls
                ]
            history.append(entry)
            if not tool_calls:
                final = entry["content"]
                break
            for tc in tool_calls:
                name = tc.function.name
                arguments = tc.function.arguments or "{}"
                if on_event:
                    on_event("tool_start", {"name": name, "arguments": arguments})
                result = self.registry.call(name, arguments)
                if on_event:
                    on_event("tool_end", {"name": name, "result": result})
                history.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        else:
            final = "（这一轮的工具调用次数已达上限，我先停在这里。请告诉我接下来怎么做。）"
        return final
