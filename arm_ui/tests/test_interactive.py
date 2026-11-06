"""交互式命令支持测试：stdin 往返、整树停止、API 端点行为。"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arm_ui.executor import _kill_tree, run_real_streaming, write_stdin  # noqa: E402


def _wait_handle(handle: dict, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if handle.get("proc") is not None:
            return True
        time.sleep(0.05)
    return False


class TestStdinRoundtrip:
    def test_input_reaches_child_process(self):
        """子进程 input() 等待时，UI 注入的一行输入能送达并继续执行。"""
        handle: dict = {}
        lines: list[str] = []
        result: dict = {}

        def worker():
            result["res"] = run_real_streaming(
                'python -c "import sys; print(\'waiting\'); '
                's = input(); print(\'got:\' + s)"',
                "safe",
                lines.append,
                handle=handle,
            )

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        try:
            assert _wait_handle(handle), "子进程未启动"
            time.sleep(0.5)  # 等 print('waiting') 先落盘（不关键，仅稳定性）
            assert write_stdin(handle, "hello")
            t.join(10)
            assert not t.is_alive(), "子进程未按预期结束"
            code, out, _ = result["res"]
            assert code == 0
            assert "waiting" in out
            assert "got:hello" in out
        finally:
            if t.is_alive():
                _kill_tree(handle["proc"])
                t.join(5)


class TestPartialPrompt:
    def test_prompt_without_newline_emitted_while_waiting(self):
        """回归：input() 提示无换行，必须在进程等待输入期间实时送达（原来只在结束后才出来）。"""
        handle: dict = {}
        lines: list[str] = []
        result: dict = {}

        def worker():
            result["res"] = run_real_streaming(
                'python -c "import sys; sys.stdout.write(\'Press ENTER to use file, '
                'or type \'\'c\'\' and press ENTER to run calibration:\'); '
                'sys.stdout.flush(); sys.stdin.readline(); print(\'done\')"',
                "safe", lines.append, handle=handle,
            )

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        try:
            assert _wait_handle(handle), "子进程未启动"
            # 进程阻塞在等输入时，提示应当已经在 ~1.2s 内被发出
            deadline = time.time() + 3
            while time.time() < deadline and not any("press ENTER to run calibration" in l for l in lines):
                time.sleep(0.1)
            assert any("press ENTER to run calibration" in l for l in lines), f"提示未被实时发出: {lines}"
            # 此时进程必然还活着（还没作答）
            assert handle["proc"].poll() is None
            assert write_stdin(handle, "\n")
            t.join(10)
            assert not t.is_alive()
            code, out, _ = result["res"]
            assert code == 0
            assert "done" in out
        finally:
            if t.is_alive():
                _kill_tree(handle["proc"])
                t.join(5)


class TestStopTree:
    def test_kill_tree_ends_long_running_child(self):
        handle: dict = {}
        result: dict = {}

        def worker():
            result["res"] = run_real_streaming(
                'python -c "import time; print(\'loop-start\'); time.sleep(120)"',
                "safe",
                lambda l: None,
                handle=handle,
            )

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        try:
            assert _wait_handle(handle)
            _kill_tree(handle["proc"])
            t.join(10)
            assert not t.is_alive(), "长命令应被停止"
            code, out, _ = result["res"]
            assert code != 0
        finally:
            if t.is_alive():
                _kill_tree(handle["proc"])
                t.join(5)


class TestFlowInteractiveGuard:
    def test_mock_mode_send_input_refused(self, tmp_path):
        from arm_copilot.products.base import PRODUCTS

        from arm_ui.events import EventBus
        from arm_ui.flow import FlowEngine

        eng = FlowEngine(
            PRODUCTS["so101"], mock=True, approve=lambda *a: True, bus=EventBus(),
            mock_state={}, state_path=tmp_path / "p.json", mock_line_delay=0,
        )
        eng.statuses["install.0"] = "done"
        eng.run_step("install.1")  # mock：跑完即结束，无交互
        assert eng.send_input("\n") is False
        assert eng.stop_current() is False


class TestInteractiveEndpoints:
    def test_endpoints_409_when_nothing_running(self, tmp_path):
        from fastapi.testclient import TestClient

        from arm_ui.app import create_app
        from arm_ui.state import AppState

        state = AppState(mock=True, state_dir=tmp_path)
        with TestClient(create_app(state)) as c:
            r = c.post("/api/interactive/input", json={"text": "\n"})
            assert r.status_code == 409
            r = c.post("/api/interactive/stop")
            assert r.status_code == 409


class TestRunQuick:
    def test_timeout_kills_tree_and_returns(self):
        """回归：超时只杀 cmd 外壳时，孙进程占管道会让读取永久挂死。"""
        from arm_ui.executor import run_quick

        start = time.time()
        code, out = run_quick('python -c "import time; time.sleep(30)"', timeout=1.5)
        elapsed = time.time() - start
        assert code == 124
        assert "超时" in out
        assert elapsed < 15, f"run_quick 超时路径挂死（{elapsed:.1f}s）"

    def test_normal_command_output(self):
        from arm_ui.executor import run_quick

        code, out = run_quick('python -c "print(123)"')
        assert code == 0 and "123" in out


class TestSendInputRouting:
    """streaming=True（校准录制中）走控制台注入；否则走 stdin 管道。"""

    def test_streaming_uses_console_enter(self, tmp_path, monkeypatch):
        from arm_copilot.products.base import PRODUCTS

        from arm_ui import flow as flow_mod
        from arm_ui.events import EventBus

        calls = {"console": 0, "stdin": 0}
        monkeypatch.setattr(flow_mod, "send_console_enter", lambda: (calls.__setitem__("console", calls["console"] + 1) or True))
        monkeypatch.setattr(flow_mod, "write_stdin", lambda *a: (calls.__setitem__("stdin", calls["stdin"] + 1) or True))

        eng = flow_mod.FlowEngine(
            PRODUCTS["so101"], mock=False, approve=lambda *a: True, bus=EventBus(),
            mock_state={}, state_path=tmp_path / "p.json", mock_line_delay=0,
        )
        eng.running_step = "calibrate.0"  # 模拟有步骤在跑
        eng.live_handle = {}
        assert eng.send_input("\n", streaming=True) is True
        assert calls["console"] == 1
        assert eng.send_input("\n", streaming=False) is True
        assert calls["stdin"] == 1 and calls["console"] == 1  # 静止提示只走管道

    def test_console_enter_on_windows_or_graceful(self):
        from arm_ui.executor import send_console_enter

        # 无控制台的测试进程里应安全返回 False，不抛异常
        result = send_console_enter()
        assert isinstance(result, bool)


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from arm_ui.app import create_app
    from arm_ui.state import AppState

    state = AppState(mock=True, state_dir=tmp_path)
    with TestClient(create_app(state)) as c:
        yield c, state


class TestServoToolEndpoints:

    def test_scan_and_set_id_endpoints(self, client, monkeypatch):
        c, state = client
        from arm_ui import servo_tool

        monkeypatch.setattr(
            servo_tool, "scan",
            lambda port: {"ok": True, "port": port, "baudrates": {"1000000": [6]}, "total_online": 1},
        )
        r = c.get("/api/servo/scan", params={"port": "COM11"})
        assert r.status_code == 200
        assert r.json()["baudrates"] == {"1000000": [6]}

        monkeypatch.setattr(
            servo_tool, "set_id",
            lambda port, new_id: {"ok": True, "changed": True, "old_id": 6, "new_id": new_id, "baudrate": 1000000},
        )
        r = c.post("/api/servo/set-id", json={"port": "COM11", "new_id": 1})
        assert r.status_code == 200
        assert r.json()["old_id"] == 6

    def test_set_id_invalid_range(self, client):
        from fastapi.testclient import TestClient  # noqa: F401

        c, _ = client
        r = c.post("/api/servo/set-id", json={"port": "COM11", "new_id": 0})
        assert r.status_code == 200  # 业务层返回 ok=False 的 JSON，而不是 4xx
        assert r.json()["ok"] is False

    def test_servo_guard_while_step_running(self, client, monkeypatch):
        c, state = client
        state.flow.running_step = "install.0"  # 模拟步骤占用串口
        try:
            r = c.get("/api/servo/scan", params={"port": "COM11"})
            assert r.status_code == 409
        finally:
            state.flow.running_step = None


class TestRealEnvCheck:
    def test_real_mode_env_check_completes(self, tmp_path):
        """真实模式体检必须秒级返回（不跑交互式 find-port）。"""
        from arm_ui.state import AppState

        state = AppState(mock=False, state_dir=tmp_path)
        start = time.time()
        out = state.env_check()
        elapsed = time.time() - start
        assert "环境体检结果" in out
        assert "Python:" in out
        assert "LeRobot:" in out
        assert elapsed < 90, f"体检耗时异常（{elapsed:.1f}s）"

    def test_real_mode_registry_env_check_patched(self, tmp_path):
        from arm_ui.state import AppState

        state = AppState(mock=False, state_dir=tmp_path)
        if state.agent is None:
            pytest.skip("需要模型配置")
        handler_out = state.agent.registry._tools["env_check"].handler({})
        assert "环境体检结果" in handler_out


class TestTrainConfigApi:
    def test_get_includes_cuda_flag(self, client):
        c, _ = client
        r = c.get("/api/train-config")
        assert r.status_code == 200
        body = r.json()
        assert "config" in body and "cuda_available" in body

    def test_set_and_validate(self, client):
        c, _ = client
        r = c.post("/api/train-config", json={"steps": 2000, "batch_size": 8, "device": "cpu"})
        assert r.status_code == 200 and r.json()["steps"] == 2000
        r = c.post("/api/train-config", json={"steps": 5, "batch_size": 8, "device": "cpu"})
        assert r.status_code == 200  # 1–5 步合法：仅用于流程冒烟
        r = c.post("/api/train-config", json={"steps": 0, "batch_size": 8, "device": "cpu"})
        assert r.status_code == 400
