"""工具层：注册表构建入口。"""

from __future__ import annotations

from .base import RISK_HARDWARE, RISK_RISKY, RISK_SAFE, Tool, ToolContext, ToolRegistry
from .diagnostics import make_env_check_tool
from .knowledge import make_faq_tool, make_workflow_tool
from .shell import make_run_command_tool


def build_registry(ctx: ToolContext) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(make_run_command_tool(ctx))
    reg.register(make_env_check_tool(ctx))
    reg.register(make_workflow_tool())
    reg.register(make_faq_tool())
    return reg
