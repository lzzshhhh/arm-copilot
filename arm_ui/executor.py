"""命令执行器：供流程引擎实时逐行输出。

- mock：先用 arm_copilot.tools.shell.mock_run 得到完整结果，再逐行回放；
- 真实：Popen 流式读取（stdout 与 stderr 合流），看门狗按风险超时强杀。

之所以放在 arm_ui 而不是改 tools/shell.py：Web 层专属能力，原工具路径不受影响。
"""

from __future__ import annotations

import codecs
import os
import queue as queue_mod
import re
import subprocess
import sys
import threading
import time
from typing import Callable

from arm_copilot.tools.base import RISK_HARDWARE, RISK_RISKY, RISK_SAFE
from arm_copilot.tools.shell import _TIMEOUTS, mock_run

OnLine = Callable[[str], None]


def _child_env() -> dict:
    """子进程统一 UTF-8 stdio + HF 离线模式。

    UTF-8：Windows 管道默认走 ANSI 代码页（中文系统是 GBK），pip 打印包元数据
    里的 🤗 emoji 会 UnicodeEncodeError 崩掉，中文输出也会变乱码。

    HF_HUB_OFFLINE：向导全部本地化（不上传、不下载），lerobot 结束录制时默认
    会连 Hugging Face Hub，网络不通会干等超时（表现为"结束录制后不返回"）。
    离线模式让任何意外的外网调用立即失败，而不是挂住。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["HF_HUB_OFFLINE"] = "1"
    # PATH 补全：服务若由未激活 venv 的终端启动（如直接 python arm_ui/app.py），
    # 子进程 shell 找不到 lerobot-train 等命令，报"不是内部或外部命令"。
    # 把当前解释器目录与项目根 .venv/Scripts 都补到 PATH 最前（存在与否无害）。
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    project_venv = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".venv", "Scripts",
    )
    extras = [exe_dir, project_venv, os.path.join(exe_dir, "Scripts")]
    env["PATH"] = os.pathsep.join(dict.fromkeys(extras + [env.get("PATH", "")]))
    return env

# Web UI 的超时预算比 CLI 更宽：pip 装 torch、真机训练/采集都远超 15/30 分钟。
# （safe 级沿用原值 60s；只放宽 UI 自己的执行器，不动 arm_copilot 原代码）
_TIMEOUTS_UI = {
    RISK_SAFE: _TIMEOUTS[RISK_SAFE],
    RISK_RISKY: 3600,    # pip 安装大依赖：1 小时
    RISK_HARDWARE: 7200,  # 长时间遥操作 / 批量数据采集：2 小时
}

# 训练例外：CPU 上 ACT 每步可达数十秒（视频解码为主），几千步要数小时，
# 按风险等级的 1 小时看门狗会把正常训练拦腰杀掉，单独立预算
_TRAIN_TIMEOUT_UI = 6 * 3600


def _timeout_for(command: str, risk: str) -> int:
    if command.strip().lower().startswith("lerobot-train"):
        return _TRAIN_TIMEOUT_UI
    return _TIMEOUTS_UI.get(risk, 300)


def run_real_streaming(
    command: str, risk: str, on_output: OnLine, handle: dict | None = None
) -> tuple[int, str, str]:
    """真实执行并实时回调输出；返回 (退出码, 完整输出, stderr 空串)。

    关键设计：input() 的提示语没有换行符，逐行读管道永远读不到它——
    改为线程按块读取 + 主循环空闲探测：进程还活着且 ~0.6s 无新数据，
    就把没有换行的残留文字当作"等待输入的提示"立即发出，
    这样前端能实时看到提示并给出对应的操作按钮（回车 / 输入 c）。
    """
    timeout = _timeout_for(command, risk)
    killed = {"v": False}
    try:
        proc = subprocess.Popen(
            command, shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=False, bufsize=0,
            env=_child_env(),
        )
    except OSError as e:
        return 127, f"无法启动命令：{e}", ""
    if handle is not None:
        handle["proc"] = proc

    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    chunks: queue_mod.Queue = queue_mod.Queue()

    def _reader() -> None:
        try:
            fd = proc.stdout.fileno()
            while True:
                data = os.read(fd, 4096)
                if not data:
                    break
                chunks.put(data)
        except (OSError, ValueError):
            pass
        finally:
            chunks.put(None)  # EOF 哨兵

    threading.Thread(target=_reader, daemon=True).start()

    def _kill() -> None:
        killed["v"] = True
        _kill_tree(proc)

    watchdog = threading.Timer(timeout, _kill)
    watchdog.start()

    _EMPTY = object()
    out_parts: list[str] = []
    pending = ""
    eof = False
    idle_rounds = 0

    def _emit(text: str) -> None:
        if text.strip():
            out_parts.append(text)
            on_output(text)

    code = 0
    try:
        while True:
            try:
                item = chunks.get(timeout=0.3)
            except queue_mod.Empty:
                item = _EMPTY

            if item is None:
                eof = True
            elif item is not _EMPTY:
                pending += decoder.decode(item)

            # \n 与 \r（就地刷新的实时表）都作为行边界
            while True:
                m = re.search(r"[\r\n]", pending)
                if not m:
                    break
                piece, pending = pending[: m.start()], pending[m.end():]
                _emit(piece)

            # 进程活着但 ~0.6s 无新数据 + 有未换行残留 → 这是等待输入的提示
            if not eof and pending.strip() and proc.poll() is None and idle_rounds >= 2:
                _emit(pending)
                pending = ""

            if item is _EMPTY:
                idle_rounds += 1
            else:
                idle_rounds = 0

            if eof:
                _emit(pending)
                break

        try:
            code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _kill()
            proc.wait(timeout=10)
            code = 124
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            _kill_tree(proc)

    if killed["v"]:
        code = 124
        out_parts.append(f"命令超时（>{timeout}s）已终止：{command}")
    return code, "\n".join(out_parts), ""


def _kill_tree(proc: subprocess.Popen) -> None:
    """整树终止：shell=True 的直接 kill 只杀掉 cmd 外壳，孙进程可能存活。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        try:
            proc.terminate()
        except OSError:
            pass


