"""创建持久化 ADC 任务，或在原线程中继续已完成的任务。"""

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from deepagents.backends.utils import create_file_data, file_data_to_string
from langchain_core.messages import messages_to_dict
from langgraph.checkpoint.sqlite import SqliteSaver

from app.adc_tools import build_adc_tools
from app.config import BACKEND_DIR, RUNTIME_DIR
from app.contracts import AdcRequirements
from app.documents import register_pdf
from app.harness import create_workbench_agent
from app.persistence import PersistAdcRecords, restore_session, snapshot_session
from scripts.run_adc_agent import (
    SKILL_PATH,
    SOURCE_URL,
    save_json,
    successful_reads,
)

ROOT = RUNTIME_DIR / "persistent_tasks"


def initial_files(meta: dict, document) -> dict:
    """准备首次执行的虚拟输入。

    输入：固定任务信息和已登记文档。
    输出：StateBackend 文件数据。
    边界：续接时不能重新注入这些文件覆盖旧状态。
    """
    task = {
        "task_id": meta["task_id"],
        "requirements": meta["requirements"],
        "supply_reviewed_by_user": meta["reviewed"],
        "scope": "supply_voltage_only",
    }
    index = {
        "document_id": document.id,
        "document_version": document.version,
        "sha256": document.sha256,
        "source_url": document.source_url,
        "page_count": len(document.pages),
        "pages_directory": "/documents/pages/",
    }
    texts = {
        "/inputs/task.json": json.dumps(task, ensure_ascii=False, indent=2),
        "/documents/index.json": json.dumps(index, ensure_ascii=False, indent=2),
        SKILL_PATH: meta["skill_text"],
    }

    for number, text in enumerate(document.pages, start=1):
        texts[f"/documents/pages/{number:04d}.txt"] = text

    return {path: create_file_data(text) for path, text in texts.items()}


