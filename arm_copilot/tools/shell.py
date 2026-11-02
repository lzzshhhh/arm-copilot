"""run_command 工具：白名单校验 + 风险分级确认 + mock 模拟执行。"""

from __future__ import annotations

import platform
import re
import subprocess
import sys
from typing import Callable

from .base import RISK_HARDWARE, RISK_RISKY, RISK_SAFE, Tool, ToolContext

Approver = Callable[[str, str, str], bool]

# 白名单：lerobot-* 为前缀匹配；python 仅限查询版本与 -m pip 形式
_ALLOWED_PREFIXES = ("lerobot-",)
_ALLOWED_FIRST = {"pip", "pip3", "uv"}
_PYTHON = {"python", "python3"}

# 黑名单（第二道防线，命中即拒绝）
_FORBIDDEN_WORDS = (
    "rm", "del", "rmdir", "rd", "sudo", "apt", "apt-get", "yum", "dnf",
    "shutdown", "reboot", "mkfs", "dd", "format", "regedit", "reg",
    "powershell", "curl", "wget", "chmod", "chown", "kill", "taskkill",
    "git",
)
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in _FORBIDDEN_WORDS) + r")\b")
_FORBIDDEN_CHARS = (";", "&&", "||", "|", ">", "<", "`", "$(", "\n", "\r")

# 会让真实电机运动的命令
_HARDWARE_PREFIXES = (
    "lerobot-setup-motors", "lerobot-calibrate", "lerobot-teleoperate",
    "lerobot-record", "lerobot-replay", "lerobot-eval", "lerobot-rollout",
)
# 长时间占用资源的任务
_HEAVY_PREFIXES = ("lerobot-train", "lerobot-train-tokenizer")

_TIMEOUTS = {RISK_SAFE: 60, RISK_RISKY: 900, RISK_HARDWARE: 1800}


def validate(command: str) -> tuple[bool, str]:
    """白名单 + 危险模式双重校验。返回 (是否放行, 拒绝原因)。"""
    cmd = command.strip()
    if not cmd:
        return False, "空命令"
    low = cmd.lower()
    if _FORBIDDEN_RE.search(low):
        return False, "包含被禁止的命令或单词（rm/sudo/curl/git 等）"
    for ch in _FORBIDDEN_CHARS:
        if ch in cmd:
            return False, f"包含被禁止的字符或连接符：{ch!r}（不允许管道/重定向/链式命令）"
    first = low.split(maxsplit=1)[0]
    if first.startswith(_ALLOWED_PREFIXES) or first in _ALLOWED_FIRST:
        return True, ""
    if first in _PYTHON:
        rest = low[len(first):].strip()
        if rest.startswith(("--version", "-v", "-m pip")):
            return True, ""
        return False, "python 仅允许 --version / -V 与 -m pip 形式"
    return False, (
        f"命令 {first!r} 不在白名单内"
        "（仅允许 lerobot-*、pip、uv、python --version、python -m pip）"
    )


def classify_risk(command: str) -> str:
    cmd = command.strip().lower()
    if cmd.startswith(_HARDWARE_PREFIXES):
        return RISK_HARDWARE
    if cmd.startswith(_HEAVY_PREFIXES):
        return RISK_RISKY
    if re.search(r"\b(install|uninstall)\b", cmd):
        return RISK_RISKY
    return RISK_SAFE


def _run_real(command: str, risk: str) -> tuple[int, str, str]:
    timeout = _TIMEOUTS.get(risk, 300)
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"命令超时（>{timeout}s）已终止：{command}"
    except OSError as e:
        return 127, "", f"无法启动命令：{e}"


def _win() -> bool:
    return platform.system() == "Windows"


def _mock_ports() -> str:
    if _win():
        return (
            "CRITICAL:find-port: Found 2 ports:\n"
            "[0] COM3 [FEETECH] (available)\n"
            "[1] COM4 [FEETECH] (available)"
        )
    return (
        "CRITICAL:find-port: Found 2 ports:\n"
        "[0] /dev/ttyACM0 [FEETECH] (available)\n"
        "[1] /dev/ttyACM1 [FEETECH] (available)"
    )


