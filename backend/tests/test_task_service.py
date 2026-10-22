"""验证应用服务的新建、只读查询与续聊，不调用远程模型。"""

import hashlib
import json
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


def test_create_query_and_continue(tmp_path, monkeypatch):
    """
    作用：真实图与 SQLite 执行两次，确认续聊保留输入且查询没有写入。
    输入：pytest 临时目录 tmp_path 和替换依赖的 monkeypatch。
    输出：无返回值；断言新建、查询和续聊符合预期。
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
    meta = service.create_task(root=tmp_path / "tasks")
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


def test_invalid_task_id(tmp_path):
    """
    作用：应用服务自身拒绝路径穿越，不能只依赖 CLI 校验。
    输入：pytest 临时目录 tmp_path。
    输出：无返回值；断言非法任务标识被拒绝。
    """
    # 验证应用服务自身拒绝路径穿越，不依赖命令行保护。
    with pytest.raises(ValueError):
        service.load_task("../outside", root=tmp_path)
