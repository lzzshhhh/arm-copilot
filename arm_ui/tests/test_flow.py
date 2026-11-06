"""流程引擎测试：状态流转、端口替换、持久化、确认门、闯关解锁。"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

# 让未安装源码树的测试也能找到 arm_copilot / arm_ui
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arm_copilot.products.base import PRODUCTS  # noqa: E402

from arm_ui.events import EventBus  # noqa: E402
from arm_ui.flow import (  # noqa: E402
    FlowEngine,
    FlowError,
    ST_DONE,
    ST_FAILED,
    ST_PENDING,
    ST_SKIPPED,
    parse_ports,
)


def make_engine(tmp_path: Path, approve=lambda *a: True, mock_state=None) -> FlowEngine:
    bus = EventBus()
    return FlowEngine(
        PRODUCTS["so101"],
        mock=True,
        approve=approve,
        bus=bus,
        mock_state=mock_state if mock_state is not None else {},
        state_path=tmp_path / "progress.json",
        mock_line_delay=0,  # 测试不等逐行回放
    )


class TestBasics:
    def test_flatten_13_steps_in_order(self, tmp_path):
        eng = make_engine(tmp_path)
        assert len(eng.order) == 13
        assert eng.order[0] == "install.0"
        assert eng.order[-1] == "next.2"
        # arm_ui 注入的 leader 端口识别步骤位于 configure.1
        assert "leader" in eng.steps["configure.1"].title
        assert eng.steps["configure.1"].commands == ["lerobot-find-port"]

    def test_all_engine_commands_pass_whitelist(self, tmp_path):
        from arm_copilot.tools.shell import validate

        eng = make_engine(tmp_path)
        for sid, step in eng.steps.items():
            for c in step.commands:
                ok, why = validate(c)
                assert ok, f"{sid} 的命令未过白名单：{c}（{why}）"

    def test_locked_until_previous_finished(self, tmp_path):
        eng = make_engine(tmp_path)
        assert eng.unlocked("install.0")
        assert not eng.unlocked("install.1")
        assert not eng.unlocked("configure.0")
        eng.statuses["install.0"] = ST_DONE
        assert eng.unlocked("install.1")
        assert not eng.unlocked("install.2")
        # 跳过也算通关
        eng.statuses["install.1"] = ST_SKIPPED
        assert eng.unlocked("install.2")

    def test_run_unknown_step_raises(self, tmp_path):
        eng = make_engine(tmp_path)
        with pytest.raises(FlowError):
            eng.check_can_run("nope.9")

    def test_run_locked_step_raises(self, tmp_path):
        eng = make_engine(tmp_path)
        with pytest.raises(FlowError):
            eng.check_can_run("calibrate.0")


class TestRunStep:
    def test_safe_step_becomes_done(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.run_step("install.0")  # python --version
        assert eng.statuses["install.0"] == ST_DONE

    def test_failed_command_marks_failed(self, tmp_path):
        eng = make_engine(tmp_path, mock_state={})  # lerobot 未安装
        eng.run_step("install.0")
        eng.statuses["install.1"] = ST_SKIPPED
        eng.statuses["install.2"] = ST_SKIPPED
        eng.run_step("configure.0")  # find-port → 127
        assert eng.statuses["configure.0"] == ST_FAILED

    def test_approval_denied_returns_pending(self, tmp_path):
        eng = make_engine(tmp_path, approve=lambda *a: False)
        eng.statuses["install.0"] = ST_DONE  # 解锁 install.1
        # install.1 是 pip install（risky）→ 拒绝后整步回到待办
        eng.run_step("install.1")
        assert eng.statuses["install.1"] == ST_PENDING

    def test_hardware_step_requires_approval(self, tmp_path):
        calls = []

        def approve(command, risk, reason):
            calls.append((command, risk))
            return True

        eng = make_engine(tmp_path, approve=approve)
        for sid in ("install.0", "install.1", "install.2", "configure.0", "configure.1"):
            eng.statuses[sid] = ST_DONE
        eng.set_ports("COM3", "COM4")  # Windows 占位端口守卫需要实际端口
        eng.run_step("configure.2")  # setup-motors follower（hardware）
        assert eng.statuses["configure.2"] == ST_DONE
        assert calls and calls[0][1] == "hardware"

    def test_windows_missing_ports_blocked_early(self, tmp_path, monkeypatch):
        """Windows + 端口未设置 → 命令不执行，直接友好失败并落日志。"""
        monkeypatch.setattr("arm_ui.flow.platform.system", lambda: "Windows")
        eng = make_engine(tmp_path)
        for sid in ("install.0", "install.1", "install.2", "configure.0", "configure.1"):
            eng.statuses[sid] = ST_DONE
        eng.run_step("configure.2")
        assert eng.statuses["configure.2"] == ST_FAILED
        content = eng.run_log_path.read_text(encoding="utf-8")
        assert "端口" in content

    def test_run_log_written_after_step(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.run_step("install.0")
        assert eng.run_log_path.exists()
        content = eng.run_log_path.read_text(encoding="utf-8")
        assert "python --version" in content
        assert "退出码 0" in content

    def test_run_step_updates_mock_state_shared(self, tmp_path):
        shared = {}
        eng = make_engine(tmp_path, mock_state=shared)
        eng.statuses["install.0"] = ST_DONE  # 解锁 install.1
        eng.run_step("install.1")  # pip install lerobot
        assert shared.get("lerobot") == "0.5.2"
        # 安装后 find-port 成功
        eng.statuses["install.2"] = ST_DONE
        eng.run_step("configure.0")
        assert eng.statuses["configure.0"] == ST_DONE


class TestPorts:
    def test_parse_ports_from_find_port_output(self):
        text = "[0] COM3 [FEETECH] (available)\n[1] COM4 [FEETECH] (available)"
        assert parse_ports(text) == ["COM3", "COM4"]
        assert parse_ports("nothing here") == []

    def test_parse_ports_real_interactive_verdict(self):
        """真实 lerobot ≥0.6 的拔线结论行（回归：此前只认列表式，导致自动填入失效）。"""
        text = (
            "Finding all available ports for the MotorsBus.\n\n"
            "Ports before disconnecting: ['COM8', 'COM4', 'COM3']\n\n"
            "Remove the USB cable from your MotorsBus and press Enter when done.\n\n"
            "The port of this MotorsBus is 'COM12'\n\n"
            "Reconnect the USB cable."
        )
        assert parse_ports(text) == ["COM12"]

    def test_capture_real_interactive_output(self, tmp_path):
        eng = make_engine(tmp_path)
        eng._maybe_capture_ports(
            "configure.0", "lerobot-find-port", 0,
            "The port of this MotorsBus is 'COM11'",
        )
        assert eng.ports["follower"] == "COM11"

    def test_apply_ports_substitutes_placeholders(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.set_ports("COM7", "COM9")
        cmd = eng._apply_ports(
            "lerobot-setup-motors --robot.type=so101_follower --robot.port=/dev/ttyACM0 "
            "--teleop.type=so101_leader --teleop.port=/dev/ttyACM1"
        )
        assert "COM7" in cmd and "COM9" in cmd
        assert "/dev/ttyACM" not in cmd


class TestPersistence:
    def test_save_load_roundtrip(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.run_step("install.0")
        eng.set_ports("COM3", "COM4")
        eng.skip_step("install.1")  # 走正式接口（会保存）

        eng2 = make_engine(tmp_path)  # 同一 state_path 重新加载
        assert eng2.statuses["install.0"] == ST_DONE
        assert eng2.statuses["install.1"] == ST_SKIPPED
        assert eng2.ports == {"follower": "COM3", "leader": "COM4"}

    def test_running_status_recovered_as_pending(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.statuses["install.0"] = "running"
        eng._save()
        eng2 = make_engine(tmp_path)
        assert eng2.statuses["install.0"] == ST_PENDING

    def test_reset_clears_everything(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.run_step("install.0")
        eng.set_ports("COM3", "")
        eng.reset()
        assert all(s == ST_PENDING for s in eng.statuses.values())
        assert eng.ports == {"follower": "", "leader": ""}


class TestSkip:
    def test_skip_pending_and_unlocks_next(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.skip_step("install.0")
        assert eng.statuses["install.0"] == ST_SKIPPED
        assert eng.unlocked("install.1")

    def test_cannot_skip_done(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.run_step("install.0")
        with pytest.raises(FlowError):
            eng.skip_step("install.0")


class TestPortAutoCapture:
    def _captured(self, eng):
        events = []
        orig = eng.bus.publish
        eng.bus.publish = lambda kind, data=None: events.append((kind, data))
        return events

    def test_single_port_fills_follower_then_leader(self, tmp_path):
        eng = make_engine(tmp_path)
        events = self._captured(eng)
        out = "CRITICAL:find-port: Found 1 ports:\n[0] COM5 [FEETECH] (available)"
        eng._maybe_capture_ports("configure.0", "lerobot-find-port", 0, out)
        assert eng.ports["follower"] == "COM5"
        assert any(k == "ports_autofilled" and d["target"] == "follower" and d["both"] is False
                   for k, d in events)

        eng._maybe_capture_ports("configure.1", "lerobot-find-port", 0, out.replace("COM5", "COM6"))
        assert eng.ports["leader"] == "COM6"
        assert any(k == "ports_autofilled" and d["both"] is True for k, d in events)
        # 已保存
        assert "COM5" in eng.state_path.read_text(encoding="utf-8")

    def test_multiple_ports_no_guess(self, tmp_path):
        eng = make_engine(tmp_path)
        events = self._captured(eng)
        out = "[0] COM3 [FEETECH] (available)\n[1] COM4 [FEETECH] (available)"
        eng._maybe_capture_ports("configure.0", "lerobot-find-port", 0, out)
        assert eng.ports["follower"] == ""  # 不瞎猜
        assert any(k == "ports_detected" for k, d in events)

    def test_failure_output_not_captured(self, tmp_path):
        eng = make_engine(tmp_path)
        eng._maybe_capture_ports("configure.0", "lerobot-find-port", 1, "[0] COM5 [FEETECH]")
        assert eng.ports["follower"] == ""

    def test_other_step_find_port_fills_empty_slot(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.set_ports("COM5", "")  # follower 已知
        eng._maybe_capture_ports("configure.3", "lerobot-find-port", 0,
                                 "[0] COM6 [FEETECH] (available)")
        assert eng.ports["leader"] == "COM6"


class TestFlowVersionMigration:
    def test_stale_progress_resets_configure_keeps_install(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.statuses["install.0"] = ST_DONE
        eng.statuses["configure.0"] = ST_DONE
        # 手写旧版进度（无 flow_version 字段）
        import json as _json

        data = {
            "product": "so101",
            "statuses": {"install.0": "done", "configure.0": "done"},
            "updated_at": {},
            "ports": {"follower": "", "leader": ""},
        }
        eng.state_path.write_text(_json.dumps(data), encoding="utf-8")

        eng2 = make_engine(tmp_path)
        assert eng2.statuses["install.0"] == ST_DONE   # install 保留
        assert eng2.statuses["configure.0"] == ST_PENDING  # 编号变了 → 重置

    def test_current_version_progress_loaded_as_is(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.statuses["install.0"] = ST_DONE
        eng.statuses["configure.0"] = ST_DONE
        eng._save()
        eng2 = make_engine(tmp_path)
        assert eng2.statuses["configure.0"] == ST_DONE


class TestConcurrency:
    def test_second_run_while_busy_rejected(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.statuses["install.0"] = ST_DONE
        started = threading.Event()
        release = threading.Event()
        real_approve = eng.approve

        def slow_approve(*a):
            started.set()
            release.wait(5)
            return real_approve(*a)

        eng.approve = slow_approve
        t = threading.Thread(target=eng.run_step, args=("install.1",), daemon=True)
        t.start()
        assert started.wait(5)
        with pytest.raises(FlowError):  # running_step 占用中
            eng.check_can_run("install.2")
        release.set()
        t.join(10)
        assert eng.statuses["install.1"] == ST_DONE


class TestLogTrim:
    def test_short_output_kept_intact(self):
        from arm_ui.flow import _trim_for_log
        text = "line1\nline2"
        assert _trim_for_log(text) == text

    def test_huge_output_trimmed_head_tail(self):
        from arm_ui.flow import _trim_for_log
        text = "HEADMARK\n" + "x" * 60000 + "\nTAILMARK"
        trimmed = _trim_for_log(text)
        assert "HEADMARK" in trimmed and "TAILMARK" in trimmed
        assert "高频输出" in trimmed
        assert len(trimmed) < len(text)


class TestTeleoperateStopIsDone:
    def test_user_stop_on_teleoperate_counts_as_done(self, tmp_path, monkeypatch):
        """遥操作被用户停止 = 正常完成；同样的非零退出、非用户停止 = 失败。"""
        from arm_ui import flow as flow_mod

        eng = make_engine(tmp_path)
        monkeypatch.setattr(flow_mod, "run_mock_streaming", lambda *a, **k: (130, "会话被中断", ""))
        eng.set_ports("COM3", "COM4")  # 绕过 Windows 占位端口守卫
        for sid in eng.order[:9]:
            eng.statuses[sid] = ST_DONE

        eng.statuses["calibrate.2"] = ST_PENDING
        eng.run_step("calibrate.2")  # 非用户停止 → 失败
        assert eng.statuses["calibrate.2"] == ST_FAILED

        eng.statuses["calibrate.2"] = ST_PENDING
        eng._user_stopped = True  # 模拟用户点了「停止」
        eng.run_step("calibrate.2")
        assert eng.statuses["calibrate.2"] == ST_DONE

    def test_user_stop_on_rollout_counts_as_done(self, tmp_path, monkeypatch):
        """评估（lerobot-rollout）被用户停止 = 正常完成。"""
        from arm_ui import flow as flow_mod

        eng = make_engine(tmp_path)
        monkeypatch.setattr(flow_mod, "run_mock_streaming", lambda *a, **k: (130, "会话被中断", ""))
        eng.set_ports("COM3", "COM4")
        for sid in eng.order[:-1]:
            eng.statuses[sid] = ST_DONE
        eng.statuses["next.2"] = ST_PENDING
        eng._user_stopped = True  # 模拟用户点了「停止」
        eng.run_step("next.2")
        assert eng.statuses["next.2"] == ST_DONE


class TestRecordConfig:
    def test_validation(self, tmp_path):
        eng = make_engine(tmp_path)
        with pytest.raises(FlowError):
            eng.set_record_config("no-slash", 20)   # 缺少 用户名/ 前缀
        with pytest.raises(FlowError):
            eng.set_record_config("a/b", 0)          # 数量越界
        with pytest.raises(FlowError):
            # 双视角依赖第一相机
            eng.set_record_config("a/b", 5, camera=False, camera2=True)
        with pytest.raises(FlowError):
            # 两个相机同号
            eng.set_record_config("a/b", 5, camera=True, camera_index=1, camera2=True, camera2_index=1)
        ok = eng.set_record_config("zhangsan/pick_cube", 30)
        assert ok["repo_id"] == "zhangsan/pick_cube" and ok["num_episodes"] == 30
        assert ok["camera2"] is False and ok["camera2_index"] == 2  # 新字段有默认值

    def test_dual_camera_args_in_record_and_eval(self, tmp_path):
        """双视角：采集与评估命令都带 cam_0+cam_1；单视角只带 cam_0。"""
        eng = make_engine(tmp_path)
        eng.set_ports("COM3", "COM4")
        eng.set_record_config("zhangsan/box", 5, camera=True, camera_index=1,
                              camera2=True, camera2_index=2)
        rec = eng._apply_record_config("lerobot-record --dataset.repo_id=你的用户名/数据集名")
        assert "cam_0: {type: opencv, index_or_path: 1" in rec
        assert "cam_1: {type: opencv, index_or_path: 2" in rec
        ev = eng._apply_record_config("lerobot-eval --policy.path=outputs/train/act/checkpoints/last")
        assert "cam_0: {type: opencv, index_or_path: 1" in ev
        assert "cam_1: {type: opencv, index_or_path: 2" in ev

        # 关掉双视角 → 命令只剩 cam_0
        eng.set_record_config("zhangsan/box", 5, camera=True, camera_index=1)
        rec2 = eng._apply_record_config("lerobot-record --dataset.repo_id=你的用户名/数据集名")
        assert "cam_0" in rec2 and "cam_1" not in rec2

    def test_substitution_into_command(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.set_record_config("zhangsan/pick_cube", 25)
        cmd = eng._apply_record_config(
            "lerobot-record --dataset.repo_id=你的用户名/数据集名 --num-episodes=50"
        )
        assert "zhangsan/pick_cube" in cmd
        assert "--dataset.num_episodes=25" in cmd
        assert "你的用户名" not in cmd

    def test_unset_repo_blocks_step(self, tmp_path, monkeypatch):
        """没填数据集名称 → 采集步骤被拦截，不执行命令。"""
        eng = make_engine(tmp_path)
        eng.set_ports("COM3", "COM4")
        for sid in eng.order[:12]:
            eng.statuses[sid] = ST_DONE
        monkeypatch.setattr("arm_ui.flow.run_mock_streaming", lambda *a, **k: (0, "", ""))
        eng.run_step("next.0")
        assert eng.statuses["next.0"] == ST_FAILED
        assert "数据集名称" in eng.run_log_path.read_text(encoding="utf-8")

    def test_record_step_runs_with_config(self, tmp_path, monkeypatch):
        eng = make_engine(tmp_path)
        eng.set_ports("COM3", "COM4")
        eng.set_record_config("zhangsan/pick_cube", 30)
        for sid in eng.order[:12]:
            eng.statuses[sid] = ST_DONE
        monkeypatch.setattr("arm_ui.flow.run_mock_streaming", lambda *a, **k: (0, "done", ""))
        eng.run_step("next.0")
        assert eng.statuses["next.0"] == ST_DONE
        log = eng.run_log_path.read_text(encoding="utf-8")
        assert "zhangsan/pick_cube" in log and "--dataset.num_episodes=30" in log

    def test_persistence_roundtrip(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.set_record_config("zhangsan/pick_cube", 30, camera=True, camera_index=1,
                              camera2=True, camera2_index=2)
        eng2 = make_engine(tmp_path)
        assert eng2.record_config["repo_id"] == "zhangsan/pick_cube"
        assert eng2.record_config["num_episodes"] == 30
        assert eng2.record_config["camera2"] is True
        assert eng2.record_config["camera2_index"] == 2


class TestInstallPatch:
    def test_install_command_includes_all_extras(self, tmp_path):
        """第一关安装命令被补丁为 CUDA 版 torch + 一次装全 4 个附加包。"""
        eng = make_engine(tmp_path)
        payload_cmds = [c for p in eng.profile_payload()["phases"] for s in p["steps"] for c in s["commands"]]
        assert any(
            "download.pytorch.org/whl/cu126" in c and "--extra-index-url" in c
            and "lerobot[feetech,dataset,training,evaluation]" in c and "pynput" in c
            for c in payload_cmds
        ), "安装命令应默认使用 CUDA 版 torch 源"
        assert 'pip install "lerobot[feetech]"' not in payload_cmds

    def test_patched_install_passes_whitelist_and_executes(self, tmp_path):
        from arm_copilot.tools.shell import validate

        from arm_ui.flow import _INSTALL_PATCH_TO

        ok, why = validate(_INSTALL_PATCH_TO)
        assert ok, why
        eng = make_engine(tmp_path)  # approve 默认放行，mock 安装写 state
        eng.statuses["install.0"] = ST_DONE  # 解锁 install.1
        eng.run_step("install.1")
        assert eng.statuses["install.1"] == ST_DONE
        log = eng.run_log_path.read_text(encoding="utf-8")
        assert "feetech,dataset,training,evaluation" in log
        assert " pynput" in log

    def test_record_command_disables_push_to_hub(self):
        from arm_ui.flow import _patch_command

        cmd = _patch_command("lerobot-record --robot.port=/dev/ttyACM0 --dataset.repo_id=a/b --num-episodes=50")
        assert "--dataset.push_to_hub=false" in cmd
        # 其他命令不受影响
        assert "--push_to_hub" not in _patch_command("lerobot-calibrate --robot.type=so101_follower")

    def test_train_command_resolves_local_versioned_dataset(self, tmp_path, monkeypatch):
        """训练命令的数据集名自动解析到本地带时间戳的实际目录（离线可加载）。"""
        import time as _time
        from pathlib import Path
        from arm_copilot.products.base import PRODUCTS

        from arm_ui.events import EventBus
        from arm_ui.flow import FlowEngine

        fake_base = tmp_path / ".cache/huggingface/lerobot"
        for ver in ("box_20260101_000000", "box_20260102_000000"):
            d = fake_base / "usr" / ver / "meta"
            d.mkdir(parents=True)
            (d / "info.json").write_text('{"total_episodes": 1}', encoding="utf-8")
            _time.sleep(0.01)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        eng = FlowEngine(PRODUCTS["so101"], mock=True, approve=lambda *a: True, bus=EventBus(),
                         mock_state={}, state_path=tmp_path / "p.json", mock_line_delay=0)
        eng.set_record_config("usr/box", 10)
        train_cmd = eng._apply_record_config(
            "lerobot-train --policy.type=act --dataset.repo_id=你的用户名/数据集名"
        )
        assert "--dataset.repo_id=usr/box_20260102_000000" in train_cmd  # 取最新版本
        record_cmd = eng._apply_record_config(
            "lerobot-record --dataset.repo_id=你的用户名/数据集名 --num-episodes=50"
        )
        assert "--dataset.repo_id=usr/box " in record_cmd + " "  # 录制仍用裸名

    def test_train_command_pinned_output_and_no_push(self):
        from arm_ui.flow import _patch_command

        cmd = _patch_command("lerobot-train --policy.type=act --dataset.repo_id=usr/box")
        assert "--policy.push_to_hub=false" in cmd
        assert "--output_dir=outputs/train/act" in cmd

    def test_rotate_train_dir_archives_old(self, tmp_path, monkeypatch):
        import time as _time
        from arm_ui.flow import _rotate_train_dir

        monkeypatch.chdir(tmp_path)
        old = tmp_path / "outputs/train/act"
        old.mkdir(parents=True)
        (old / "checkpoints").mkdir()
        _rotate_train_dir()
        backups = list((tmp_path / "outputs/train").glob("act_backup_*"))
        assert len(backups) == 1 and backups[0].joinpath("checkpoints").exists()
        assert not old.exists()
        _rotate_train_dir()  # 目录不存在时安全空转

    def test_ensure_last_checkpoint_creates_junction_and_reports_success(self, tmp_path, monkeypatch):
        from arm_ui.flow import _ensure_last_checkpoint

        monkeypatch.chdir(tmp_path)
        ckpt = tmp_path / "outputs/train/act/checkpoints"
        (ckpt / "000002").mkdir(parents=True)
        (ckpt / "000002" / "pretrained_model").mkdir()

        # 无 1314 证据（如别的错误）→ 不判成功，但链接仍会兜底补建
        assert _ensure_last_checkpoint("boom") is False
        assert (ckpt / "last" / "pretrained_model").exists()

        # 输出证实只死在最后的符号链接（WinError 1314）→ 判成功
        out = 'File "train_utils.py", line 101, in update_last_checkpoint\nOSError: [WinError 1314]'
        assert _ensure_last_checkpoint(out) is True
        last = ckpt / "last"
        assert last.exists() and (last / "pretrained_model").exists()

        # 幂等：再次调用同样判定成功，链接不重复创建
        assert _ensure_last_checkpoint(out) is True

    def test_ensure_last_checkpoint_repoints_stale_link(self, tmp_path, monkeypatch):
        from arm_ui.flow import _ensure_last_checkpoint

        monkeypatch.chdir(tmp_path)
        ckpt = tmp_path / "outputs/train/act/checkpoints"
        (ckpt / "000001").mkdir(parents=True)
        (ckpt / "000002").mkdir(parents=True)
        (ckpt / "last").mkdir()  # 指向错误目标的旧目录
        ok = _ensure_last_checkpoint("...update_last_checkpoint... WinError 1314")
        assert ok is True
        # last 现在指向最新的 000002
        assert (ckpt / "last" / ".").samefile(ckpt / "000002")

    def test_camera_enabled_appends_to_record_and_eval_becomes_rollout(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.set_ports("COM11", "COM12")
        eng.set_record_config("usr/box", 5, camera=True, camera_index=1)
        rec = eng._apply_record_config("lerobot-record --dataset.repo_id=你的用户名/数据集名 --num-episodes=50")
        assert '--robot.cameras="{cam_0: {type: opencv, index_or_path: 1' in rec
        ev = eng._apply_record_config("lerobot-eval --policy.path=outputs/train/act/checkpoints/last")
        assert ev.startswith("lerobot-rollout --robot.type=so101_follower --robot.port=COM11")
        assert "index_or_path: 1" in ev
        # policy.path 必须指到 pretrained_model 子目录（0.6 checkpoint 结构）
        assert "--policy.path=outputs/train/act/checkpoints/last/pretrained_model" in ev
        # rollout 默认无限跑，必须限时让它能自己正常结束
        assert "--duration=120" in ev

    def test_camera_disabled_no_camera_arg(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.set_record_config("usr/box", 5, camera=False)
        rec = eng._apply_record_config("lerobot-record --dataset.repo_id=你的用户名/数据集名")
        assert "--robot.cameras" not in rec

    def test_camera_command_passes_whitelist(self, tmp_path):
        from arm_copilot.tools.shell import validate

        eng = make_engine(tmp_path)
        eng.set_record_config("usr/box", 5, camera=True, camera_index=0)
        eng.set_ports("COM11", "COM12")
        cmd = eng._apply_record_config("lerobot-record --dataset.repo_id=你的用户名/数据集名")
        ok, why = validate(cmd)
        assert ok, why

    def test_empty_dataset_shell_is_skipped(self, tmp_path, monkeypatch):
        """收尾被中断的 0 条空壳目录不能被选为训练数据。"""
        from pathlib import Path
        from arm_copilot.products.base import PRODUCTS

        from arm_ui.events import EventBus
        from arm_ui.flow import FlowEngine

        fake = tmp_path / ".cache/huggingface/lerobot/usr"
        good = fake / "box_20260101_000000" / "meta"
        empty = fake / "box_20260102_000000" / "meta"   # 更新但为空壳
        for d, body in ((good, '{"total_episodes": 3}'), (empty, '{"total_episodes": 0}')):
            d.mkdir(parents=True)
            (d / "info.json").write_text(body, encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        eng = FlowEngine(PRODUCTS["so101"], mock=True, approve=lambda *a: True, bus=EventBus(),
                         mock_state={}, state_path=tmp_path / "p2.json", mock_line_delay=0)
        eng.set_record_config("usr/box", 5)
        cmd = eng._apply_record_config("lerobot-train --dataset.repo_id=你的用户名/数据集名")
        assert "--dataset.repo_id=usr/box_20260101_000000" in cmd  # 跳过空壳选有效数据


class TestTrainConfig:
    def test_set_and_validate(self, tmp_path):
        eng = make_engine(tmp_path)
        with pytest.raises(FlowError):
            eng.set_train_config(0, 16)           # 步数太少（冒烟下限 1）
        with pytest.raises(FlowError):
            eng.set_train_config(3000, 0)         # batch 越界
        ok = eng.set_train_config(5000, 32, "cuda")
        assert ok == {"steps": 5000, "batch_size": 32, "device": "cuda"}

    def test_train_command_includes_params(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.set_train_config(2500, 8, "cpu")
        eng.set_record_config("usr/box", 5)
        cmd = eng._apply_record_config("lerobot-train --policy.type=act --dataset.repo_id=你的用户名/数据集名")
        assert "--steps=2500" in cmd and "--batch_size=8" in cmd
        assert "--policy.device" not in cmd  # cpu 不显式传

    def test_cuda_device_flag(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.set_train_config(2500, 8, "cuda")
        cmd = eng._apply_record_config("lerobot-train --policy.type=act --dataset.repo_id=x/y")
        assert "--policy.device=cuda" in cmd

    def test_persistence(self, tmp_path):
        eng = make_engine(tmp_path)
        eng.set_train_config(1234, 64, "cpu")
        eng2 = make_engine(tmp_path)
        assert eng2.train_config["steps"] == 1234 and eng2.train_config["batch_size"] == 64
