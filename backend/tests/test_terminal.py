"""验证终端分发和事件异常路径，不访问远程模型。"""

import json
from types import SimpleNamespace

import pytest

import app.cli.terminal as terminal
from app.agents.events import RunEvents
from app.cli.display import show_report


@pytest.mark.parametrize(
    "status,label",
    [
        ("satisfied", "满足"),
        ("not_satisfied", "不满足"),
        ("unknown", "待确认"),
        ("conflict", "证据冲突"),
    ],
)
def test_report_summary_preserves_verdict(status, label):
    """
    作用：验证摘要保留真实结论和未解决项，隐藏内部引用。
    输入：status - 原始判定；label - 中文判定。
    输出：无
    """
    output = []
    show_report(
        {
            "constraint_result": {
                "status": status,
                "reason": "核验原因",
                "unresolved": ["待核对条件"],
                "fact_ids": ["internal-fact-id"],
                "evidence_ids": ["internal-evidence-id"],
            }
        },
        output.append,
    )
    text = "\n".join(output)
    assert f"结论：{label}" in text
    assert "核验原因" in text and "待核对条件" in text
    assert "internal-" not in text


def test_terminal_commands(tmp_path, monkeypatch):
    """
    作用：验证打开、查询、对话和退出使用正确服务边界。
    输入：tmp_path - pytest 临时目录；monkeypatch - pytest 依赖替换工具。
    输出：无
    """
    # 用可控服务隔离终端分发；真实执行另由服务测试覆盖。
    calls, output = [], []
    monkeypatch.setattr(terminal, "load_task", lambda *a, **k: {"task_id": "task-test"})
    monkeypatch.setattr(terminal, "get_task_status", lambda *a, **k: {"adc_records": {}})

    def send(task_id, message, **kwargs):
        """
        作用：记录普通消息的服务调用并返回固定答复。
        输入：task_id - 任务 ID；message - 用户消息；kwargs - 其他服务参数。
        输出：dict，合成模型答复和程序报告。
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
    输入：tmp_path - pytest 临时目录。
    输出：无
    """
    # 使用固定异常模拟真实工具失败，不伪造成功事件。
    events = RunEvents("task-test", "run-test", tmp_path / "events.jsonl")
    request = SimpleNamespace(tool_call={"name": "read_file", "id": "call-1", "args": {}})

    def fail(request):
        """
        作用：模拟实际工具执行失败。
        输入：request - 工具请求。
        输出：无
        """
        # 固定故障只用于验证中间件，不访问文件。
        raise ValueError("synthetic failure")

    with pytest.raises(ValueError):
        events.wrap_tool_call(request, fail)
    records = [json.loads(line) for line in events.path.read_text().splitlines()]
    assert [r["type"] for r in records] == ["tool_started", "tool_failed"]
    assert records[-1]["payload"]["reason"] == "ValueError"


def test_review_command(tmp_path, monkeypatch):
    """
    作用：审核命令只提交宿主服务，格式错误时不写入，也不触发模型。
    输入：tmp_path - pytest 临时目录；monkeypatch - pytest 依赖替换工具。
    输出：无
    """
    # 固定服务响应，单独验证命令解析与服务参数传递。
    calls, output = [], []
    monkeypatch.setattr(terminal, "load_task", lambda *a, **k: {"task_id": "task-test"})

    def review(task_id, **kwargs):
        """
        作用：记录审核命令的服务参数并返回固定审核结果。
        输入：task_id - 任务 ID；kwargs - 审核请求参数。
        输出：dict，包含审核版本及合成报告。
        """
        calls.append((task_id, kwargs))
        return {"revision": 1, "report": {"status": "satisfied"}}

    monkeypatch.setattr(terminal, "review_supply", review)
    monkeypatch.setattr(terminal, "get_task_status", lambda *a, **k: {"reviews": [{"revision": 1}]})
    # 同时输入错误格式和合法审核，确认只有合法请求提交到服务。
    commands = iter(
        [
            "/open task-test",
            "/review bad",
            "/review r1 | 0 | tester | approve | 核对原文",
            "/reviews",
            "/exit",
        ]
    )
    terminal.run_terminal(root=tmp_path, read=lambda _: next(commands), write=output.append)
    assert calls == [
        (
            "task-test",
            {
                "request_id": "r1",
                "expected_revision": 0,
                "reviewer": "tester",
                "decision": "approve",
                "note": "核对原文",
                "root": tmp_path,
            },
        )
    ]
    assert any("本次未调用模型" in line for line in output)


def test_wait_and_resume_commands(tmp_path, monkeypatch):
    """
    作用：验证等待与恢复命令独立于普通消息分发。
    输入：tmp_path - 临时目录；monkeypatch - 依赖替换工具。
    输出：无
    """
    calls, output = [], []
    monkeypatch.setattr(terminal, "load_task", lambda *a, **k: {"task_id": "task-test"})

    def wait(task_id, **kwargs):
        """
        作用：记录等待命令参数。
        输入：task_id - 任务 ID；kwargs - 等待参数。
        输出：合成等待事项。
        """
        calls.append(("wait", task_id, kwargs))
        return {"pending": [{"id": "pending-test"}]}

    def resume(task_id, interrupt_id, answer, **kwargs):
        """
        作用：记录明确恢复操作的参数。
        输入：task_id - 任务 ID；interrupt_id - 中断 ID；answer - 答案；kwargs - 服务参数。
        输出：合成运行结果。
        """
        calls.append(("resume", task_id, interrupt_id, answer))
        return {"report": {}, "reply": "已恢复"}

    monkeypatch.setattr(terminal, "wait_for_input", wait)
    monkeypatch.setattr(terminal, "resume_task", resume)
    commands = iter(
        [
            "/open task-test",
            "/wait-review",
            "/resume bad",
            "/resume pending-test | review-1",
            "/ask 需要说明什么？",
            "/exit",
        ]
    )
    terminal.run_terminal(root=tmp_path, read=lambda _: next(commands), write=output.append)
    assert calls == [
        ("wait", "task-test", {"root": tmp_path}),
        ("resume", "task-test", "pending-test", "review-1"),
        (
            "wait",
            "task-test",
            {"root": tmp_path, "kind": "clarification", "question": "需要说明什么？"},
        ),
    ]
