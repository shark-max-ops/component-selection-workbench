"""在验证 Agent 是否按计划完成了虚拟文件读写任务，
并且没有篡改输入、没有丢失业务判定结果；
所有运行证据归档到独立目录中
"""

import argparse
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from deepagents.backends.utils import (
    create_file_data,
    file_data_to_string,
)
from langchain_core.messages import ToolMessage, messages_to_dict

from app.config import RUNTIME_DIR
from app.contracts import (
    AdcRequirements,
    ConstraintResult,
    Evidence,
    ParameterFact,
)
from app.harness import create_workbench_agent


def save_json(path: Path, data: object) -> None:
    """保存 JSON。

    输入：目标路径和可序列化对象。
    输出：UTF-8 文件。
    边界：调用方负责创建父目录。
    """
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def validate_snapshot(payload: dict) -> dict:
    """检查源快照结构和本轮引用关系。

    输入：供电脚本保存的 JSON 对象。
    输出：规范化后的四类业务对象。
    边界：不重新读取 PDF，也不替代原来的供电核验规则。
    """
    requirements = AdcRequirements.model_validate(payload["requirements"])
    evidence = Evidence.model_validate(payload["evidence"])
    fact = ParameterFact.model_validate(payload["parameter_fact"])
    result = ConstraintResult.model_validate(payload["constraint_result"])

    if (
        result.requirement_id != requirements.id
        or result.part_number != fact.part_number
        or fact.part_number not in requirements.candidate_parts
        or result.parameter != "supply_voltage"
        or fact.parameter != "supply_voltage"
        or result.fact_ids != [fact.id]
        or result.evidence_ids != [evidence.id]
        or fact.evidence_ids != [evidence.id]
    ):
        raise ValueError("源快照的型号、需求或引用关系不一致。")

    return {
        "requirements": requirements.model_dump(mode="json"),
        "evidence": evidence.model_dump(mode="json"),
        "parameter_fact": fact.model_dump(mode="json"),
        "constraint_result": result.model_dump(mode="json"),
    }


def main() -> None:
    """执行一次有边界的 Harness 检查。
    输入：--source 指定的既有供电核验 JSON。
    输出：独立目录中的输入、消息、虚拟文件、报告和运行摘要。
    边界：会调用真实模型；失败时只保存已经返回给宿主的数据。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    args = parser.parse_args()

    payload = json.loads(args.source.read_text(encoding="utf-8"))
    snapshot = validate_snapshot(payload)

    task_id = f"harness-{uuid4().hex}"

    # 随机标记只放在输入文件中，用于检查报告确实取得了本次输入。
    token = secrets.token_hex(16)
    snapshot["harness_task"] = {
        "task_id": task_id,
        "verification_token": token,
    }

    input_text = json.dumps(
        snapshot,
        ensure_ascii=False,
        indent=2,
    )

    expected = {
        "task_id": task_id,
        "verification_token": token,
        "scope": "supply_voltage_only",
        "constraint_result": snapshot["constraint_result"],
    }

    run_dir = RUNTIME_DIR / "harness_runs" / task_id
    run_dir.mkdir(parents=True, exist_ok=False)

    save_json(run_dir / "input_snapshot.json", snapshot)

    manifest = {
        "task_id": task_id,
        "source": str(args.source.resolve()),
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "recursion_limit": 24,
        "backend": "default StateBackend",
        "checkpointing": False,
    }
    save_json(run_dir / "manifest.json", manifest)

    print(f"任务目录：{run_dir}")

    try:
        agent = create_workbench_agent()

        state = agent.invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": ("读取虚拟输入文件，按要求建立计划并生成报告。"),
                    }
                ],
                "files": {
                    "/inputs/snapshot.json": create_file_data(input_text),
                },
            },
            config={"recursion_limit": 24},
        )

        messages = state.get("messages", [])
        files = state.get("files", {})
        todos = state.get("todos", [])

        # 先保存实际返回内容，验证失败时也能回看。
        save_json(
            run_dir / "messages.json",
            messages_to_dict(messages),
        )
        save_json(run_dir / "workspace.json", files)
        save_json(run_dir / "todos.json", todos)

        successful_tools = [
            message.name
            for message in messages
            if isinstance(message, ToolMessage) and message.status == "success"
        ]
        manifest["successful_tools"] = successful_tools

        required = {
            "read_file",
            "write_file",
            "write_todos",
        }

        if not required.issubset(set(successful_tools)):
            raise ValueError("缺少成功的文件读取、写入或计划工具调用。")

        if "/outputs/report.json" not in files:
            raise ValueError("Agent 没有生成指定报告文件。")

        if "/inputs/snapshot.json" not in files:
            raise ValueError("Agent 删除了虚拟输入文件。")

        saved_input = file_data_to_string(files["/inputs/snapshot.json"])
        if saved_input != input_text:
            raise ValueError("Agent 修改了虚拟输入文件。")

        report_text = file_data_to_string(files["/outputs/report.json"])
        report = json.loads(report_text)

        # 比较实际文件内容，不以模型声称“已完成”为通过依据。
        if report != expected:
            raise ValueError("报告没有完整保留任务标记或原始核验结果。")

        if not todos or any(item.get("status") != "completed" for item in todos):
            raise ValueError("任务计划未全部完成。")

        save_json(run_dir / "report.json", report)
        manifest["status"] = "passed"

        print("Harness 检查通过：计划、文件读写和判定保留均通过。")
        print(
            f"业务判定：{report['constraint_result']['status']}"  # 任务状态和业务状态分开
        )

    except (Exception, KeyboardInterrupt) as exc:
        manifest["status"] = "failed"
        manifest["error_type"] = type(exc).__name__
        raise

    finally:
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        save_json(run_dir / "manifest.json", manifest)


if __name__ == "__main__":
    main()
