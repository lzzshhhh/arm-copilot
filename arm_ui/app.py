"""臂管家 Web UI：FastAPI 应用与启动入口。

用法（在仓库根目录）：
    python arm_ui/app.py --mock     # 模拟模式，无硬件演示
    python -m arm_ui --mock
    python arm_ui/app.py            # 真实模式（会真装机 / 驱动电机，注意安全）
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import webbrowser
from pathlib import Path

# 允许未 pip install -e 的源码树直接运行（arm_copilot 在仓库根目录）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from arm_copilot import __version__
from arm_ui import servo_tool
from arm_ui.events import EventBus
from arm_ui.flow import FlowError
from arm_ui.state import AppState, BusyError, ChatDisabledError

_STATIC_DIR = Path(__file__).resolve().parent / "static"


class PortsBody(BaseModel):
    follower: str = ""
    leader: str = ""


class ApprovalBody(BaseModel):
    approved: bool


class InputBody(BaseModel):
    text: str = "\n"
    streaming: bool = False  # 命令正在刷输出（校准录制）→ 控制台注入回车


class ServoIdBody(BaseModel):
    port: str
    new_id: int


class RecordConfigBody(BaseModel):
    repo_id: str = ""
    num_episodes: int = 50
    camera: bool = False
    camera_index: int = 0


class TrainConfigBody(BaseModel):
    steps: int = 3000
    batch_size: int = 16
    device: str = "cpu"


class ChatBody(BaseModel):
    text: str


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="臂管家 arm-copilot UI", version=__version__)
    app.state.app_state = state

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    # 静态资源不声明缓存策略时，浏览器会自作主张用旧缓存（改了 app.js
    # 用户却跑着旧校验逻辑）。no-cache = 每次携带 ETag 重新校验，文件
    # 没变时 304 几乎零开销，变了立即拿新的。
    @app.middleware("http")
    async def _static_no_cache(request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # ---------------- 页面 ----------------

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(
            _STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
        )

    # ---------------- 向导 ----------------

    @app.get("/api/profile")
    def profile() -> dict:
        return {
            "product": state.flow.profile_payload(),
            "version": __version__,
            "mock": state.mock,
            "chat_enabled": state.agent is not None,
            "chat_error": state.chat_error,
        }

    @app.get("/api/progress")
    def progress() -> dict:
        return state.flow.snapshot()

    @app.post("/api/progress/reset")
    def reset_progress() -> dict:
        state.flow.reset()
        return state.flow.snapshot()

    @app.post("/api/steps/{step_id}/run", status_code=202)
    def run_step(step_id: str) -> dict:
        try:
            state.start_step(step_id)
        except FlowError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"started": step_id}

    @app.post("/api/steps/{step_id}/skip")
    def skip_step(step_id: str) -> dict:
        try:
            state.flow.skip_step(step_id)
        except FlowError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return state.flow.snapshot()

    @app.get("/api/ports")
    def get_ports() -> dict:
        return state.flow.ports

    @app.post("/api/ports")
    def set_ports(body: PortsBody) -> dict:
        return state.flow.set_ports(body.follower, body.leader)

    @app.get("/api/record-config")
    def get_record_config() -> dict:
        return state.flow.record_config

    @app.post("/api/record-config")
    def set_record_config(body: RecordConfigBody) -> dict:
        try:
            return state.flow.set_record_config(
                body.repo_id, body.num_episodes, camera=body.camera, camera_index=body.camera_index
            )
        except FlowError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/api/train-config")
    def get_train_config() -> dict:
        from arm_ui.state import _cuda_cached

        # 只读缓存：预热未完成时为 null（前端显示"检测中"），绝不在请求里 import torch
        return {"config": state.flow.train_config, "cuda_available": _cuda_cached()}

    @app.post("/api/train-config")
    def set_train_config(body: TrainConfigBody) -> dict:
        try:
            return state.flow.set_train_config(body.steps, body.batch_size, body.device)
        except FlowError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/env-check")
    def env_check() -> dict:
        return {"output": state.env_check()}

    # ---------------- 确认门 ----------------

    @app.get("/api/approvals")
    def list_approvals() -> dict:
        return {"ids": state.approvals.pending_ids()}

    # ---------------- 舵机 ID 工具（修复被改乱的编号） ----------------

    def _servo_guard() -> None:
        """串口同一时刻只能被一个使用者打开：有步骤在跑就拒绝。"""
        if state.flow.running_step is not None:
            raise HTTPException(status_code=409, detail="有步骤正在运行，等它结束后再用舵机工具。")

    @app.get("/api/servo/scan")
    def servo_scan(port: str) -> dict:
        _servo_guard()
        try:
            return servo_tool.scan(port.strip())
        except Exception as e:  # 兜底：任何意外都给用户看得懂的错误
            return {"ok": False, "reason": f"扫描异常：{type(e).__name__}: {e}"}

    @app.post("/api/servo/set-id")
    def servo_set_id(body: ServoIdBody) -> dict:
        _servo_guard()
        try:
            result = servo_tool.set_id(body.port.strip(), body.new_id)
        except Exception as e:
            result = {"ok": False, "reason": f"写入异常：{type(e).__name__}: {e}"}
        state.bus.publish("servo_id_set", {
            "port": body.port, "result": result,
        })
        return result

    # ---------------- 交互命令（真实模式"按回车"提示 / 主动停止） ----------------

    @app.post("/api/interactive/input")
    def interactive_input(body: InputBody) -> dict:
        if not state.flow.send_input(body.text, streaming=body.streaming):
            raise HTTPException(
                status_code=409,
                detail="当前没有等待输入的命令（可能已结束，或处于模拟模式）。",
            )
        return {"ok": True}

    @app.post("/api/interactive/stop")
    def interactive_stop() -> dict:
        if not state.flow.stop_current():
            raise HTTPException(status_code=409, detail="当前没有可停止的命令。")
        return {"ok": True}

    @app.post("/api/approvals/{approval_id}")
    def resolve_approval(approval_id: str, body: ApprovalBody) -> dict:
        if not state.approvals.resolve(approval_id, body.approved):
            raise HTTPException(status_code=404, detail="该确认请求不存在或已处理。")
        return {"resolved": approval_id, "approved": body.approved}

    # ---------------- 对话 ----------------

    @app.post("/api/chat", status_code=202)
    def chat(body: ChatBody) -> dict:
        try:
            state.start_chat(body.text)
        except ChatDisabledError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except BusyError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"ok": True}

    @app.get("/api/chat/messages")
    def chat_messages() -> dict:
        return {
            "messages": state.chat_messages(),
            "busy": state.chat_busy,
            "enabled": state.agent is not None,
            "error": state.chat_error,
        }

    @app.post("/api/chat/reset")
    def chat_reset() -> dict:
        try:
            state.reset_chat()
        except BusyError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"ok": True}

    # ---------------- SSE 事件流 ----------------

    @app.get("/api/events")
    def events() -> StreamingResponse:
        return StreamingResponse(
            event_stream(state),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def event_stream(state: AppState):
    """同步生成器：Starlette 会放到线程池里迭代，阻塞读队列是安全的。"""
    q = state.bus.subscribe()
    try:
        while True:
            try:
                msg = q.get(timeout=15)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            yield EventBus.encode(msg)
    finally:
        state.bus.unsubscribe(q)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arm-copilot-ui", description="臂管家 Web 界面")
    parser.add_argument("--mock", action="store_true", help="模拟执行所有命令（无硬件演示 / 测试）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，仅本机）")
    parser.add_argument("--port", type=int, default=8000, help="端口（默认 8000）")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--state-dir", default=None, help="进度文件目录（默认 arm_ui/state）")
    args = parser.parse_args(argv)

    import uvicorn

    state = AppState(
        mock=args.mock,
        state_dir=Path(args.state_dir) if args.state_dir else None,
    )
    app = create_app(state)
    url = f"http://{args.host}:{args.port}"

    print()
    print("=" * 52)
    print("  🤖 臂管家 arm-copilot · 配置小助手")
    print(f"  版本 {__version__}    地址 {url}")
    if state.mock:
        print("  🎭 模拟模式：所有命令仅模拟执行，不会碰真实硬件")
    else:
        print("  ⚠️  真实模式：安装/电机命令会在本机真实执行")
    if state.agent is None:
        print("  💬 对话功能未启用（缺少模型配置），向导可正常使用")
    print("=" * 52)
    print()

    if not args.no_open:
        threading.Timer(1.5, webbrowser.open, [url]).start()

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
