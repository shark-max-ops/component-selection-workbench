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
from langgraph.types import Command, Overwrite

from app.adc.constants import SKILL_PATH, SOURCE_URL
from app.adc.extractors import extract_ads1115_supply
from app.adc.rules import check_supply_voltage
from app.agents.events import RunEvents
from app.agents.harness import create_workbench_agent
from app.agents.human_input import AwaitHumanInput, HumanInputError, pending_interrupts
from app.agents.messages import successful_reads
from app.agents.state_sync import PersistAdcRecords, restore_session, snapshot_session
from app.agents.tools import build_adc_tools
from app.config import BACKEND_DIR, RUNTIME_DIR
from app.contracts import AdcRequirements
from app.infrastructure.documents import register_pdf
from app.infrastructure.json_store import save_json
from app.services.reviews import read_reviews, task_write

ROOT = RUNTIME_DIR / "persistent_tasks"


def initial_files(meta: dict, document) -> dict:
    """
    作用：准备首次执行所需的虚拟输入文件。
    输入：meta - 任务元信息；document - 已登记文档。
    输出：dict，虚拟路径到框架文件数据的映射。
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
    作用：读取任务元信息并校验任务与线程身份。
    输入：task_id - 任务 ID；root - 任务根目录。
    输出：dict，身份校验通过的任务元信息。
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
    作用：登记资料并保存固定需求和任务快照。
    输入：voltage - 供电电压；reviewed - 初始审核状态；pdf - 可选 PDF 路径；root - 任务根目录。
    输出：dict，新任务元信息。
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
    save_json(task_dir / "task.json", meta)

    return meta


def get_task_status(task_id: str, *, root: Path = ROOT) -> dict:
    """
    作用：只读查询检查点、最新业务结果和审核历史。
    输入：task_id - 任务 ID；root - 任务根目录。
    输出：dict，需求、检查点标记、业务记录、审核版本及历史。
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
    reviews = read_reviews(task_dir)
    pending = pending_interrupts(checkpoint)
    # 运行失败与器件判定分开；真实中断优先显示为等待输入。
    runs = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (task_dir / "runs").glob("*/manifest.json")
    ]
    latest_run = max(runs, key=lambda item: item["started_at"], default={})
    return {
        "task_id": meta["task_id"],
        "thread_id": meta["thread_id"],
        "requirements": meta["requirements"],
        "checkpoint_exists": checkpoint is not None,
        "adc_records": reviews[-1]["records"] if reviews else values.get("adc_records", {}),
        "review_revision": reviews[-1]["revision"] if reviews else 0,
        "reviews": reviews,
        "pending": pending,
        "execution_status": "waiting_input"
        if pending
        else (
            latest_run["status"]
            if latest_run.get("status") in {"running", "failed"}
            else "unfinished"
            if any(k.startswith("branch:to:") and v for k, v in values.items())
            else "completed"
            if checkpoint
            else "created"
        ),
        "human_response": values.get("human_response"),
    }


