"""创建持久化 ADC 任务，或在原线程中继续已完成的任务。"""

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from deepagents.backends.utils import create_file_data, file_data_to_string
from langchain_core.messages import messages_to_dict
from langgraph.checkpoint.sqlite import SqliteSaver

from app.adc.constants import SKILL_PATH, SOURCE_URL
from app.agents.events import RunEvents
from app.agents.harness import create_workbench_agent
from app.agents.messages import successful_reads
from app.agents.state_sync import PersistAdcRecords, restore_session, snapshot_session
from app.agents.tools import build_adc_tools
from app.config import BACKEND_DIR, RUNTIME_DIR
from app.contracts import AdcRequirements
from app.infrastructure.documents import register_pdf
from app.infrastructure.json_store import save_json

ROOT = RUNTIME_DIR / "persistent_tasks"


def initial_files(meta: dict, document) -> dict:
    """
    作用：准备首次执行的虚拟输入。
    输入：固定任务元信息 meta 和已登记文档 document。
    输出：供首次执行注入的虚拟文件字典。
    """
    # 整理固定需求与审核声明，明确本轮只核验供电。
    task = {
        "task_id": meta["task_id"],
        "requirements": meta["requirements"],
        "supply_reviewed_by_user": meta["reviewed"],
        "scope": "supply_voltage_only",
    }
    # 整理文档版本、哈希和页面位置，便于回溯原文。
    index = {
        "document_id": document.id,
        "document_version": document.version,
        "sha256": document.sha256,
        "source_url": document.source_url,
        "page_count": len(document.pages),
        "pages_directory": "/documents/pages/",
    }
    # 把需求、文档索引和 Skill 转成虚拟文件文本。
    texts = {
        "/inputs/task.json": json.dumps(task, ensure_ascii=False, indent=2),
        "/documents/index.json": json.dumps(index, ensure_ascii=False, indent=2),
        SKILL_PATH: meta["skill_text"],
    }

    # 按从 1 开始的页序保存文本，保持证据页码一致。
    for number, text in enumerate(document.pages, start=1):
        texts[f"/documents/pages/{number:04d}.txt"] = text

    return {path: create_file_data(text) for path, text in texts.items()}


def load_task(task_id: str, *, root: Path = ROOT) -> dict:
    """
    作用：读取固定任务元信息，校验任务与线程身份，不执行模型。
    输入：任务标识 task_id 和任务根目录 root。
    输出：经过身份校验的任务元信息字典。
    """
    # 先验证任务标识，非法路径不会进入后面的文件读取。
    if not re.fullmatch(r"task-[0-9a-f]{32}", task_id):
        raise ValueError("任务 ID 格式不正确。")

    # 定位当前任务目录，避免混用其他任务的数据。
    task_dir = root / task_id
    meta = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))

    # 核对文件内的任务与线程身份，防止打开错误记录。
    if meta["task_id"] != task_id or meta["thread_id"] != task_id:
        raise ValueError("任务身份不一致。")

    return meta


def create_task(
    *, voltage: str = "3.3", reviewed: bool = False, pdf: Path | None = None, root: Path = ROOT
) -> dict:
    """
    作用：登记资料并固定需求快照；创建任务不调用模型。
    输入：供电电压、人工审核声明、PDF 路径及任务根目录。
    输出：已保存的任务元信息字典，不调用模型。
    """
    # 生成独立任务身份，随后先校验需求再登记资料。
    task_id = f"task-{uuid4().hex}"
    requirements = AdcRequirements(
        id=f"req-{task_id}",
        candidate_parts=["ADS1115"],
        supply_voltage_v=voltage or "3.3",
        interface="I2C",
        min_resolution_bits=12,
        required_channels=4,
        min_samples_per_channel_sps="1000",
    )
    # 登记并读取绑定的资料，复用现有文档处理逻辑。
    document = register_pdf(
        pdf or RUNTIME_DIR / "raw" / "ads1115.pdf",
        SOURCE_URL,
        RUNTIME_DIR / "documents",
    )

    # 定位当前任务目录，避免混用其他任务的数据。
    task_dir = root / task_id
    task_dir.mkdir(parents=True, exist_ok=False)

    # 保存固定快照，后续续聊继续使用这里的需求和资料版本。
    meta = {
        "task_id": task_id,
        "thread_id": task_id,
        "requirements": requirements.model_dump(mode="json"),
        "reviewed": reviewed,
        "document_sha256": document.sha256,
        "source_pdf": str((document.directory / "source.pdf").resolve()),
        "source_url": SOURCE_URL,
        "skill_text": (BACKEND_DIR / "skills" / "adc-supply-verification" / "SKILL.md").read_text(
            encoding="utf-8"
        ),
    }
    # 保存运行产物或清单，供后续查询和复核。
    save_json(task_dir / "task.json", meta)

    return meta


