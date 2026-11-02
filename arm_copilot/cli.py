"""Rich 终端聊天入口。"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt

from . import __version__
from .agent.llm import LLMClient
from .agent.loop import Agent
from .agent.prompts import SYSTEM_PROMPT
from .config import ConfigError, load_config
from .tools import ToolContext, build_registry

console = Console()

_RISK_BADGE = {
    "safe": "[green]safe · 查询类[/green]",
    "risky": "[yellow]risky · 会改动软件环境[/yellow]",
    "hardware": "[red]hardware · 会驱动真实电机[/red]",
}


def make_approver():
    def approve(command: str, risk: str, reason: str) -> bool:
        body = f"[bold]{command}[/bold]\n\n风险等级：{_RISK_BADGE.get(risk, risk)}"
        if reason:
            body += f"\n说明：{reason}"
        console.print(Panel(body, title="确认执行命令", border_style="yellow"))
        if risk == "hardware":
            console.print("[red]⚠ 执行后机械臂可能真实运动：请确保工作区无人手、无易倒物品。[/red]")
        try:
            return Prompt.ask("是否执行", choices=["y", "n"], default="n") == "y"
        except (EOFError, KeyboardInterrupt):
            return False

    return approve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arm-copilot", description="具身智能产品部署助手")
    parser.add_argument("--mock", action="store_true", help="模拟执行所有命令（无硬件演示 / 测试）")
    parser.add_argument("--version", action="version", version=f"arm-copilot {__version__}")
    args = parser.parse_args(argv)

    console.print(Panel.fit(
        f"[bold]臂管家 arm-copilot[/bold] v{__version__}\n"
        "具身智能产品（SO-101 等）安装 · 配置 · 校准 AI 助手",
        border_style="blue",
    ))
    try:
        config = load_config(mock=args.mock)
    except ConfigError as e:
        console.print(f"[red]配置错误：{e}[/red]")
        return 2
    if config.mock:
        console.print("[cyan]mock 模式：所有命令仅模拟执行，不会触碰真实硬件。[/cyan]")

    log_dir = config.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"session-{datetime.now():%Y%m%d-%H%M%S}.md"

    def log(text: str) -> None:
        try:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass

    llm = LLMClient(config)
    ctx = ToolContext(mock=config.mock, approve=make_approver())
    agent = Agent(llm, build_registry(ctx), max_steps=config.max_tool_steps)
    history: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    def on_event(kind: str, data: dict) -> None:
        if kind == "tool_start":
            console.print(f"[dim]→ 工具 {data['name']} {data['arguments'][:160]}[/dim]")
        elif kind == "tool_end":
            preview = data["result"].replace("\n", " ")[:240]
            console.print(Panel(f"[dim]{preview}[/dim]", title=f"工具结果 · {data['name']}", border_style="dim"))
            log(f"\n### 工具 {data['name']}\n```\n{data['result'][:2000]}\n```\n")

    log(f"# arm-copilot 会话 {datetime.now():%Y-%m-%d %H:%M:%S}（mock={config.mock}）\n")
    console.print(
        "输入问题开始对话；[bold]/reset[/bold] 清空上下文，[bold]/exit[/bold] 退出。"
        f"会话日志：[dim]{log_path}[/dim]"
    )

    while True:
        try:
            user = console.input("[bold blue]你 >[/bold blue] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not user:
            continue
        if user in ("/exit", "/quit"):
            break
        if user == "/reset":
            history = [{"role": "system", "content": SYSTEM_PROMPT}]
            console.print("[dim]已清空对话上下文。[/dim]")
            continue

        log(f"\n## 用户\n{user}\n")
        try:
            reply = agent.handle(user, history, on_event)
        except KeyboardInterrupt:
            console.print("[red]已中断本次回答。[/red]")
            continue
        except Exception as e:  # LLM 网络错误等：提示但不退出会话
            console.print(f"[red]调用模型失败：{type(e).__name__}: {e}[/red]")
            continue
        console.print(Panel(Markdown(reply or "（无输出）"), title="臂管家", border_style="blue"))
        log(f"\n## 臂管家\n{reply}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
