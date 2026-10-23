"""用两个独立 Python 进程验证状态恢复，不调用模型服务。"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.backends.utils import create_file_data, file_data_to_string
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver

import app.agents.harness as harness
from app.agents.state_sync import PersistAdcRecords, merge_adc_records, restore_session
from app.contracts import ConstraintResult, Evidence, NumericValue, ParameterFact


class OfflineModel(FakeMessagesListChatModel):
    """
    作用：提供固定响应以验证真实图和检查点。
    输入：responses - 固定模型响应列表；其他父类模型字段。
    输出：OfflineModel 离线模型对象。
    """

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """
        作用：接受工具绑定并保留测试响应游标。
        输入：tools - 工具列表；tool_choice - 工具选择策略；kwargs - 其他绑定选项。
        输出：当前离线模型对象。
        """
        return self

    def get_num_tokens_from_messages(self, messages, tools=None):
        """
        作用：提供不依赖外部 tokenizer 的测试计数。
        输入：messages - 消息列表；tools - 可选工具列表。
        输出：int，测试用的粗略 token 数量。
        """
        return 1 + sum(len(str(item.content)) for item in messages) // 4


def child_run(database: str, phase: str) -> None:
    """
    作用：在子进程中保存或续接固定线程。
    输入：database - 检查点数据库路径；phase - write 或 read 阶段。
    输出：无
    """
    # 每个进程从空业务对象开始，恢复能力由后续图执行验证。
    session = SimpleNamespace(
        evidence=None,
        fact=None,
        result=None,
    )

    @tool
    def read_adc_supply_evidence() -> dict:
        """
        作用：生成供状态持久化测试使用的合成证据和参数。
        输入：无
        输出：dict，包含 ok 成功标记。
        """
        session.evidence = Evidence(
            id="ev-test",
            document_id="doc-test",
            document_version="synthetic",
            document_sha256="1" * 64,
            source_url="https://example.com/test.pdf",
            pdf_page=1,
            quote="Synthetic evidence.",
        )
        session.fact = ParameterFact(
            id="fact-test",
            part_number="TEST_ADC",
            parameter="supply_voltage",
            value=NumericValue(
                lower="2",
                upper="5.5",
                unit="V",
            ),
            conditions="Synthetic conditions.",
            evidence_ids=["ev-test"],
        )
        return {"ok": True}

    @tool
    def check_adc_supply() -> dict:
        """
        作用：检查业务对象已恢复并生成合成判定。
        输入：无
        输出：dict，包含 ok 成功标记。
        """
        assert session.evidence is not None
        assert session.fact is not None

        session.result = ConstraintResult(
            id="result-test",
            requirement_id="req-test",
            part_number="TEST_ADC",
            parameter="supply_voltage",
            status="unknown",
            reason="Synthetic result.",
            fact_ids=["fact-test"],
            evidence_ids=["ev-test"],
            unresolved=["Not reviewed."],
        )
        return {"ok": True}

    # 写入阶段读取证据，续接阶段只核验已恢复的业务对象。
    names = [
        "write_todos",
        "read_adc_supply_evidence",
        "check_adc_supply",
    ]
    if phase == "read":
        # 第二进程不重新执行证据读取工具。
        names = ["check_adc_supply"]

    # 构造固定工具请求，实际图与检查点继续使用框架实现。
    responses = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": name,
                    "args": (
                        {
                            "todos": [
                                {
                                    "content": "Synthetic task",
                                    "status": "completed",
                                }
                            ]
                        }
                        if name == "write_todos"
                        else {}
                    ),
                    "id": f"c-{n}",
                }
            ],
        )
        for n, name in enumerate(names)
    ] + [AIMessage(content="Done.")]

    model = OfflineModel(responses=responses)
    harness.load_settings = lambda: None
    harness.create_chat_model = lambda _: model

    config = {
        "configurable": {"thread_id": "task-test"},
        "recursion_limit": 40,
    }

    # 两个阶段绑定相同数据库与线程，保持文件和业务状态连续。
    with SqliteSaver.from_conn_string(database) as saver:
        agent = harness.create_workbench_agent(
            tools=[
                read_adc_supply_evidence,
                check_adc_supply,
            ],
            checkpointer=saver,
            middleware=[PersistAdcRecords(session)],
        )

        if phase == "write":
            agent.invoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": "First run.",
                        }
                    ],
                    "files": {"/inputs/marker.txt": create_file_data("saved-marker")},
                },
                config=config,
            )
        else:
            previous = agent.get_state(config)

            assert not previous.next
            assert (
                file_data_to_string(previous.values["files"]["/inputs/marker.txt"])
                == "saved-marker"
            )
            assert previous.values["todos"][0]["status"] == "completed"

            # 新创建的运行时对象还没有恢复。
            assert session.evidence is None

            # 其他线程不能取得当前线程状态。
            other = agent.get_state({"configurable": {"thread_id": "other"}})
            assert not other.values

            # 不重新传入 files、todos 或业务记录。
            state = agent.invoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": "Continue.",
                        }
                    ]
                },
                config=config,
            )

            assert session.evidence.id == "ev-test"
            assert session.result.id == "result-test"
            assert all(
                item.status == "success"
                for item in state["messages"]
                if isinstance(item, ToolMessage)
            )

        # 输出结构化摘要，供父进程比较结果和消息历史。
        saved = agent.get_state(config)
        print(
            json.dumps(
                {
                    "result_id": (saved.values["adc_records"]["constraint_result"]["id"]),
                    "message_count": len(saved.values["messages"]),
                }
            )
        )


def test_two_processes_restore_state_and_business(tmp_path):
    """
    作用：第一进程退出后，第二进程找回图状态并恢复业务对象。
    输入：tmp_path - pytest 临时目录。
    输出：无
    """
    # 子进程共享项目代码与数据库，关闭外部追踪并固定输出编码。
    env = os.environ | {
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "LANGSMITH_TRACING": "false",
        "LANGCHAIN_TRACING_V2": "false",
        "PYTHONUTF8": "1",
    }

    # 先让写入进程正常退出，再由独立进程续接同一线程。
    results = []
    for phase in ["write", "read"]:
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                str(tmp_path / "state.sqlite"),
                phase,
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        results.append(json.loads(completed.stdout))

    assert results[0]["result_id"] == results[1]["result_id"]
    assert results[1]["message_count"] > results[0]["message_count"]


def test_repeated_records_are_idempotent():
    """
    作用：重复写入不追加条目，空值不抹掉已有记录。
    输入：无
    输出：无
    """
    record = {"evidence": {"id": "ev-test"}}

    assert merge_adc_records(record, record) == record
    assert merge_adc_records(record, {"evidence": None}) == record


def test_conflicting_records_are_rejected():
    """
    作用：当前任务输入固定，不允许静默替换已保存记录。
    输入：无
    输出：无
    """
    with pytest.raises(ValueError):
        merge_adc_records(
            {"evidence": {"id": "one"}},
            {"evidence": {"id": "two"}},
        )


def test_empty_state_restores_nothing():
    """
    作用：新线程没有业务记录时保持空对象。
    输入：无
    输出：无
    """
    session = SimpleNamespace(
        evidence=None,
        fact=None,
        result=None,
    )
    restore_session({"adc_records": {}}, session)

    assert session.evidence is None
    assert session.fact is None
    assert session.result is None


if __name__ == "__main__":
    child_run(sys.argv[1], sys.argv[2])
