"""agent 主循环：用 FakeLLM 脚本化验证 tool-calling 流程。"""

from arm_copilot.agent.loop import Agent
from arm_copilot.tools import ToolContext, build_registry
from arm_copilot.tools.base import ToolRegistry


class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, id, name, arguments):
        self.id = id
        self.function = _Fn(name, arguments)


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.seen_tools = []

    def chat(self, messages, tools):
        self.seen_tools.append(tools)
        return self.responses.pop(0)


def _make_agent(responses, ctx=None):
    llm = FakeLLM(responses)
    ctx = ctx or ToolContext(mock=True)
    return Agent(llm, build_registry(ctx), max_steps=5), llm, ctx


def test_plain_answer_without_tools():
    agent, llm, _ = _make_agent([_Msg(content="你好，我是臂管家。")])
    history = []
    reply = agent.handle("你好", history)
    assert reply == "你好，我是臂管家。"
    assert len(llm.seen_tools[0]) == 4  # run_command / env_check / get_workflow / search_faq
    assert [m["role"] for m in history] == ["user", "assistant"]


def test_tool_call_roundtrip():
    ctx = ToolContext(mock=True)
    ctx.mock_state["lerobot"] = "0.5.2"  # 模拟已装好 lerobot 的环境
    agent, _, _ = _make_agent([
        _Msg(tool_calls=[_TC("call1", "run_command", '{"command": "lerobot-find-port", "reason": "找串口"}')]),
        _Msg(content="已找到 2 个 FEETECH 串口。"),
    ], ctx=ctx)
    events = []
    history = []
    reply = agent.handle("帮我找串口", history, on_event=lambda k, d: events.append(k))
    assert reply == "已找到 2 个 FEETECH 串口。"
    assert events == ["tool_start", "tool_end"]
    tool_msgs = [m for m in history if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert "FEETECH" in tool_msgs[0]["content"]
    # assistant 的 tool_calls 已回填为协议要求的结构
    assistant = [m for m in history if m["role"] == "assistant"][0]
    assert assistant["tool_calls"][0]["function"]["name"] == "run_command"


def test_max_steps_guard():
    always_tool = _Msg(tool_calls=[_TC("c", "run_command", '{"command": "python --version", "reason": "x"}')])
    agent, _, _ = _make_agent([always_tool] * 10)
    reply = agent.handle("触发上限", [])
    assert "上限" in reply


def test_unknown_tool_returns_error_text():
    reg = ToolRegistry()
    out = reg.call("nope", "{}")
    assert "未知工具" in out