def write_stdin(handle: dict | None, text: str) -> bool:
    """向正在运行的交互式命令发送一行输入（自动补换行）。"""
    proc = (handle or {}).get("proc")
    if proc is None or proc.poll() is not None or proc.stdin is None:
        return False
    data = text if text.endswith("\n") else text + "\n"
    try:
        try:
            proc.stdin.write(data)          # 文本管道
        except TypeError:
            proc.stdin.write(data.encode("utf-8"))  # 二进制管道
        proc.stdin.flush()
        return True
    except (OSError, ValueError):
        return False


def send_console_enter() -> bool:
    """向本进程控制台注入回车键（按下+释放，仅 Windows）。

    lerobot 在 Windows 上的 enter_pressed() 用 msvcrt 轮询控制台键盘缓冲，
    读不到我们写进 stdin 管道的回车（校准录制循环正是这种等待方式）。
    本函数与子进程共享同一控制台，WriteConsoleInput 写入的按键能被 kbhit() 看到。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class _KEY_EVENT_RECORD(ctypes.Structure):
            _fields_ = [
                ("bKeyDown", wintypes.BOOL),
                ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD),
                ("wVirtualScanCode", wintypes.WORD),
                ("UnicodeChar", wintypes.WCHAR),
                ("wPadding", wintypes.WORD),
                ("dwControlKeyState", wintypes.DWORD),
            ]

        class _INPUT_RECORD(ctypes.Structure):
            class _Event(ctypes.Union):
                _fields_ = [("KeyEvent", _KEY_EVENT_RECORD)]

            _anonymous_ = ("Event",)
            _fields_ = [("EventType", wintypes.WORD), ("Event", _Event)]

        KEY_EVENT = 0x0001
        VK_RETURN = 0x0D

        def _rec(key_down: bool) -> _INPUT_RECORD:
            r = _INPUT_RECORD(EventType=KEY_EVENT)
            r.KeyEvent = _KEY_EVENT_RECORD(
                bKeyDown=key_down, wRepeatCount=1,
                wVirtualKeyCode=VK_RETURN, wVirtualScanCode=0x1C,
                UnicodeChar="\r",
            )
            return r

        kh = ctypes.windll.kernel32
        h_in = kh.GetStdHandle(-10)  # STD_INPUT_HANDLE
        if not h_in or h_in == -1:
            return False
        kh.FlushConsoleInputBuffer(h_in)  # 清掉用户可能误敲进控制台的键
        records = (_INPUT_RECORD * 2)(_rec(True), _rec(False))  # 按下 + 释放
        written = wintypes.DWORD(0)
        ok = kh.WriteConsoleInputW(h_in, records, 2, ctypes.byref(written))
        return bool(ok) and written.value == 2
    except Exception:
        return False


def run_quick(command: str, timeout: float = 20.0) -> tuple[int, str]:
    """快速跑一条命令拿全部输出（真实模式体检用）。

    与 subprocess.run(timeout=...) 的关键差别：超时后整树终止，
    否则 shell=True 的孙进程仍握着管道，communicate() 会永远等不到 EOF。
    """
    try:
        proc = subprocess.Popen(
            command, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=_child_env(),
        )
    except OSError as e:
        return 127, f"无法启动命令：{e}"
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode or 0, out or ""
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, _ = proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            out = ""
        return 124, (out or "") + f"\n（超时 {int(timeout)}s 已强制终止：{command}）"


def run_mock_streaming(
    command: str, mock_state: dict, on_output: OnLine, line_delay: float = 0.12
) -> tuple[int, str, str]:
    """模拟执行并逐行回放（加一点延迟让界面有"正在跑"的感觉）。"""
    code, out, err = mock_run(command, mock_state)
    text = out + (("\n" + err) if err.strip() else "")
    for line in text.splitlines():
        on_output(line)
        if line_delay > 0:
            time.sleep(line_delay)
    return code, out, err
