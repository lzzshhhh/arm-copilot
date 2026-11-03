"""产品档案数据完整性与工具查询。"""

import pytest

from arm_copilot.products import PRODUCTS
from arm_copilot.products.base import ProductProfile, register
from arm_copilot.tools.base import ToolContext
from arm_copilot.tools.diagnostics import _has_port_listing, make_env_check_tool
from arm_copilot.tools.knowledge import make_faq_tool, make_workflow_tool
from arm_copilot.tools.shell import validate


def test_so101_registered():
    p = PRODUCTS["so101"]
    assert {"install", "configure", "calibrate"} <= set(p.phases)
    assert p.faqs


def test_all_profile_commands_pass_whitelist():
    """不变量：档案里的每条命令都必须能通过安全白名单。"""
    for p in PRODUCTS.values():
        for phase, steps in p.phases.items():
            for s in steps:
                for c in s.commands:
                    ok, why = validate(c)
                    assert ok, f"{p.key}/{phase}/{s.title}: {c} -> {why}"


def test_workflow_tool_renders_commands():
    out = make_workflow_tool().handler({"product": "so101", "phase": "calibrate"})
    assert "lerobot-calibrate" in out
    assert "排障" in out


def test_workflow_tool_rejects_unknown():
    assert "未知产品" in make_workflow_tool().handler({"product": "nope"})
    assert "未知阶段" in make_workflow_tool().handler({"product": "so101", "phase": "nope"})


def test_faq_search_hits():
    out = make_faq_tool().handler({"query": "为什么要校准", "product": "so101"})
    assert "校准" in out


def test_faq_search_miss_reports_no_hit():
    out = make_faq_tool().handler({"query": "zzz qq xxx"}, )
    assert "未命中" in out


def test_env_check_mock_fresh_machine():
    tool = make_env_check_tool(ToolContext(mock=True))
    out = tool.handler({})
    assert "LeRobot" in out
    assert "未安装" in out


def test_env_check_mock_after_install():
    ctx = ToolContext(mock=True)
    ctx.mock_state["lerobot"] = "0.5.2"
    out = make_env_check_tool(ctx).handler({})
    assert "0.5.2" in out
    assert "COM" in out or "ttyACM" in out


def test_register_rejects_duplicate_key():
    dup = ProductProfile(key="so101", name="重复档案", description="", phases={}, faqs=[])
    with pytest.raises(ValueError, match="重复注册"):
        register(dup)
    assert PRODUCTS["so101"].name == "SO-101 机械臂"  # 原档案未被覆盖


def test_find_port_output_detection():
    assert _has_port_listing("CRITICAL:find-port: Found 2 ports:\n[0] COM3 [FEETECH] (available)")
    assert not _has_port_listing("CRITICAL:find-port: No ports found")
    assert not _has_port_listing("")
