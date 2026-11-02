"""工具注册表：统一描述、JSON Schema 与风险分级。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

RISK_SAFE = "safe"          # 查询类，直接执行
RISK_RISKY = "risky"        # 会改变软件环境（安装 / 卸载 / 长训练）
RISK_HARDWARE = "hardware"  # 会让真实电机运动


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema（type: object, properties, required）
    handler: Callable[[dict[str, Any]], str]
    risk: str = RISK_SAFE

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolContext:
    """工具运行环境：mock 开关、确认回调、mock 状态记忆。"""

    mock: bool = False
    # approve(command, risk, reason) -> bool；None 表示当前环境无法交互确认
    approve: Callable[[str, str, str], bool] | None = None
    mock_state: dict = field(default_factory=dict)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册: {tool.name}")
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def call(self, name: str, arguments_json: str) -> str:
        """按 LLM 给定的名称与 JSON 参数执行工具，返回给模型的结果文本。"""
        tool = self._tools.get(name)
        if tool is None:
            return f"错误：未知工具 '{name}'，可用工具：{', '.join(self._tools)}"
        try:
            args = json.loads(arguments_json) if arguments_json.strip() else {}
        except json.JSONDecodeError as e:
            return f"错误：参数不是合法 JSON：{e}"
        if not isinstance(args, dict):
            return "错误：参数必须是 JSON 对象。"
        try:
            return tool.handler(args)
        except Exception as e:  # 工具内部错误以文本回给模型，避免打断循环
            return f"工具 {name} 执行异常：{type(e).__name__}: {e}"
