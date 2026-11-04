"""shell 工具：白名单、风险分级、确认门、mock 执行。"""

from arm_copilot.tools.base import ToolContext
from arm_copilot.tools.shell import classify_risk, make_run_command_tool, validate


class TestValidate:
    def test_allows_lerobot(self):
        assert validate("lerobot-find-port")[0]
        assert validate("lerobot-calibrate --robot.type=so101_follower --robot.port=COM3")[0]
        assert validate("lerobot-teleoperate --robot.type=so101_follower --robot.port=COM3")[0]

    def test_allows_pip_and_python_queries(self):
        assert validate('pip install "lerobot[feetech]"')[0]
        assert validate("pip show lerobot")[0]
        assert validate("python --version")[0]
        assert validate("python -m pip install lerobot")[0]

    def test_rejects_out_of_whitelist(self):
        for cmd in [
            "curl http://example.sh",
            "git push origin main",
            "node index.js",
            "python train.py",
            "python -c import_os",
        ]:
            assert not validate(cmd)[0], cmd

    def test_rejects_dangerous_patterns(self):
        for cmd in [
            "pip install x && rm -rf /",
            "pip install x; ls",
            "lerobot-find-port > /etc/passwd",
            "pip install `whoami`",
            "rm -rf /",
            "sudo pip install lerobot",
        ]:
            assert not validate(cmd)[0], cmd

    def test_rejects_empty(self):
        assert not validate("   ")[0]


class TestRisk:
    def test_hardware(self):
        for cmd in [
            "lerobot-calibrate --robot.type=so101_follower --robot.port=COM3",
            "lerobot-setup-motors --teleop.type=so101_leader --teleop.port=COM4",
            "lerobot-teleoperate --robot.type=so101_follower",
            "lerobot-record --dataset.repo_id=x/y",
        ]:
            assert classify_risk(cmd) == "hardware", cmd

    def test_risky(self):
        assert classify_risk('pip install "lerobot[feetech]"') == "risky"
        assert classify_risk("python -m pip install lerobot") == "risky"
        assert classify_risk("lerobot-train --policy.type=act") == "risky"

    def test_safe(self):
        assert classify_risk("lerobot-find-port") == "safe"
        assert classify_risk("pip show lerobot") == "safe"
        assert classify_risk("python --version") == "safe"


class TestRunCommandTool:
    def test_mock_install_sets_state(self):
        ctx = ToolContext(mock=True, approve=lambda *a: True)
        tool = make_run_command_tool(ctx)
        out = tool.handler({"command": 'pip install "lerobot[feetech]"', "reason": "安装依赖"})
        assert "Successfully installed" in out
        assert ctx.mock_state.get("lerobot") == "0.5.2"

    def test_risky_requires_approval_and_respects_deny(self):
        seen = []

        def approve(command, risk, reason):
            seen.append(risk)
            return False

        tool = make_run_command_tool(ToolContext(mock=True, approve=approve))
        out = tool.handler({"command": "pip install lerobot", "reason": ""})
        assert seen == ["risky"]
        assert "拒绝" in out

    def test_safe_skips_approval(self):
        seen = []
        ctx = ToolContext(mock=True, approve=lambda *a: seen.append(1) or True)
        ctx.mock_state["lerobot"] = "0.5.2"
        make_run_command_tool(ctx).handler({"command": "lerobot-find-port", "reason": ""})
        assert not seen

    def test_rejects_non_whitelisted_even_if_approved(self):
        tool = make_run_command_tool(ToolContext(mock=True, approve=lambda *a: True))
        out = tool.handler({"command": "rm -rf /", "reason": ""})
        assert "拒绝" in out

    def test_hardware_mock_flow(self):
        ctx = ToolContext(mock=True, approve=lambda *a: True)
        ctx.mock_state["lerobot"] = "0.5.2"
        out = make_run_command_tool(ctx).handler({
            "command": "lerobot-calibrate --robot.type=so101_follower --robot.port=COM3",
            "reason": "校准",
        })
        assert "Calibration saved" in out

    def test_risky_denied_when_no_approver(self):
        # 无确认回调（approve=None）时按拒绝处理（fail-closed），命令不执行
        ctx = ToolContext(mock=True)
        out = make_run_command_tool(ctx).handler({"command": "pip install lerobot", "reason": ""})
        assert "按拒绝处理" in out
        assert ctx.mock_state.get("lerobot") is None

    def test_mock_python_m_pip_install_sets_state(self):
        # python -m pip 形式的安装在 mock 下与裸 pip 等价，必须写入 state
        ctx = ToolContext(mock=True, approve=lambda *a: True)
        out = make_run_command_tool(ctx).handler({
            "command": 'python -m pip install "lerobot[feetech]"',
            "reason": "安装依赖",
        })
        assert "Successfully installed" in out
        assert ctx.mock_state.get("lerobot") == "0.5.2"

    def test_mock_python_m_pip_show_reads_state(self):
        ctx = ToolContext(mock=True)
        ctx.mock_state["lerobot"] = "0.5.2"
        out = make_run_command_tool(ctx).handler({"command": "python -m pip show lerobot", "reason": ""})
        assert "0.5.2" in out


class TestMockNextPhase:
    def test_help_is_safe(self):
        assert classify_risk("lerobot-record --help") == "safe"
        assert classify_risk("lerobot-teleoperate -h") == "safe"
        # 非 help 形式仍按原规则分级
        assert classify_risk("lerobot-record --dataset.repo_id=x/y") == "hardware"

    def test_help_mock_returns_args_summary(self):
        tool = make_run_command_tool(ToolContext(mock=True))
        out = tool.handler({"command": "lerobot-record --help", "reason": ""})
        assert "--dataset.repo_id" in out
        assert "--cameras" in out

    def test_record_mock(self):
        tool = make_run_command_tool(ToolContext(mock=True, approve=lambda *a: True))
        out = tool.handler({
            "command": "lerobot-record --robot.type=so101_follower --robot.port=COM3 "
                       "--teleop.type=so101_leader --teleop.port=COM4 "
                       "--dataset.repo_id=user/demo --num-episodes=10",
            "reason": "采集",
        })
        assert "user/demo" in out
        assert "未配置相机" in out
        assert "episode" in out

    def test_record_mock_with_cameras(self):
        tool = make_run_command_tool(ToolContext(mock=True, approve=lambda *a: True))
        out = tool.handler({
            "command": "lerobot-record --robot.port=COM3 --teleop.port=COM4 "
                       "--dataset.repo_id=user/demo --cameras.type=opencv --cameras.index=0",
            "reason": "采集",
        })
        assert "相机" in out
        assert "opencv" in out

    def test_train_mock_checkpoint(self):
        tool = make_run_command_tool(ToolContext(mock=True, approve=lambda *a: True))
        out = tool.handler({
            "command": "lerobot-train --policy.type=act --dataset.repo_id=user/demo",
            "reason": "训练",
        })
        assert "outputs/train/act/checkpoints" in out

    def test_eval_mock(self):
        tool = make_run_command_tool(ToolContext(mock=True, approve=lambda *a: True))
        out = tool.handler({
            "command": "lerobot-eval --policy.path=outputs/train/act/checkpoints/last",
            "reason": "评估",
        })
        assert "checkpoint" in out