def mock_run(command: str, state: dict) -> tuple[int, str, str]:
    """按命令模式返回模拟输出；state 记录 mock 环境的安装状态（如 lerobot 是否已装）。"""
    cmd = command.strip()
    low = cmd.lower()
    py = platform.python_version()

    if low.startswith(("python", "python3")):
        return 0, f"Python {py}", ""
    if low.startswith(("pip --version", "pip3 --version", "pip -v", "pip3 -v")):
        return 0, f"pip 25.2 from /usr/local/bin/pip (python {py})", ""
    if low.startswith(("pip show", "pip3 show")):
        parts = cmd.split()
        pkg = parts[-1] if len(parts) > 2 else ""
        if pkg == "lerobot" and state.get("lerobot"):
            return 0, (
                "Name: lerobot\n"
                f"Version: {state['lerobot']}\n"
                "Summary: LeRobot: State-of-the-art machine learning for real-world robotics"
            ), ""
        return 1, "", f"WARNING: Package(s) not found: {pkg}"
    if low.startswith(("pip install", "pip3 install")):
        if "lerobot" in low:
            state["lerobot"] = "0.5.2"
            return 0, (
                "Looking in indexes: https://pypi.org/simple\n"
                "Collecting lerobot\n"
                "Successfully installed feetech-sdk-1.0.3 lerobot-0.5.2"
            ), ""
        return 0, "Successfully installed (mock)", ""

    if low.startswith("lerobot-find-port"):
        if not state.get("lerobot"):
            return 127, "", "'lerobot-find-port' is not recognized as a command（lerobot 未安装）"
        return 0, _mock_ports(), ""
    if low.startswith("lerobot-setup-motors"):
        leader = "--teleop.type" in low
        arm = "so101_leader" if leader else "so101_follower"
        if _win():
            port = "COM4" if leader else "COM3"
        else:
            port = "/dev/ttyACM1" if leader else "/dev/ttyACM0"
        lines = [
            f"Setting up motors for {arm} on port {port}",
            "Scanning bus... found 6 motors",
        ]
        for i, name in enumerate(
            ["base_rotation", "shoulder_pitch", "shoulder_rotation",
             "elbow_flex", "wrist_roll", "gripper"], start=1,
        ):
            lines.append(f"Writing ID {i} -> {name} ... OK")
        lines.append("Done. 建议给机械臂断电重启后继续下一步。")
        return 0, "\n".join(lines), ""
    if low.startswith("lerobot-calibrate"):
        arm = "so101_leader" if "--teleop.type" in low else "so101_follower"
        return 0, "\n".join([
            f"Preparing to calibrate {arm}",
            "请按提示依次将各关节摆到校准姿态：",
            "[base] 摆到中间位置，按回车记录 ... OK",
            "[shoulder_pitch] 摆到水平位置，按回车记录 ... OK",
            "[shoulder_rotation / elbow / wrist / gripper] ... OK",
            f"Calibration saved to ~/.cache/huggingface/lerobot/calibration/{arm}.json",
        ]), ""
    if low.startswith("lerobot-teleoperate"):
        return 0, (
            "Connecting so101_follower <-> so101_leader ...\n"
            "Teleoperation started — 移动 leader 臂，follower 将同步跟随（Ctrl+C 退出）。\n"
            "[mock] 已模拟 10 秒遥操作，关节同步正常。"
        ), ""

    return 0, f"[mock] 已模拟执行：{cmd}", ""


def format_result(command: str, code: int, stdout: str, stderr: str, limit: int = 3000) -> str:
    def _trim(text: str) -> str:
        text = text.strip()
        if not text:
            return "(无输出)"
        if len(text) <= limit:
            return text
        head, tail = text[: int(limit * 0.6)], text[-int(limit * 0.3):]
        return f"{head}\n……（输出过长，中间省略）……\n{tail}"

    return (
        f"命令: {command}\n退出码: {code}\n"
        f"--- stdout ---\n{_trim(stdout)}\n--- stderr ---\n{_trim(stderr)}"
    )


def make_run_command_tool(ctx: ToolContext) -> Tool:
    def handler(args: dict) -> str:
        command = str(args.get("command", "")).strip()
        reason = str(args.get("reason", ""))
        if not command:
            return "错误：缺少 command 参数。"
        ok, why = validate(command)
        if not ok:
            return f"拒绝执行：{why}\n命令：{command}"
        risk = classify_risk(command)
        if risk != RISK_SAFE and ctx.approve is not None:
            if not ctx.approve(command, risk, reason):
                return (
                    f"用户拒绝执行该命令（{risk} 级）：{command}\n"
                    "请向用户说明情况，调整方案或询问下一步。"
                )
        if ctx.mock:
            code, out, err = mock_run(command, ctx.mock_state)
        else:
            code, out, err = _run_real(command, risk)
        return format_result(command, code, out, err)

    return Tool(
        name="run_command",
        description=(
            "在本机终端执行一条白名单内的命令（lerobot-*、pip、python --version 等），"
            "用于环境检查、安装依赖、配置与校准机械臂。risky/hardware 级命令会先请用户确认。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的完整命令行"},
                "reason": {"type": "string", "description": "执行目的（一两句话），将展示给用户"},
            },
            "required": ["command", "reason"],
        },
        handler=handler,
    )
