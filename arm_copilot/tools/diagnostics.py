"""env_check 工具：部署前主机体检。"""

from __future__ import annotations

import re
import subprocess

from .base import Tool, ToolContext
from .shell import mock_run

_TIMEOUT = 30


def _exec(ctx: ToolContext, command: str) -> tuple[int, str, str]:
    if ctx.mock:
        return mock_run(command, ctx.mock_state)
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=_TIMEOUT, encoding="utf-8", errors="replace",
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (subprocess.TimeoutExpired, OSError) as e:
        return 127, "", str(e)


def _token(out: str, index: int) -> str:
    """取输出第一行的第 index 个词作为版本号。"""
    lines = out.strip().splitlines()
    if not lines:
        return "未知"
    toks = lines[0].split()
    return toks[index] if len(toks) > index else "未知"


_PORT_LINE = re.compile(r"^\[\d+\]", re.MULTILINE)


def _has_port_listing(out: str) -> bool:
    """find-port 输出中是否有「[0] COM3 [FEETECH] (available)」式的端口清单行。

    无设备时该命令可能退出码为 0 且输出一段提示文本，不能只凭退出码与非空判断。
    """
    return bool(_PORT_LINE.search(out))


def make_env_check_tool(ctx: ToolContext) -> Tool:
    def handler(args: dict) -> str:
        lines = ["环境体检结果："]

        code_py, out_py, _ = _exec(ctx, "python --version")
        if code_py != 0:
            code_py, out_py, _ = _exec(ctx, "python3 --version")  # Linux 常只有 python3
        py = _token(out_py, -1) if code_py == 0 else "未知"
        try:
            ok_py = tuple(map(int, py.split(".")[:2])) >= (3, 12)
        except ValueError:
            ok_py = None
        if ok_py:
            lines.append(f"- Python: {py} ✓（满足 LeRobot 要求 ≥3.12）")
        elif ok_py is False:
            lines.append(f"- Python: {py} ⚠ 建议升级到 3.12+（LeRobot 要求）")
        else:
            lines.append(f"- Python: {py}（无法解析版本）")

        code_pip, out_pip, _ = _exec(ctx, "pip --version")
        pip = _token(out_pip, 1) if code_pip == 0 else "未知"
        lines.append(f"- pip: {pip}")

        code_lr, out_lr, _ = _exec(ctx, "pip show lerobot")
        if code_lr == 0 and "Version:" in out_lr:
            ver = next(l.split()[-1] for l in out_lr.splitlines() if l.startswith("Version:"))
            lines.append(f"- LeRobot: {ver} ✓ 已安装")
            code_port, out_port, _ = _exec(ctx, "lerobot-find-port")
            if code_port == 0 and _has_port_listing(out_port):
                lines.append(f"- 串口设备：\n{out_port.strip()}")
            else:
                lines.append("- 串口设备：未检测到 FEETECH 设备（确认机械臂已供电、USB 已连接后重试）")
        else:
            lines.append('- LeRobot: 未安装 ✗ → 需要先执行：pip install "lerobot[feetech]"')

        return "\n".join(lines)

    return Tool(
        name="env_check",
        description=(
            "对当前主机做部署体检：Python / pip 版本、LeRobot 是否安装、FEETECH 串口设备。"
            "调用后把结果解读给用户，并给出下一步建议。"
        ),
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
