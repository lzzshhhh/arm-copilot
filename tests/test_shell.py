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
