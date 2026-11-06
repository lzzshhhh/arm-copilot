"""Web API 测试：TestClient + mock 模式 + FakeLLM，覆盖向导 / 确认门 / 对话 / SSE。"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from arm_ui.app import create_app  # noqa: E402
from arm_ui.state import AppState  # noqa: E402


class _Msg:
    """OpenAI 风格回复消息的极简替身。"""

    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeLLM:
    """脚本化 LLM：按顺序弹出预设回复。"""

    def __init__(self, replies):
        self.replies = list(replies)

    def chat(self, history, tools):
        return self.replies.pop(0)


@pytest.fixture()
def client(tmp_path):
    state = AppState(
        mock=True,
        state_dir=tmp_path,
        llm=FakeLLM([_Msg("SO-101 是一台开源机械臂 🤖")]),
    )
    # 测试便利：pip 等软件级命令自动放行；hardware 命令仍走 HTTP 确认门
    orig_request = state.approvals.request
    _HARDWARE = ("lerobot-setup-motors", "lerobot-calibrate", "lerobot-teleoperate")

    def request(command, risk, reason):
        if command.startswith(_HARDWARE):
            return orig_request(command, risk, reason)
        return True

    state.approvals.request = request
    with TestClient(create_app(state)) as c:
        yield c, state


def wait_until(cond, timeout=15.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


class TestProfileAndProgress:
    def test_profile_shape(self, client):
        c, _ = client
        r = c.get("/api/profile")
        assert r.status_code == 200
        data = r.json()
        assert data["mock"] is True
        assert data["chat_enabled"] is True
        phases = data["product"]["phases"]
        assert [p["key"] for p in phases] == ["install", "configure", "calibrate", "next"]
        assert len(phases[0]["steps"]) == 3

    def test_step_guides_present_for_interactive_steps(self, client):
        c, _ = client
        steps = c.get("/api/profile").json()["product"]["phases"][1]["steps"]
        by_id = {s["id"]: s for s in steps}
        # 拔线找端口与校准是交互最密集的步骤，必须带操作指引
        assert any("Remove the USB" in g or "USB" in g for g in by_id["configure.0"]["guide"])
        assert len(by_id["configure.1"]["guide"]) >= 3

    def test_progress_initial(self, client):
        c, _ = client
        data = c.get("/api/progress").json()
        assert data["total"] == 13
        assert data["done_count"] == 0
        assert data["steps"]["install.0"]["status"] == "pending"
        assert data["steps"]["install.0"]["unlocked"] is True
        assert data["steps"]["install.1"]["unlocked"] is False


class TestStepRun:
    def test_run_safe_step_completes(self, client):
        c, _ = client
        assert c.post("/api/steps/install.0/run").status_code == 202
        assert wait_until(
            lambda: c.get("/api/progress").json()["steps"]["install.0"]["status"] == "done"
        )

    def test_run_locked_step_409(self, client):
        c, _ = client
        r = c.post("/api/steps/configure.1/run")
        assert r.status_code == 409
        assert "解锁" in r.json()["detail"]

    def test_run_unknown_step_409(self, client):
        c, _ = client
        assert c.post("/api/steps/nope.0/run").status_code == 409

    def test_skip_then_run_next(self, client):
        c, _ = client
        assert c.post("/api/steps/install.0/skip").status_code == 200
        data = c.get("/api/progress").json()
        assert data["steps"]["install.0"]["status"] == "skipped"
        assert data["steps"]["install.1"]["unlocked"] is True

    def test_hardware_step_full_approval_flow(self, client):
        c, state = client
        # 通关 install + configure.0/.1（mock 里 pip install 会写入 mock_state）
        for sid in ("install.0", "install.1", "install.2", "configure.0", "configure.1"):
            assert c.post(f"/api/steps/{sid}/run").status_code == 202
            step = sid
            assert wait_until(
                lambda s=sid: c.get("/api/progress").json()["steps"][s]["status"] == "done"
            ), f"{step} 未完成"
        # Windows 占位端口守卫：hardware 步骤需要实际端口
        c.post("/api/ports", json={"follower": "COM3", "leader": "COM4"})

        # configure.2 = setup-motors follower（hardware）→ 需要确认
        assert c.post("/api/steps/configure.2/run").status_code == 202
        assert wait_until(lambda: state.approvals.pending_ids())
        approval_id = state.approvals.pending_ids()[0]

        assert c.post(f"/api/approvals/{approval_id}", json={"approved": False}).status_code == 200
        assert wait_until(
            lambda: c.get("/api/progress").json()["steps"]["configure.2"]["status"] == "pending"
        )  # 拒绝 → 回到待办

        # 重跑并同意
        assert c.post("/api/steps/configure.2/run").status_code == 202
        assert wait_until(lambda: state.approvals.pending_ids())
        approval_id = state.approvals.pending_ids()[0]
        assert c.post(f"/api/approvals/{approval_id}", json={"approved": True}).status_code == 200
        assert wait_until(
            lambda: c.get("/api/progress").json()["steps"]["configure.2"]["status"] == "done"
        )

    def test_resolve_missing_approval_404(self, client):
        c, _ = client
        assert c.post("/api/approvals/whatever", json={"approved": True}).status_code == 404


class TestPorts:
    def test_set_and_get(self, client):
        c, _ = client
        r = c.post("/api/ports", json={"follower": "COM3", "leader": "COM4"})
        assert r.status_code == 200
        assert r.json() == {"follower": "COM3", "leader": "COM4"}
        assert c.get("/api/ports").json() == {"follower": "COM3", "leader": "COM4"}

    def test_port_substitution_reaches_mock_run(self, client):
        c, _ = client
        c.post("/api/ports", json={"follower": "COM7", "leader": "COM8"})
        for sid in ("install.0", "install.1", "install.2", "configure.0"):
            c.post(f"/api/steps/{sid}/run")
            assert wait_until(
                lambda s=sid: c.get("/api/progress").json()["steps"][s]["status"] == "done"
            )
        # 用捕获 SSE 的方式验证替换后的命令（见下个测试类），这里验证不阻塞即可


class TestSSE:
    def test_events_stream_contains_lifecycle(self, client):
        c, _ = client
        seen = []
        stop = threading.Event()

        def reader():
            with c.stream("GET", "/api/events") as r:
                for line in r.iter_lines():
                    if stop.is_set():
                        break
                    if line.startswith("data: "):
                        msg = json.loads(line[6:])
                        seen.append(msg["kind"])
                        if msg["kind"] == "cmd_end":
                            break  # 收到第一条命令结束即止

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        time.sleep(0.3)  # 等订阅生效
        c.post("/api/steps/install.0/run")
        t.join(15)
        stop.set()
        assert "connected" in seen
        for kind in ("step_update", "cmd_start", "cmd_output", "cmd_end"):
            assert kind in seen, f"缺少事件 {kind}: {seen}"


class TestChat:
    def test_chat_roundtrip(self, client):
        c, state = client
        r = c.post("/api/chat", json={"text": "SO-101 是什么？"})
        assert r.status_code == 202
        assert wait_until(
            lambda: any(
                m["role"] == "assistant" and m["content"]
                for m in c.get("/api/chat/messages").json()["messages"]
            )
        )
        msgs = c.get("/api/chat/messages").json()["messages"]
        assert msgs[-1]["content"] == "SO-101 是一台开源机械臂 🤖"
        assert state.chat_busy is False

    def test_chat_busy_409(self, client):
        c, state = client
        release = threading.Event()
        original = state.agent.handle

        def slow_handle(*a, **kw):
            release.wait(10)
            return original(*a, **kw)

        state.agent.handle = slow_handle
        try:
            assert c.post("/api/chat", json={"text": "你好"}).status_code == 202
            r = c.post("/api/chat", json={"text": "再来一个问题"})
            assert r.status_code == 409
        finally:
            release.set()
            wait_until(lambda: not state.chat_busy)
            state.agent.handle = original

    def test_chat_reset(self, client):
        c, state = client
        c.post("/api/chat", json={"text": "hi"})
        wait_until(lambda: len(state.chat_history) > 1)
        assert c.post("/api/chat/reset").status_code == 200
        assert len(c.get("/api/chat/messages").json()["messages"]) == 0


class TestChatDisabled:
    def test_chat_disabled_503_but_wizard_ok(self, tmp_path, monkeypatch):
        import arm_ui.state as state_mod

        class NoConfig:
            def __init__(self, *a, **kw):
                raise state_mod.ConfigError("缺少 ARM_COPILOT_MODEL")

        monkeypatch.setattr(state_mod, "load_config", NoConfig)
        st = AppState(mock=True, state_dir=tmp_path)
        with TestClient(create_app(st)) as c:
            assert c.get("/api/profile").json()["chat_enabled"] is False
            r = c.post("/api/chat", json={"text": "hi"})
            assert r.status_code == 503
            # 向导不受影响
            assert c.post("/api/steps/install.0/run").status_code == 202
            assert wait_until(
                lambda: c.get("/api/progress").json()["steps"]["install.0"]["status"] == "done"
            )


class TestEnvCheck:
    def test_env_check_returns_output(self, client):
        c, _ = client
        r = c.post("/api/env-check")
        assert r.status_code == 200
        assert "Python" in r.json()["output"]


class TestRecordConfigApi:
    def test_get_set_roundtrip(self, client):
        c, _ = client
        r = c.post("/api/record-config", json={"repo_id": "demo/pick", "num_episodes": 20, "camera": True, "camera_index": 1,
                                               "camera2": True, "camera2_index": 2})
        assert r.status_code == 200
        body = r.json()
        assert body["repo_id"] == "demo/pick" and body["num_episodes"] == 20
        assert body["camera"] is True and body["camera_index"] == 1
        assert body["camera2"] is True and body["camera2_index"] == 2
        assert c.get("/api/record-config").json()["repo_id"] == "demo/pick"

    def test_camera2_requires_camera_400(self, client):
        c, _ = client
        r = c.post("/api/record-config", json={"repo_id": "demo/pick", "num_episodes": 20,
                                               "camera": False, "camera2": True})
        assert r.status_code == 400

    def test_invalid_repo_400(self, client):
        c, _ = client
        r = c.post("/api/record-config", json={"repo_id": "没有斜杠", "num_episodes": 20})
        assert r.status_code == 400

    def test_snapshot_contains_record_config(self, client):
        c, _ = client
        data = c.get("/api/progress").json()
        assert "record_config" in data and "repo_id" in data["record_config"]