def get_task_status(task_id: str, *, root: Path = ROOT) -> dict:
    """
    作用：只读查询检查点与业务记录，不创建 Agent 或运行归档。
    输入：任务标识 task_id 和任务根目录 root。
    输出：需求、检查点存在标记和已有业务记录。
    """
    meta = load_task(task_id, root=root)
    # 定位当前任务目录，避免混用其他任务的数据。
    task_dir = root / task_id
    database = task_dir / "checkpoints.sqlite"
    # 使用固定线程定位检查点，并限制图执行步数。
    config = {
        "configurable": {
            "thread_id": meta["thread_id"],
        },
        "recursion_limit": 40,
    }

    # 没有检查点时返回空记录，查询不创建新任务。
    checkpoint = None
    if database.exists():
        # 单任务单进程边界内，无 WAL 时按不可变快照读取，避免生成 shm/wal 文件。
        # 已有 WAL 时必须读取其中的最新提交，不能使用 immutable 忽略它。
        options = "?mode=ro"
        if not database.with_name(database.name + "-wal").exists():
            options += "&immutable=1"
        with sqlite3.connect(database.resolve().as_uri() + options, uri=True) as connection:
            saver = SqliteSaver(connection)
            # 已有数据库无需初始化；只读查询不能触发建表。
            saver.is_setup = True
            checkpoint = saver.get_tuple(config)

    # 整理查询结果，保留原有返回字段。
    values = checkpoint.checkpoint["channel_values"] if checkpoint else {}
    return {
        "task_id": meta["task_id"],
        "thread_id": meta["thread_id"],
        "requirements": meta["requirements"],
        "checkpoint_exists": checkpoint is not None,
        "adc_records": values.get("adc_records", {}),
    }


