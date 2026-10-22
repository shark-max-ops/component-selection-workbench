"""验证终端分发和事件异常路径，不访问远程模型。"""

import json
from types import SimpleNamespace

import pytest

import app.cli.terminal as terminal
from app.agents.events import RunEvents


def test_terminal_commands(tmp_path, monkeypatch):
    """
    作用：验证打开、查询、对话和退出使用正确服务边界。
    输入：临时目录与 monkeypatch。
    输出：无返回值；断言只有普通消息触发模型服务。
    """
    # 用可控服务隔离终端分发；真实执行另由服务测试覆盖。
    calls, output = [], []
    monkeypatch.setattr(terminal, "load_task", lambda *a, **k: {"task_id": "task-test"})
    monkeypatch.setattr(terminal, "get_task_status", lambda *a, **k: {"adc_records": {}})

    def send(task_id, message, **kwargs):
        """
        作用：记录终端提交的普通消息。
        输入：任务标识、消息与额外服务参数。
        输出：合成的解释和结构化结果。
        """
        # 记录调用次数，防止查询或未知命令误触模型。
        calls.append((task_id, message))
        return {"reply": "说明", "report": {"status": "unknown"}}

    monkeypatch.setattr(terminal, "send_message", send)
    commands = iter(
        [
            "/status",
            "/open task-test",
            "/status",
            "/evidence",
            "/report",
            "/wrong",
            "继续说明",
            "/exit",
        ]
    )
    terminal.run_terminal(root=tmp_path, read=lambda _: next(commands), write=output.append)
    assert calls == [("task-test", "继续说明")]
    assert any("模型答复" in line for line in output)
    assert any("程序核验" in line for line in output)


def test_failed_tool_events(tmp_path):
    """
    作用：验证工具抛异常也有配对的失败事件。
    输入：pytest 临时目录。
    输出：无返回值；断言异常类型和调用标识被记录。
    """
    # 使用固定异常模拟真实工具失败，不伪造成功事件。
    events = RunEvents("task-test", "run-test", tmp_path / "events.jsonl")
    request = SimpleNamespace(tool_call={"name": "read_file", "id": "call-1", "args": {}})

    def fail(request):
        """
        作用：制造可控工具异常。
        输入：工具请求。
        输出：抛出 ValueError，无正常返回值。
        """
        # 固定故障只用于验证中间件，不访问文件。
        raise ValueError("synthetic failure")

    with pytest.raises(ValueError):
        events.wrap_tool_call(request, fail)
    records = [json.loads(line) for line in events.path.read_text().splitlines()]
    assert [r["type"] for r in records] == ["tool_started", "tool_failed"]
    assert records[-1]["payload"]["reason"] == "ValueError"
