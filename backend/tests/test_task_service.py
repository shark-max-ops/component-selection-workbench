"""验证任务创建、只读查询、人工审核与续聊，不调用远程模型。"""

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage
from test_adc_tools import make_case
from test_persistence import OfflineModel

import app.agents.harness as harness
import app.services.task_service as service
from app.adc.extractors import extract_ads1115_supply
from app.adc.rules import check_supply_voltage
from app.contracts import AdcRequirements
from app.services.reviews import task_write


@pytest.mark.parametrize("voltage, approved_status", [("3.3", "satisfied"), ("6", "not_satisfied")])
def test_create_query_and_continue(tmp_path, monkeypatch, voltage, approved_status):
    """
    作用：验证任务创建、审核、续聊、历史保留与写入边界。
    输入：tmp_path - 临时目录；monkeypatch - 依赖替换工具；voltage - 电压；
          approved_status - 审核后期望判定。
    输出：无
    """
    # 构造明确标注的合成资料，不使用真实模型或厂家文件。
    _, document, _, _ = make_case()
    source = tmp_path / "source.pdf"
    source.write_bytes(b"synthetic fixture, not a manufacturer PDF")
    document = replace(
        document, directory=tmp_path, sha256=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(service, "register_pdf", lambda *args: document)
    monkeypatch.setattr(harness, "load_settings", lambda: None)
    # 创建任务并验证尚未生成检查点。
    meta = service.create_task(root=tmp_path / "tasks", voltage=voltage)
    task_id = meta["task_id"]
    root = tmp_path / "tasks"
    assert not service.get_task_status(task_id, root=root)["checkpoint_exists"]
    # 用真实规则预先计算期望结果，供报告一致性断言使用。
    evidence, fact = extract_ads1115_supply(document)
    result = check_supply_voltage(
        AdcRequirements.model_validate(meta["requirements"]), fact, evidence, document
    )
    report = {
        "task_id": task_id,
        "scope": "supply_voltage_only",
        "constraint_result": result.model_dump(mode="json"),
    }
    # 固定模型发出的工具请求，实际执行图、工具与检查点。
    calls = [
        ("write_todos", {"todos": [{"content": "verify", "status": "completed"}]}),
        *[
            ("read_file", {"file_path": path})
            for path in [
                service.SKILL_PATH,
                "/inputs/task.json",
                "/documents/index.json",
                "/documents/pages/0001.txt",
            ]
        ],
        ("read_adc_supply_evidence", {"document_id": document.id, "part_number": "ADS1115"}),
        ("check_adc_supply", {"fact_id": fact.id, "evidence_id": evidence.id}),
        ("write_file", {"file_path": "/outputs/report.json", "content": json.dumps(report)}),
    ]
    # 将请求转为离线模型响应，避免访问远程模型。
    responses = [
        AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call-{index}"}])
        for index, (name, args) in enumerate(calls)
    ] + [AIMessage(content="done")]
    monkeypatch.setattr(harness, "create_chat_model", lambda _: OfflineModel(responses=responses))
    # 执行首次核验，再保存文件内容用于检查查询是否产生写入。
    first = service.send_message(task_id, root=root)
    # 真实中间件应为每个工具保存相同调用标识的开始与终止事件。
    event_path = root / task_id / "runs" / first["manifest"]["run_id"] / "events.jsonl"
    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    started = [e["payload"]["call_id"] for e in events if e["type"] == "tool_started"]
    finished = [e["payload"]["call_id"] for e in events if e["type"] == "tool_finished"]
    assert started and started == finished
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    before = {p: p.read_bytes() for p in (root / task_id).rglob("*") if p.is_file()}
    assert (
        service.get_task_status(task_id, root=root)["adc_records"]["constraint_result"]
        == report["constraint_result"]
    )
    assert before == {p: p.read_bytes() for p in (root / task_id).rglob("*") if p.is_file()}
    monkeypatch.setattr(
        harness,
        "create_chat_model",
        lambda _: OfflineModel(responses=[AIMessage(content="original result remains unknown")]),
    )
    # 续聊同一任务，检查报告与需求保持一致、运行标识独立。
    second = service.send_message(task_id, "continue", root=root)
    assert first["report"] == second["report"]
    assert first["manifest"]["run_id"] != second["manifest"]["run_id"]
    assert second["manifest"]["message_count"] > first["manifest"]["message_count"]
    assert service.load_task(task_id, root=root) == meta

    # 人工审核走宿主入口，无需创建模型；版本和重复请求在业务库事务中校验。
    old_report = root / task_id / "runs" / first["manifest"]["run_id"] / "report.json"
    old_bytes = old_report.read_bytes()
    approval = dict(
        request_id="review-1",
        expected_revision=0,
        reviewer="测试审核人",
        decision="approve",
        note="合成资料：已核对范围、单位和条件",
    )
    reviewed = service.review_supply(task_id, root=root, **approval)
    assert reviewed["revision"] == 1
    assert reviewed["report"]["constraint_result"]["status"] == approved_status
    assert reviewed["previous_records"]["constraint_result"]["status"] == "unknown"
    assert service.review_supply(task_id, root=root, **approval) == reviewed
    assert len(service.get_task_status(task_id, root=root)["reviews"]) == 1
    # 新进程重放同一请求，只读出已提交版本，不生成第二条有效审核。
    child = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import json,sys; from pathlib import Path; "
            "from app.services.task_service import review_supply; "
            "print(json.dumps(review_supply(sys.argv[1], root=Path(sys.argv[2]), "
            "**json.loads(sys.argv[3])), ensure_ascii=False))",
            task_id,
            str(root),
            json.dumps(approval),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert json.loads(child.stdout) == reviewed
    with pytest.raises(ValueError, match="不同的审核内容"):
        service.review_supply(task_id, root=root, **{**approval, "note": "different"})
    with pytest.raises(ValueError, match="版本已变化"):
        service.review_supply(task_id, root=root, **{**approval, "request_id": "stale"})

    # 同任务写锁拒绝第二个操作，回滚后可继续；不同任务库不共享锁。
    with task_write(root / task_id):
        with pytest.raises(service.sqlite3.OperationalError, match="locked"):
            service.review_supply(task_id, root=root, **approval)
        other = service.create_task(root=root)
        with task_write(root / other["task_id"]):
            pass
    assert service.get_task_status(other["task_id"], root=root)["reviews"] == []
    with pytest.raises(ValueError, match="完成一次供电核验"):
        service.review_supply(other["task_id"], root=root, **approval)

    # 同线程续聊注入最新宿主审核；旧归档与创建快照保持不变。
    third = service.send_message(task_id, "说明审核后的结果", root=root)
    assert third["report"] == reviewed["report"]
    assert third["manifest"]["review_revision"] == 1
    assert old_report.read_bytes() == old_bytes
    assert service.load_task(task_id, root=root) == meta
    revoked = service.review_supply(
        task_id,
        root=root,
        request_id="review-2",
        expected_revision=1,
        reviewer="测试审核人",
        decision="reject",
        note="撤回确认，等待再次核对",
    )
    assert revoked["report"]["constraint_result"]["status"] == "unknown"
    fourth = service.send_message(task_id, "说明待确认项", root=root)
    assert fourth["report"] == revoked["report"]
    status = service.get_task_status(task_id, root=root)
    before = {p: p.read_bytes() for p in (root / task_id).rglob("*") if p.is_file()}
    assert status["review_revision"] == 2
    assert service.get_task_status(task_id, root=root) == status
    assert before == {p: p.read_bytes() for p in (root / task_id).rglob("*") if p.is_file()}

    for changes in ({"decision": "yes"}, {"reviewer": " "}, {"expected_revision": True}):
        with pytest.raises(ValueError, match="审核需要"):
            service.review_supply(task_id, root=root, **{**approval, **changes})

    # 在原线程暂停，普通消息和错误恢复不得改变等待点。
    waiting = service.wait_for_input(task_id, root=root)
    pending_id = waiting["pending"][0]["id"]
    assert waiting["manifest"]["status"] == "waiting_input"
    assert service.get_task_status(task_id, root=root)["execution_status"] == "waiting_input"
    with pytest.raises(ValueError, match="普通消息不能恢复"):
        service.send_message(task_id, "同意", root=root)
    with pytest.raises(ValueError, match="中断 ID"):
        service.resume_task(task_id, "wrong", "同意", root=root)
    with pytest.raises(ValueError, match="先提交"):
        service.resume_task(task_id, pending_id, "同意", root=root)
    with pytest.raises(ValueError):
        service.resume_task(other["task_id"], pending_id, "同意", root=root)
    confirmed = service.review_supply(
        task_id,
        root=root,
        **{
            **approval,
            "request_id": "review-3",
            "expected_revision": 2,
        },
    )
    assert service.get_task_status(task_id, root=root)["pending"][0]["id"] == pending_id

    # 用 uv 启动独立进程，重建模型及业务对象后恢复同一检查点。
    worker = """
import json, sys
from pathlib import Path
from dataclasses import replace
sys.path.insert(0, "tests")
from test_adc_tools import make_case
from test_persistence import OfflineModel
from langchain_core.messages import AIMessage
import app.services.task_service as service
import app.agents.harness as harness
task_id, root, pending_id = sys.argv[1:]
root = Path(root)
meta = service.load_task(task_id, root=root)
_, document, _, _ = make_case()
document = replace(document, directory=Path(meta["source_pdf"]).parent,
                   sha256=meta["document_sha256"])
service.register_pdf = lambda *args: document
harness.load_settings = lambda: None
harness.create_chat_model = lambda _: OfflineModel(responses=[AIMessage(content="resumed")])
result = service.resume_task(task_id, pending_id, "review-3", root=root)
print(json.dumps(result))
"""
    completed = subprocess.run(
        [
            "uv",
            "--cache-dir",
            "../runtime/uv-cache",
            "run",
            "--offline",
            "--no-sync",
            "python",
            "-B",
            "-c",
            worker,
            task_id,
            str(root),
            pending_id,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    resumed = json.loads(completed.stdout)
    assert resumed["manifest"]["thread_id"] == task_id
    assert resumed["report"] == confirmed["report"]
    assert service.get_task_status(task_id, root=root)["pending"] == []
    with pytest.raises(ValueError, match="中断 ID"):
        service.resume_task(task_id, pending_id, "review-3", root=root)

    # 澄清使用独立等待事项，只记录答案，不更改固定需求。
    clarification = service.wait_for_input(
        task_id,
        root=root,
        kind="clarification",
        question="是否只解释供电结果？",
    )
    clarified = service.resume_task(
        task_id,
        clarification["pending"][0]["id"],
        "是",
        root=root,
    )
    assert clarified["report"] == confirmed["report"]
    assert service.load_task(task_id, root=root) == meta

    # 即使历史上成功，最近一次执行失败后也不能用旧产物接受新审核。
    def fail_agent(**kwargs):
        """
        作用：模拟创建 Agent 时的可控失败。
        输入：kwargs - Agent 创建参数。
        输出：无
        """
        raise RuntimeError("controlled failure before graph execution")

    monkeypatch.setattr(service, "create_workbench_agent", fail_agent)
    with pytest.raises(RuntimeError):
        service.send_message(task_id, "触发可控失败", root=root)
    with pytest.raises(ValueError, match="最新运行尚未通过验收"):
        service.review_supply(
            task_id,
            root=root,
            **{
                **approval,
                "request_id": "after-failure",
                "expected_revision": 3,
            },
        )
    assert len(service.get_task_status(task_id, root=root)["reviews"]) == 3


def test_invalid_task_id(tmp_path):
    """
    作用：应用服务自身拒绝路径穿越，不能只依赖 CLI 校验。
    输入：tmp_path - pytest 临时目录。
    输出：无
    """
    # 验证应用服务自身拒绝路径穿越，不依赖命令行保护。
    with pytest.raises(ValueError):
        service.load_task("../outside", root=tmp_path)
