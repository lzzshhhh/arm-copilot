"""确认门桥接：向导 / 对话里的工作线程阻塞等待，HTTP 端点负责答复。

复用 arm_copilot.tools.base.ToolContext.approve 的回调签名
(command, risk, reason) -> bool，因此可直接注入 run_command 工具与流程引擎。
"""

from __future__ import annotations

import threading
import uuid

from .events import EventBus

DEFAULT_TIMEOUT = 600.0  # 10 分钟无人回应 → 自动拒绝（fail-closed）


class ApprovalBroker:
    def __init__(self, bus: EventBus, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._bus = bus
        self._timeout = timeout
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}

    def as_approve_callback(self):
        """返回 ToolContext.approve 兼容的回调。"""

        def approve(command: str, risk: str, reason: str) -> bool:
            return self.request(command, risk, reason)

        return approve

    def request(self, command: str, risk: str, reason: str) -> bool:
        """在工作线程中调用：广播确认请求并阻塞等待答复。"""
        approval_id = uuid.uuid4().hex[:12]
        item = {"event": threading.Event(), "approved": False}
        with self._lock:
            self._pending[approval_id] = item
        self._bus.publish(
            "approval_required",
            {"id": approval_id, "command": command, "risk": risk, "reason": reason},
        )
        answered = item["event"].wait(self._timeout)
        with self._lock:
            self._pending.pop(approval_id, None)
        approved = answered and item["approved"]
        self._bus.publish(
            "approval_resolved",
            {"id": approval_id, "approved": approved, "timeout": not answered},
        )
        return approved

    def resolve(self, approval_id: str, approved: bool) -> bool:
        """由 HTTP 端点调用；返回 False 表示该确认不存在或已被解决。"""
        with self._lock:
            item = self._pending.get(approval_id)
        if item is None:
            return False
        item["approved"] = bool(approved)
        item["event"].set()
        return True

    def pending_ids(self) -> list[str]:
        with self._lock:
            return list(self._pending)
