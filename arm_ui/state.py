"""Web 应用全局状态组装：向导、确认门、对话共享同一份环境。

要点：
- 向导不依赖 LLM —— 配置缺失时只禁用对话页，并给出友好提示；
- mock_state 在向导与对话（run_command 工具）之间共享：
  向导里 pip install 装好的 lerobot，对话里 env_check 也能看到；
- 同一时刻只允许一个步骤 / 一轮对话在跑（单用户本机场景）。
"""

from __future__ import annotations

import platform
import sys
import threading
from pathlib import Path

from arm_copilot.agent.llm import LLMClient
from arm_copilot.agent.loop import Agent
from arm_copilot.agent.prompts import SYSTEM_PROMPT
from arm_copilot.config import ConfigError, load_config
from arm_copilot.products.base import PRODUCTS
from arm_copilot.tools import ToolContext, build_registry
from arm_copilot.tools.diagnostics import make_env_check_tool

from arm_ui.approvals import ApprovalBroker
from arm_ui.events import EventBus
from arm_ui.executor import run_quick
from arm_ui.flow import FlowEngine, FlowError


_cuda_cache: dict = {}


def _cuda_available() -> bool:
    """探测 CUDA 版 torch；结果缓存（import torch 很重，只在后台预热时执行一次）。"""
    if "ok" not in _cuda_cache:
        try:
            import torch

            _cuda_cache["ok"] = bool(torch.cuda.is_available())
        except Exception:
            _cuda_cache["ok"] = False
    return _cuda_cache["ok"]


def _cuda_cached() -> bool | None:
    """只读缓存：未预热完成时返回 None，绝不在请求路径里触发 import。"""
    return _cuda_cache.get("ok")


def warm_cuda_cache_async() -> None:
    """服务启动后后台预热（torch 导入耗时且占 GIL，放请求里会把服务卡死）。"""
    def _warm():
        _cuda_available()

    threading.Timer(2.0, _warm).start()


class BusyError(Exception):
    """已有任务在执行。"""


class ChatDisabledError(Exception):
    """对话功能未启用（缺少 LLM 配置）。"""


