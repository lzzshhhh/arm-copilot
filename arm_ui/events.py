"""进程内事件总线：工作线程发布事件，SSE 订阅者各持一个队列。"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any


class EventBus:
    """多生产者多订阅者；每条消息会投递给当时在线的全部订阅者。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        # 订阅即回包，客户端据此确认链路已通（也方便测试确定性读取）
        q.put({"kind": "connected", "data": {}, "ts": time.time()})
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def publish(self, kind: str, data: dict | None = None) -> None:
        msg = {"kind": kind, "data": data or {}, "ts": time.time()}
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            q.put(msg)

    @staticmethod
    def encode(msg: dict[str, Any]) -> str:
        """编码为一条 SSE data 帧。"""
        return f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