def review_supply(
    task_id: str,
    *,
    request_id: str,
    expected_revision: int,
    reviewer: str,
    decision: str,
    note: str,
    root: Path = ROOT,
) -> dict:
    """
    作用：保存版本化供电审核并触发程序重新核验。
    输入：task_id - 任务 ID；request_id - 请求 ID；expected_revision - 期望版本；
          reviewer - 审核人；decision - 决定；note - 说明；root - 任务根目录。
    输出：dict，新审核记录或同一请求已提交的原记录。
    """
    # 先校验任务身份和审核字段，非法请求不进入审核事务。
    meta = load_task(task_id, root=root)
    if (
        not all(isinstance(v, str) and v.strip() for v in (request_id, reviewer, note))
        or decision not in {"approve", "reject"}
        or type(expected_revision) is not int
        or expected_revision < 0
    ):
        raise ValueError("审核需要请求ID、当前版本、审核人、approve/reject 和说明。")
    # 固定请求内容，重复请求只允许返回同一份已提交审核。
    payload = json.dumps(
        {
            "request_id": request_id,
            "expected_revision": expected_revision,
            "reviewer": reviewer,
            "decision": decision,
            "note": note,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    task_dir = root / task_id
    with task_write(task_dir) as connection:
        existing = connection.execute(
            "SELECT payload, record FROM reviews WHERE request_id=?", (request_id,)
        ).fetchone()
        if existing:
            if existing[0] != payload:
                raise ValueError("请求ID已用于不同的审核内容。")
            return json.loads(existing[1])
        # 核对当前审核版本，拒绝基于旧版本提交的新请求。
        status = get_task_status(task_id, root=root)
        if status["review_revision"] != expected_revision:
            raise ValueError("审核版本已变化，请重新查看参数。")
        # 必须先有已验收运行；不把残留工具产物或未完成节点当作可审核结果。
        database = task_dir / "checkpoints.sqlite"
        if not status["checkpoint_exists"]:
            raise ValueError("请先完成一次供电核验。")
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
            saver = SqliteSaver(db)
            saver.is_setup = True
            checkpoint = saver.get_tuple({"configurable": {"thread_id": task_id}})
            # 完成的图在检查点中没有待执行的分支。
            if not status["pending"] and any(
                key.startswith("branch:to:") and value
                for key, value in checkpoint.checkpoint["channel_values"].items()
            ):
                raise ValueError("任务存在未完成节点，不能审核。")
        runs = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (task_dir / "runs").glob("*/manifest.json")
        ]
        latest_run = max(runs, key=lambda item: item["started_at"], default={})
        if latest_run.get("status") != "passed" and not (
            latest_run.get("status") == "waiting_input"
            and status["pending"]
            and status["pending"][0]["value"]["kind"] == "review"
        ):
            raise ValueError("最新运行尚未通过验收，不能审核。")
        # 重新校验绑定原件和提取结果，避免审核与当前证据脱节。
        source = Path(meta["source_pdf"])
        if hashlib.sha256(source.read_bytes()).hexdigest() != meta["document_sha256"]:
            raise ValueError("归档 PDF 与任务绑定的版本不一致。")
        document = register_pdf(source, meta["source_url"], RUNTIME_DIR / "documents")
        evidence, fact = extract_ads1115_supply(document)
        old = status["adc_records"]
        if (
            old.get("evidence") != evidence.model_dump(mode="json")
            or not old.get("constraint_result")
            or {k: v for k, v in old.get("parameter_fact", {}).items() if k != "reviewed"}
            != fact.model_dump(mode="json", exclude={"reviewed"})
        ):
            raise ValueError("当前参数与绑定原文不一致，不能审核。")
        # 应用人工决定并运行原供电规则，审核确认不强制器件满足需求。
        fact.reviewed = decision == "approve"
        result = check_supply_voltage(
            AdcRequirements.model_validate(meta["requirements"]), fact, evidence, document
        )
        # 保存前后业务快照与审核信息，保留旧版本和旧报告。
        record = {
            "task_id": task_id,
            "revision": expected_revision + 1,
            "request_id": request_id,
            "reviewer": reviewer,
            "decision": decision,
            "note": note,
            "created_at": datetime.now(UTC).isoformat(),
            "document_sha256": meta["document_sha256"],
            "previous_records": old,
            "records": {
                "evidence": evidence.model_dump(mode="json"),
                "parameter_fact": fact.model_dump(mode="json"),
                "constraint_result": result.model_dump(mode="json"),
            },
            "report": {
                "task_id": task_id,
                "scope": "supply_voltage_only",
                "constraint_result": result.model_dump(mode="json"),
            },
        }
        connection.execute(
            "INSERT INTO reviews VALUES (?, ?, ?, ?)",
            (record["revision"], request_id, payload, json.dumps(record, ensure_ascii=False)),
        )
        return record


def send_message(
    task_id: str,
    message: str = "复述原供电判定并说明未核验范围，不修改原任务。",
    *,
    root: Path = ROOT,
    on_event=None,
) -> dict:
    """
    作用：在任务写锁内执行新任务或续聊。
    输入：task_id - 任务 ID；message - 用户消息；root - 任务根目录；on_event - 可选事件回调。
    输出：dict，运行清单、结构化报告及模型答复。
    """
    load_task(task_id, root=root)
    with task_write(root / task_id):
        if get_task_status(task_id, root=root)["pending"]:
            raise HumanInputError("任务正在等待输入，请使用 /resume；普通消息不能恢复。")
        return _send_message(task_id, message, root=root, on_event=on_event)


def wait_for_input(task_id, *, kind="review", question="请核对供电证据并提交审核。", root=ROOT):
    """
    作用：在已完成核验的同一线程开启人工等待点。
    输入：task_id - 任务 ID；kind - review 或 clarification；question - 等待问题；root - 根目录。
    输出：包含真实中断 ID 的等待运行结果。
    """
    load_task(task_id, root=root)
    if (
        kind not in {"review", "clarification"}
        or not isinstance(question, str)
        or not question.strip()
    ):
        raise HumanInputError("等待类型或问题无效。")
    with task_write(root / task_id):
        status = get_task_status(task_id, root=root)
        if status["pending"] or not status["adc_records"].get("constraint_result"):
            raise HumanInputError("请先完成核验，且不能重复开启等待事项。")
        # 只在已验收运行后设置等待点，不把失败任务包装成可恢复暂停。
        runs = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in (root / task_id / "runs").glob("*/manifest.json")
        ]
        if max(runs, key=lambda item: item["started_at"], default={}).get("status") != "passed":
            raise HumanInputError("最新运行尚未通过验收，不能开启等待。")
        return _send_message(
            task_id,
            question,
            root=root,
            on_event=None,
            waiting={
                "kind": kind,
                "question": question,
                "review_revision": status["review_revision"],
            },
        )