def main() -> None:
    """创建、查看或续接任务。

    输入：新任务参数，或已有 --task 标识。
    输出：固定任务身份、SQLite 检查点和独立运行记录。
    边界：单任务单进程；暂不恢复未完成节点或人工中断。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--voltage")
    parser.add_argument("--reviewed", action="store_true")
    parser.add_argument("--pdf", type=Path)
    parser.add_argument(
        "--message",
        default="复述原供电判定并说明未核验范围，不修改原任务。",
    )
    args = parser.parse_args()

    if args.show and not args.task:
        parser.error("--show 需要 --task。")

    if args.task and (args.voltage is not None or args.pdf is not None or args.reviewed):
        parser.error("续接不修改需求、文档或审核状态；这些变化需新建任务。")

    if args.task:
        if not re.fullmatch(r"task-[0-9a-f]{32}", args.task):
            parser.error("任务 ID 格式不正确。")

        task_dir = ROOT / args.task
        meta = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))

        if meta["task_id"] != args.task or meta["thread_id"] != args.task:
            raise ValueError("任务身份不一致。")

    else:
        task_id = f"task-{uuid4().hex}"
        requirements = AdcRequirements(
            id=f"req-{task_id}",
            candidate_parts=["ADS1115"],
            supply_voltage_v=args.voltage or "3.3",
            interface="I2C",
            min_resolution_bits=12,
            required_channels=4,
            min_samples_per_channel_sps="1000",
        )
        document = register_pdf(
            args.pdf or RUNTIME_DIR / "raw" / "ads1115.pdf",
            SOURCE_URL,
            RUNTIME_DIR / "documents",
        )

        task_dir = ROOT / task_id
        task_dir.mkdir(parents=True, exist_ok=False)

        meta = {
            "task_id": task_id,
            "thread_id": task_id,
            "requirements": requirements.model_dump(mode="json"),
            "reviewed": args.reviewed,
            "document_sha256": document.sha256,
            "source_pdf": str((document.directory / "source.pdf").resolve()),
            "source_url": SOURCE_URL,
            "skill_text": (
                BACKEND_DIR / "skills" / "adc-supply-verification" / "SKILL.md"
            ).read_text(encoding="utf-8"),
        }
        save_json(task_dir / "task.json", meta)

    database = task_dir / "checkpoints.sqlite"
    config = {
        "configurable": {
            "thread_id": meta["thread_id"],
        },
        "recursion_limit": 40,
    }

    if args.show:
        checkpoint = None
        if database.exists():
            with SqliteSaver.from_conn_string(str(database)) as saver:
                checkpoint = saver.get_tuple(config)

        values = checkpoint.checkpoint["channel_values"] if checkpoint else {}
        print(
            json.dumps(
                {
                    "task_id": meta["task_id"],
                    "thread_id": meta["thread_id"],
                    "requirements": meta["requirements"],
                    "checkpoint_exists": checkpoint is not None,
                    "adc_records": values.get("adc_records", {}),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    # 续接绑定归档原件，而不是重新下载可能已更新的手册。
    source_pdf = Path(meta["source_pdf"])
    digest = hashlib.sha256(source_pdf.read_bytes()).hexdigest()
    if digest != meta["document_sha256"]:
        raise ValueError("归档 PDF 与任务绑定的版本不一致。")

    document = register_pdf(
        source_pdf,
        meta["source_url"],
        RUNTIME_DIR / "documents",
    )
    requirements = AdcRequirements.model_validate(meta["requirements"])
    tools, session = build_adc_tools(
        requirements,
        document,
        reviewed=meta["reviewed"],
    )

    run_id = f"run-{uuid4().hex}"
    run_dir = task_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    manifest = {
        "task_id": meta["task_id"],
        "thread_id": meta["thread_id"],
        "run_id": run_id,
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
    }
    save_json(run_dir / "manifest.json", manifest)

    print(f"task_id：{meta['task_id']}\nthread_id：{meta['thread_id']}\nrun_id：{run_id}")

    try:
        # 连接必须在图执行和状态读取期间保持打开。
        with SqliteSaver.from_conn_string(str(database)) as saver:
            agent = create_workbench_agent(
                tools=tools,
                skills=["/skills/"],
                checkpointer=saver,
                middleware=[PersistAdcRecords(session)],
                system_prompt=(
                    "你是器件核验工作台 Agent，"
                    "使用 ADC Skill 完成供电单项核验。"
                    "业务判定以工具结果为准；其他需求未核验。"
                    "同线程续聊保留原任务数据；不使用子 Agent。"
                ),
            )

            previous = agent.get_state(config)

            if args.task:
                if not previous.values:
                    raise ValueError("找不到该任务的检查点，不能作为已完成任务续接。")
                if previous.next:
                    raise ValueError("任务存在未完成节点；本轮暂不自动恢复，请保留该任务。")

                restore_session(previous.values, session)
                payload = {
                    "messages": [
                        {
                            "role": "user",
                            "content": args.message,
                        }
                    ]
                }
                baseline = previous.values.get("files", {})

            else:
                baseline = initial_files(meta, document)
                payload = {
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "使用 adc-supply-verification Skill "
                                "读取 /inputs/task.json，"
                                "组织供电核验并生成规定的报告。"
                            ),
                        }
                    ],
                    "files": baseline,
                }

            protected = {
                path: file_data_to_string(data)
                for path, data in baseline.items()
                if path.startswith(("/inputs/", "/documents/", "/skills/"))
            }

            agent.invoke(payload, config=config)

            saved = agent.get_state(config)
            state = saved.values
            restore_session(state, session)

            save_json(
                run_dir / "messages.json",
                messages_to_dict(state.get("messages", [])),
            )
            save_json(
                run_dir / "business_records.json",
                snapshot_session(session),
            )
            save_json(
                run_dir / "todos.json",
                state.get("todos", []),
            )

            if saved.next or session.result is None:
                raise ValueError("任务尚未完成，或没有持久化业务判定。")

            if not state.get("todos") or any(
                item.get("status") != "completed" for item in state["todos"]
            ):
                raise ValueError("任务计划未全部完成。")

            if not args.task:
                reads = successful_reads(state.get("messages", []))
                required = {
                    SKILL_PATH,
                    "/inputs/task.json",
                    "/documents/index.json",
                    (f"/documents/pages/{session.evidence.pdf_page:04d}.txt"),
                }
                if not required.issubset(reads):
                    missing = sorted(required - reads)
                    raise ValueError(f"首次执行缺少必要读取：{missing}")

            files = state.get("files", {})
            for path, text in protected.items():
                if path not in files or file_data_to_string(files[path]) != text:
                    raise ValueError(f"受保护输入发生变化：{path}")

            report = json.loads(file_data_to_string(files["/outputs/report.json"]))
            expected = {
                "task_id": meta["task_id"],
                "scope": "supply_voltage_only",
                "constraint_result": session.result.model_dump(mode="json"),
            }

            if report != expected:
                raise ValueError("报告与已保存的业务判定不一致。")

            save_json(run_dir / "report.json", report)
            manifest["message_count"] = len(state.get("messages", []))
            manifest["status"] = "passed"

            print(f"持久化任务执行通过；供电判定：{session.result.status}")

    except (Exception, KeyboardInterrupt) as exc:
        manifest["status"] = "failed"
        manifest["error_type"] = type(exc).__name__
        raise

    finally:
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        save_json(run_dir / "manifest.json", manifest)


if __name__ == "__main__":
    main()