class AppState:
    def __init__(
        self,
        mock: bool = False,
        state_dir: Path | None = None,
        llm: LLMClient | None = None,
        default_product: str = "so101",
    ) -> None:
        self.mock = mock
        self.state_dir = Path(state_dir) if state_dir else Path(__file__).resolve().parent / "state"

        self.bus = EventBus()
        self.approvals = ApprovalBroker(self.bus)
        warm_cuda_cache_async()
        self.mock_state: dict = {}

        self.tool_ctx = ToolContext(
            mock=mock,
            approve=self.approvals.as_approve_callback(),
            mock_state=self.mock_state,
        )

        if default_product not in PRODUCTS:
            raise ValueError(f"未知产品档案: {default_product}")
        self.flow = FlowEngine(
            PRODUCTS[default_product],
            mock=mock,
            approve=self.approvals.as_approve_callback(),
            bus=self.bus,
            mock_state=self.mock_state,
            state_path=self.state_dir / "progress.json",
        )

        # ---- 对话（可选） ----
        self.chat_error: str | None = None
        if llm is not None:
            registry = build_registry(self.tool_ctx)
            self._patch_env_check_tool(registry)
            self.agent = Agent(llm, registry)
        else:
            try:
                config = load_config(mock=mock)
                registry = build_registry(self.tool_ctx)
                self._patch_env_check_tool(registry)
                self.agent = Agent(
                    LLMClient(config), registry, max_steps=config.max_tool_steps
                )
            except ConfigError as e:
                self.agent = None
                self.chat_error = f"对话功能未启用（{e}）。配置好 .env 后重启即可；配置向导不受影响。"

        self.chat_history: list[dict] = (
            [{"role": "system", "content": SYSTEM_PROMPT}] if self.agent else []
        )
        self.chat_busy = False
        self._chat_lock = threading.Lock()

    # ---------------- 向导 ----------------

    def start_step(self, step_id: str) -> None:
        """同步预检 + 后台线程执行；预检失败抛 FlowError。"""
        self.flow.check_can_run(step_id)
        threading.Thread(
            target=self._step_worker, args=(step_id,), daemon=True, name=f"step-{step_id}"
        ).start()

    def _step_worker(self, step_id: str) -> None:
        try:
            self.flow.run_step(step_id)
        except FlowError as e:
            self.bus.publish("error", {"step": step_id, "message": str(e)})

    def env_check(self) -> str:
        """环境体检。mock 复用原工具；真实模式用非交互实现。

        原实现在 lerobot 已安装时会跑交互式 lerobot-find-port（等按键），
        Web 场景无人应答 → 超时只杀 cmd 外壳、孙进程占着管道 → 永久挂死。
        这里串口改用 pyserial 自带的 list_ports（非交互、秒回）。
        """
        if self.mock:
            return make_env_check_tool(self.tool_ctx).handler({})

        lines = ["环境体检结果："]

        # GPU：有 N 卡 + CUDA 版 torch 才能 GPU 训练（PyPI 的 Windows torch 默认 CPU 版）
        code_gpu, out_gpu = run_quick("nvidia-smi --query-gpu=name --format=csv,noheader", timeout=10)
        gpu_name = out_gpu.strip().splitlines()[0].strip() if code_gpu == 0 and out_gpu.strip() else ""
        if gpu_name:
            cuda_ok = _cuda_available()
            if cuda_ok:
                lines.append(f"- GPU: {gpu_name} ✓ CUDA 版 PyTorch 已就绪，可 GPU 训练")
            else:
                lines.append(f"- GPU: {gpu_name}（当前 torch 是 CPU 版；GPU 训练需另装 CUDA 版 PyTorch，约 2.5GB）")
        else:
            lines.append("- GPU: 未检测到 NVIDIA 显卡（CPU 训练对小数据集足够）")

        py = platform.python_version()
        if sys.version_info >= (3, 12):
            lines.append(f"- Python: {py} ✓（满足 LeRobot 要求 ≥3.12）")
        else:
            lines.append(f"- Python: {py} ⚠ 建议升级到 3.12+（LeRobot 要求）")

        code_pip, out_pip = run_quick("pip --version")
        toks = out_pip.split()
        pip = toks[1] if code_pip == 0 and len(toks) > 1 else "未知"
        lines.append(f"- pip: {pip}")

        code_lr, out_lr = run_quick("pip show lerobot")
        if code_lr == 0 and "Version:" in out_lr:
            ver = next(
                (l.split()[-1] for l in out_lr.splitlines() if l.startswith("Version:")),
                "未知",
            )
            lines.append(f"- LeRobot: {ver} ✓ 已安装")
            code_port, out_port = run_quick("python -m serial.tools.list_ports -v")
            ports = (out_port or "").strip()
            if code_port == 0 and ports:
                lines.append(f"- 串口设备：\n{ports}")
                lines.append("  （想知道哪个是机械臂？用向导第二关的 find-port 拔线识别最可靠）")
            else:
                lines.append("- 串口设备：未检测到（确认机械臂已供电、USB 已连接后重试）")
        else:
            lines.append('- LeRobot: 未安装 ✗ → 需要先执行：pip install "lerobot[feetech]"')

        return "\n".join(lines)

    def _patch_env_check_tool(self, registry) -> None:
        """真实模式下把对话 agent 的 env_check 工具换成非交互实现（同上理由）。"""
        if self.mock:
            return
        from arm_copilot.tools.base import Tool

        original = make_env_check_tool(self.tool_ctx)
        # ToolRegistry 只有 register（查重）没有替换；这里直达内部表，
        # 属 arm_ui 对 arm_copilot 的只读复用之外的唯一一处受控写访问。
        registry._tools["env_check"] = Tool(
            name="env_check",
            description=original.description,
            parameters=original.parameters,
            risk=original.risk,
            handler=lambda args: self.env_check(),
        )

    # ---------------- 对话 ----------------

    def start_chat(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        if self.agent is None:
            raise ChatDisabledError(self.chat_error or "对话功能未启用。")
        with self._chat_lock:
            if self.chat_busy:
                raise BusyError("臂管家正在回答上一个问题，请稍等一下～")
            self.chat_busy = True
        threading.Thread(target=self._chat_worker, args=(text,), daemon=True, name="chat").start()

    def _chat_worker(self, text: str) -> None:
        def on_event(kind: str, data: dict) -> None:
            self.bus.publish(f"chat_{kind}", data)

        try:
            reply = self.agent.handle(text, self.chat_history, on_event)
            self.bus.publish("chat_reply", {"text": reply})
        except Exception as e:  # 网络 / API 错误：回给界面，不崩线程
            self.bus.publish(
                "chat_error", {"message": f"调用模型失败：{type(e).__name__}: {e}"}
            )
        finally:
            with self._chat_lock:
                self.chat_busy = False

    def reset_chat(self) -> None:
        with self._chat_lock:
            if self.chat_busy:
                raise BusyError("臂管家正在回答，稍后再清空～")
            if self.agent is not None:
                self.chat_history = [{"role": "system", "content": SYSTEM_PROMPT}]

    def chat_messages(self) -> list[dict]:
        return [
            {"role": m["role"], "content": m.get("content", "")}
            for m in self.chat_history
            if m.get("role") in ("user", "assistant")
        ]