def resume_task(task_id, interrupt_id, answer, *, root=ROOT, on_event=None):
    """
    作用：校验等待事项后，以 Command 在原线程恢复。
    输入：task_id - 任务 ID；interrupt_id - 中断 ID；answer - 回答或审核请求 ID；root - 根目录。
          on_event - 可选事件回调。
    输出：恢复后的运行结果。
    """
    load_task(task_id, root=root)
    if not isinstance(answer, str) or not answer.strip():
        raise HumanInputError("恢复答案不能为空。")
    with task_write(root / task_id):
        status = get_task_status(task_id, root=root)
        pending = status["pending"]
        if len(pending) != 1 or pending[0]["id"] != interrupt_id:
            raise HumanInputError("中断 ID 不匹配或已恢复，请重新查看 /status。")
        if pending[0]["value"]["kind"] == "review":
            latest = status["reviews"][-1] if status["reviews"] else None
            if (
                not latest
                or latest["request_id"] != answer
                or latest["revision"] <= pending[0]["value"]["review_revision"]
            ):
                raise HumanInputError("请先提交本次 /review，再使用其请求 ID 恢复。")
        return _send_message(
            task_id, "", root=root, on_event=on_event, resume={interrupt_id: answer}
        )


def _send_message(task_id, message, *, root, on_event, waiting=None, resume=None):
    """
    作用：执行图流程并校验和归档本次运行结果。
    输入：task_id - 任务 ID；message - 用户消息；root - 任务根目录；on_event - 可选事件回调。
          waiting - 可选等待事项；resume - 可选中断 ID 与答案映射。
    输出：dict，包含运行清单、报告及答复；暂停时附带 pending 且报告为空。
    """
    meta = load_task(task_id, root=root)
    reviews = read_reviews(root / task_id)
    latest_review = reviews[-1] if reviews else None
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
        reviewed=(latest_review["decision"] == "approve" if latest_review else meta["reviewed"]),
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
        "review_revision": latest_review["revision"] if latest_review else 0,
    }
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
                middleware=[events, AwaitHumanInput(), PersistAdcRecords(session)],
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
                if previous.next and resume is None:
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

            if latest_review:
                # 只有宿主审核服务可替换旧业务快照；普通工具仍通过冲突合并校验。
                payload["adc_records"] = Overwrite(latest_review["records"])
                updated_files = {
                    "/inputs/review.json": create_file_data(
                        json.dumps(latest_review, ensure_ascii=False)
                    ),
                    "/outputs/report.json": create_file_data(
                        json.dumps(latest_review["report"], ensure_ascii=False)
                    ),
                }
                payload["files"] = updated_files
                baseline = {**baseline, **updated_files}
                payload["messages"][0]["content"] = (
                    "宿主已完成供电人工审核，最新版本见 /inputs/review.json；"
                    "原 /inputs/task.json 的审核声明是创建时快照。以最新审核和程序结果为准。"
                    + message
                )

            if waiting:
                payload["human_request"] = waiting
            if resume is not None:
                # 恢复使用 Command，不追加普通用户消息；保留宿主审核同步更新。
                payload.pop("messages", None)
                payload = Command(resume=resume, update=payload)
                manifest["resumed_interrupt_ids"] = list(resume)

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

            if saved.interrupts:
                manifest["status"] = "waiting_input"
                pending = [{"id": item.id, "value": item.value} for item in saved.interrupts]
                save_json(run_dir / "pending.json", pending)
                return {
                    "manifest": manifest,
                    "pending": pending,
                    "report": None,
                    "reply": "任务已暂停，请查看等待事项并使用 /resume。",
                }

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
        save_json(run_dir / "manifest.json", manifest)