def send_message(
    task_id: str,
    message: str = "复述原供电判定并说明未核验范围，不修改原任务。",
    *,
    root: Path = ROOT,
    on_event=None,
) -> dict:
    """
    作用：执行新任务或续聊已完成任务，校验并归档；仅支持单任务单进程。
    输入：任务标识、用户消息、任务根目录和可选事件展示回调。
    输出：包含运行清单 manifest 和结构化报告 report 的字典。
    """
    meta = load_task(task_id, root=root)
    # 定位当前任务目录，避免混用其他任务的数据。
    task_dir = root / task_id
    database = task_dir / "checkpoints.sqlite"
    # 使用固定线程定位检查点，并限制图执行步数。
    config = {
        "configurable": {
            "thread_id": meta["thread_id"],
        },
        "recursion_limit": 40,
    }

    # 区分首次执行和续聊，缺失检查点的旧运行不能重新初始化。
    first_run = not database.exists()
    if first_run and (task_dir / "runs").exists():
        raise ValueError("已有运行记录但检查点缺失，不能重新注入任务输入。")
    # 续接绑定归档原件，而不是重新下载可能已更新的手册。
    source_pdf = Path(meta["source_pdf"])
    digest = hashlib.sha256(source_pdf.read_bytes()).hexdigest()
    if digest != meta["document_sha256"]:
        raise ValueError("归档 PDF 与任务绑定的版本不一致。")

    # 登记并读取绑定的资料，复用现有文档处理逻辑。
    document = register_pdf(
        source_pdf,
        meta["source_url"],
        RUNTIME_DIR / "documents",
    )
    # 重新校验保存的需求，再装配绑定当前任务的业务工具。
    requirements = AdcRequirements.model_validate(meta["requirements"])
    tools, session = build_adc_tools(
        requirements,
        document,
        reviewed=meta["reviewed"],
    )

    # 每次执行建立独立归档，保留历史运行记录。
    run_id = f"run-{uuid4().hex}"
    run_dir = task_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    # 先记录运行开始状态，失败时也能定位本次执行。
    manifest = {
        "task_id": meta["task_id"],
        "thread_id": meta["thread_id"],
        "run_id": run_id,
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
    }
    # 保存运行产物或清单，供后续查询和复核。
    save_json(run_dir / "manifest.json", manifest)
    # 事件绑定本次 run，终端关闭后仍可查看工具执行记录。
    events = RunEvents(task_id, run_id, run_dir / "events.jsonl", on_event)

    try:
        events.emit("run_started")
        # 连接必须在图执行和状态读取期间保持打开。
        with SqliteSaver.from_conn_string(str(database)) as saver:
            agent = create_workbench_agent(
                tools=tools,
                skills=["/skills/"],
                checkpointer=saver,
                middleware=[events, PersistAdcRecords(session)],
                system_prompt=(
                    "你是器件核验工作台 Agent，"
                    "使用 ADC Skill 完成供电单项核验。"
                    "业务判定以工具结果为准；其他需求未核验。"
                    "同线程续聊保留原任务数据；不使用子 Agent。"
                ),
            )

            # 读取原线程，判断是否可以作为已完成任务续聊。
            previous = agent.get_state(config)

            if not first_run:
                if not previous.values:
                    raise ValueError("找不到该任务的检查点，不能作为已完成任务续接。")
                if previous.next:
                    raise ValueError("任务存在未完成节点；本轮暂不自动恢复，请保留该任务。")

                # 恢复业务对象；续聊只追加消息，不重新注入输入文件。
                restore_session(previous.values, session)
                payload = {
                    "messages": [
                        {
                            "role": "user",
                            "content": message,
                        }
                    ]
                }
                baseline = previous.values.get("files", {})

            else:
                # 首次执行才注入需求、资料和 Skill。
                baseline = initial_files(meta, document)
                payload = {
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "使用 adc-supply-verification Skill "
                                "读取 /inputs/task.json，"
                                "组织供电核验并生成规定的报告。用户消息：" + message
                            ),
                        }
                    ],
                    "files": baseline,
                }

            # 记住受保护输入，执行结束后检查是否被改写。
            protected = {
                path: file_data_to_string(data)
                for path, data in baseline.items()
                if path.startswith(("/inputs/", "/documents/", "/skills/"))
            }

            # 执行实际图流程，模型通过受约束的工具完成调查。
            agent.invoke(payload, config=config)

            # 以最终保存状态恢复业务对象，避免只信任模型回复。
            saved = agent.get_state(config)
            state = saved.values
            restore_session(state, session)

            # 保存运行产物或清单，供后续查询和复核。
            save_json(
                run_dir / "messages.json",
                messages_to_dict(state.get("messages", [])),
            )
            # 保存运行产物或清单，供后续查询和复核。
            save_json(
                run_dir / "business_records.json",
                snapshot_session(session),
            )
            # 保存运行产物或清单，供后续查询和复核。
            save_json(
                run_dir / "todos.json",
                state.get("todos", []),
            )

            # 确认执行已结束且存在业务判定，再验收报告。
            if saved.next or session.result is None:
                raise ValueError("任务尚未完成，或没有持久化业务判定。")

            # 核对计划是否全部完成，未完成不能算执行通过。
            if not state.get("todos") or any(
                item.get("status") != "completed" for item in state["todos"]
            ):
                raise ValueError("任务计划未全部完成。")

            # 首次执行还要确认必要资料确实被工具读取。
            if first_run:
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

            # 核对原始输入保持不变，再读取结构化报告。
            files = state.get("files", {})
            for path, text in protected.items():
                if path not in files or file_data_to_string(files[path]) != text:
                    raise ValueError(f"受保护输入发生变化：{path}")

            # 将报告与程序核验结果逐字段比较，防止模型改写结论。
            report = json.loads(file_data_to_string(files["/outputs/report.json"]))
            expected = {
                "task_id": meta["task_id"],
                "scope": "supply_voltage_only",
                "constraint_result": session.result.model_dump(mode="json"),
            }

            if report != expected:
                raise ValueError("报告与已保存的业务判定不一致。")

            # 保存运行产物或清单，供后续查询和复核。
            save_json(run_dir / "report.json", report)
            manifest["message_count"] = len(state.get("messages", []))
            manifest["status"] = "passed"
            # 自然语言仅用于解释，结构化报告仍是程序核验结果。
            replies = [
                item.content
                for item in state.get("messages", [])
                if item.type == "ai" and not getattr(item, "tool_calls", None)
            ]
            return {"manifest": manifest, "report": report, "reply": replies[-1] if replies else ""}

    # 记录执行失败类型并继续抛出，避免把错误当作器件不满足。
    except (Exception, KeyboardInterrupt) as exc:
        manifest["status"] = "failed"
        manifest["error_type"] = type(exc).__name__
        raise

    # 无论成功还是失败，都记录结束时间和最终清单。
    finally:
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        events.emit("run_finished", status=manifest["status"])
        # 保存运行产物或清单，供后续查询和复核。
        save_json(run_dir / "manifest.json", manifest)
