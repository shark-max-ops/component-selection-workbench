"""保存可追溯的器件资料库，不修改 A 阶段的任务归档。"""

import hashlib
import importlib.metadata
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

from app.config import PROJECT_DIR, RUNTIME_DIR

LIBRARY_DIR = PROJECT_DIR / "library"


def _section_hint(text: str) -> str | None:
    """
    作用：从单页正文寻找章节标题提示，无法确定时留空。
    输入：text - 当前 PDF 页全文。
    输出：str 或 None，页面首个编号标题提示。
    """
    for line in text.splitlines():
        candidate = line.strip()
        if re.fullmatch(r"\d+(?:\.\d+){0,3}\s+[A-Za-z][A-Za-z ,/-]{4,75}", candidate):
            return candidate
    return None


def _connect(root: Path) -> sqlite3.Connection:
    """
    作用：打开资料库并建立最小可查询表。
    输入：root - 资料库根目录。
    输出：sqlite3.Connection，已开启外键的连接。
    """
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / "catalog.sqlite")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE,
            manufacturer TEXT NOT NULL, kind TEXT NOT NULL,
            document_number TEXT NOT NULL, revision TEXT NOT NULL,
            source_url TEXT NOT NULL, imported_at TEXT NOT NULL,
            page_count INTEGER NOT NULL, parse_status TEXT NOT NULL,
            parse_error TEXT,
            parser TEXT NOT NULL, parser_version TEXT NOT NULL,
            parser_config TEXT NOT NULL, original_path TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS document_parts (
            document_id TEXT NOT NULL REFERENCES documents(id),
            part_number TEXT NOT NULL,
            PRIMARY KEY (document_id, part_number)
        );
        CREATE TABLE IF NOT EXISTS pages (
            document_id TEXT NOT NULL REFERENCES documents(id),
            pdf_page INTEGER NOT NULL, status TEXT NOT NULL,
            text_path TEXT, char_count INTEGER NOT NULL,
            PRIMARY KEY (document_id, pdf_page)
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
            pdf_page INTEGER NOT NULL, section TEXT,
            text TEXT NOT NULL, text_path TEXT NOT NULL,
            FOREIGN KEY (document_id, pdf_page) REFERENCES pages(document_id, pdf_page)
        );
        CREATE TABLE IF NOT EXISTS part_cards (
            part_number TEXT PRIMARY KEY, summary TEXT NOT NULL,
            source_document_id TEXT NOT NULL REFERENCES documents(id),
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS record_links (
            record_kind TEXT NOT NULL, record_id TEXT NOT NULL,
            document_id TEXT NOT NULL REFERENCES documents(id),
            pdf_page INTEGER, source_path TEXT NOT NULL,
            PRIMARY KEY (record_kind, record_id)
        );
        CREATE TABLE IF NOT EXISTS parser_runs (
            document_id TEXT NOT NULL REFERENCES documents(id),
            parser TEXT NOT NULL, parser_version TEXT NOT NULL,
            parser_config TEXT NOT NULL, status TEXT NOT NULL,
            output_path TEXT, page_mapping TEXT NOT NULL, error TEXT,
            PRIMARY KEY (document_id, parser, parser_version, parser_config)
        );
        CREATE TABLE IF NOT EXISTS task_index (
            task_id TEXT PRIMARY KEY, document_id TEXT,
            source_path TEXT NOT NULL, requirements_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS run_index (
            run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            status TEXT NOT NULL, started_at TEXT NOT NULL,
            manifest_path TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evidence_index (
            evidence_id TEXT NOT NULL, task_id TEXT NOT NULL,
            document_id TEXT NOT NULL, pdf_page INTEGER NOT NULL,
            source_path TEXT NOT NULL, PRIMARY KEY (task_id, evidence_id)
        );
        CREATE TABLE IF NOT EXISTS fact_index (
            fact_id TEXT NOT NULL, task_id TEXT NOT NULL,
            part_number TEXT NOT NULL, parameter TEXT NOT NULL,
            source_path TEXT NOT NULL, PRIMARY KEY (task_id, fact_id)
        );
        CREATE TABLE IF NOT EXISTS review_index (
            task_id TEXT NOT NULL, revision INTEGER NOT NULL,
            request_id TEXT NOT NULL, source_path TEXT NOT NULL,
            PRIMARY KEY (task_id, revision)
        );
        CREATE TABLE IF NOT EXISTS report_index (
            run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            status TEXT NOT NULL, source_path TEXT NOT NULL
        );
    """)
    return connection


def import_pdf(
    source: Path,
    *,
    manufacturer: str,
    kind: str,
    parts: list[str],
    document_number: str,
    revision: str,
    source_url: str,
    root: Path = LIBRARY_DIR,
) -> dict:
    """
    作用：按文件哈希导入原始 PDF，记录元数据和逐页解析结果。
    输入：source - PDF；manufacturer - 厂家；kind - 类型；parts - 型号；
          document_number - 编号；revision - 修订；source_url - 来源；root - 资料库目录。
    输出：dict，文档标识、复用情况、页数及解析状态。
    """
    # 元数据由导入者明确提供；文件名或模型猜测不能充当文档修订。
    fields = (manufacturer, kind, document_number, revision, source_url)
    if any(not value.strip() for value in fields) or not parts or any(not p.strip() for p in parts):
        raise ValueError("厂家、类型、型号、文档编号、修订和来源都不能为空。")
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    document_id = f"doc-{digest}"
    with _connect(root) as db:
        existing = db.execute("SELECT * FROM documents WHERE sha256=?", (digest,)).fetchone()
        if existing:
            return {
                "id": existing["id"],
                "reused": True,
                "page_count": existing["page_count"],
                "parse_status": existing["parse_status"],
            }

        # 逐页解析；损坏或加密的 PDF 也登记失败状态，便于查看问题。
        try:
            with pymupdf.open(stream=data, filetype="pdf") as pdf:
                if pdf.needs_pass or len(pdf) == 0:
                    raise ValueError("PDF 加密或没有页面。")
                pages = [page.get_text("text", sort=True) for page in pdf]
            parse_error = None
        except (pymupdf.FileDataError, ValueError) as error:
            pages = []
            parse_error = str(error)
        original = root / "originals" / digest / "source.pdf"
        extracted = root / "extracted" / digest
        original.parent.mkdir(parents=True, exist_ok=True)
        extracted.mkdir(parents=True, exist_ok=True)
        original.write_bytes(data)
        now = datetime.now(timezone.utc).isoformat()
        statuses = ["parsed" if page.strip() else "empty_or_scanned" for page in pages]
        overall = (
            "failed"
            if parse_error
            else "parsed"
            if all(s == "parsed" for s in statuses)
            else ("partial" if any(s == "parsed" for s in statuses) else "needs_ocr")
        )
        # PDF 页序从 1 开始；文本产物与数据库中的页码一一对应。
        with db:
            db.execute(
                """INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    document_id,
                    digest,
                    manufacturer.strip(),
                    kind.strip(),
                    document_number.strip(),
                    revision.strip(),
                    source_url.strip(),
                    now,
                    len(pages),
                    overall,
                    parse_error,
                    "pymupdf",
                    pymupdf.__version__,
                    json.dumps({"sort": True}, ensure_ascii=False),
                    str(original.relative_to(root)),
                ),
            )
            for part in sorted(set(p.strip() for p in parts)):
                db.execute("INSERT INTO document_parts VALUES (?,?)", (document_id, part))
            for number, (page, status) in enumerate(zip(pages, statuses, strict=True), start=1):
                text_file = extracted / f"page-{number:04d}.txt"
                text_file.write_text(page, encoding="utf-8")
                relative = str(text_file.relative_to(root))
                db.execute(
                    "INSERT INTO pages VALUES (?,?,?,?,?)",
                    (
                        document_id,
                        number,
                        status,
                        relative,
                        len(page),
                    ),
                )
                if page.strip():
                    db.execute(
                        "INSERT INTO chunks VALUES (?,?,?,?,?,?)",
                        (
                            f"{document_id}-p{number}",
                            document_id,
                            number,
                            _section_hint(page),
                            page,
                            relative,
                        ),
                    )
        return {
            "id": document_id,
            "reused": False,
            "page_count": len(pages),
            "parse_status": overall,
        }


def list_documents(*, root: Path = LIBRARY_DIR) -> list[dict]:
    """
    作用：列出资料及其关联型号，供人检查版本和解析状态。
    输入：root - 资料库根目录。
    输出：list[dict]，每份文件一条记录。
    """
    with _connect(root) as db:
        rows = db.execute("SELECT * FROM documents ORDER BY imported_at, id").fetchall()
        return [
            dict(row)
            | {
                "parts": [
                    p[0]
                    for p in db.execute(
                        "SELECT part_number FROM document_parts "
                        "WHERE document_id=? ORDER BY part_number",
                        (row["id"],),
                    )
                ]
            }
            for row in rows
        ]


def list_pages(document_id: str, *, root: Path = LIBRARY_DIR) -> list[dict]:
    """
    作用：查询文档每页解析状态和可回查文本路径。
    输入：document_id - 文档 ID；root - 资料库根目录。
    输出：list[dict]，按 PDF 页序排列。
    """
    with _connect(root) as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT * FROM pages WHERE document_id=? ORDER BY pdf_page", (document_id,)
            )
        ]


def enrich_sections(*, root: Path = LIBRARY_DIR) -> int:
    """
    作用：为早期已导入的整页片段补记章节标题提示。
    输入：root - 资料库目录。
    输出：int，更新的片段数量。
    """
    with _connect(root) as db, db:
        chunks = db.execute("SELECT id,text FROM chunks WHERE section IS NULL").fetchall()
        for row in chunks:
            db.execute(
                "UPDATE chunks SET section=? WHERE id=?", (_section_hint(row["text"]), row["id"])
            )
        return len(chunks)


def save_part_card(
    part_number: str, summary: str, source_document_id: str, *, root: Path = LIBRARY_DIR
) -> None:
    """
    作用：保存带来源的短简介，供后续候选检索使用。
    输入：part_number - 型号；summary - 简介；source_document_id - 来源文档；root - 资料库目录。
    输出：无。
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", part_number) or not summary.strip():
        raise ValueError("型号和简介不能为空。")
    with _connect(root) as db:
        linked = db.execute(
            "SELECT 1 FROM document_parts WHERE document_id=? AND part_number=?",
            (source_document_id, part_number),
        ).fetchone()
        if not linked:
            raise ValueError("简介来源文档未关联该型号。")
        with db:
            db.execute(
                "INSERT INTO part_cards VALUES (?,?,?,?) "
                "ON CONFLICT(part_number) DO UPDATE SET summary=excluded.summary, "
                "source_document_id=excluded.source_document_id, updated_at=excluded.updated_at",
                (
                    part_number,
                    summary.strip(),
                    source_document_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    export_part_cards(root=root)


def list_part_cards(*, root: Path = LIBRARY_DIR) -> list[dict]:
    """
    作用：列出器件简介及来源文档。
    输入：root - 资料库目录。
    输出：list[dict]，按型号排列的简介。
    """
    with _connect(root) as db:
        return [dict(row) for row in db.execute("SELECT * FROM part_cards ORDER BY part_number")]


def export_part_cards(*, root: Path = LIBRARY_DIR) -> list[Path]:
    """
    作用：将数据库中的器件短简介导出为可直接打开的 Markdown 文件。
    输入：root - 资料库目录。
    输出：list[Path]，生成的简介文件路径。
    """
    cards_dir = root / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    written = []
    with _connect(root) as db:
        rows = db.execute(
            "SELECT c.part_number,c.summary,c.source_document_id,d.manufacturer,"
            "d.document_number,d.revision,d.source_url,d.original_path "
            "FROM part_cards c JOIN documents d ON d.id=c.source_document_id "
            "ORDER BY c.part_number"
        ).fetchall()
        for row in rows:
            path = cards_dir / f"{row['part_number']}.md"
            content = (
                f"# {row['part_number']}\n\n{row['summary']}\n\n"
                "## 简介来源\n\n"
                f"- 厂家：{row['manufacturer']}\n"
                f"- 文档编号与修订：{row['document_number']} {row['revision']}\n"
                f"- 文档 ID：`{row['source_document_id']}`\n"
                f"- 原始 PDF：`../{row['original_path'].replace(chr(92), '/')}`\n"
                f"- 来源网址：{row['source_url']}\n\n"
                "简介只供寻找候选；具体参数和条件需核对原 PDF。\n"
            )
            temporary = path.with_suffix(".md.tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
            written.append(path)
    return written


def compare_markitdown(document_id: str, *, root: Path = LIBRARY_DIR) -> dict:
    """
    作用：试验 MarkItDown 整份转换，明确记录其页码无法直接映射。
    输入：document_id - 已登记文档 ID；root - 资料库目录。
    输出：dict，解析状态、输出路径及页面定位能力。
    """
    from markitdown import MarkItDown

    with _connect(root) as db:
        row = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if not row:
            raise ValueError("文档不存在。")
        version = importlib.metadata.version("markitdown")
        config = json.dumps({"mode": "whole_pdf"}, sort_keys=True)
        target = root / "extracted" / row["sha256"] / "markitdown.md"
        try:
            converted = MarkItDown().convert(str(root / row["original_path"]))
            content = converted.text_content or ""
            if not content.strip():
                raise ValueError("MarkItDown 未产生文字。")
            target.write_text(content, encoding="utf-8")
            status, error, output = "converted_unmapped", None, str(target.relative_to(root))
        except Exception as exception:
            status, error, output = "failed", str(exception), None
        # 整份 Markdown 没有可靠的逐页来源映射，不供正式 Evidence 使用。
        with db:
            db.execute(
                "INSERT INTO parser_runs VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(document_id,parser,parser_version,parser_config) DO UPDATE SET "
                "status=excluded.status, output_path=excluded.output_path, error=excluded.error",
                (document_id, "markitdown", version, config, status, output, "unmapped", error),
            )
        return {
            "document_id": document_id,
            "parser": "markitdown",
            "status": status,
            "output_path": output,
            "page_mapping": "unmapped",
            "error": error,
        }


def index_legacy_tasks(
    *, root: Path = LIBRARY_DIR, tasks_root: Path = RUNTIME_DIR / "persistent_tasks"
) -> dict:
    """
    作用：只读旧任务 JSON 和审核库，建立可查询的业务元信息索引。
    输入：root - 新资料库；tasks_root - A 阶段任务目录。
    输出：dict，处理的任务、运行和审核数量。
    """
    counts = {"tasks": 0, "runs": 0, "reviews": 0}
    with _connect(root) as db, db:
        for task_file in tasks_root.glob("*/task.json"):
            task = json.loads(task_file.read_text(encoding="utf-8"))
            task_id = task["task_id"]
            document_id = f"doc-{task['document_sha256']}"
            db.execute(
                "INSERT OR REPLACE INTO task_index VALUES (?,?,?,?)",
                (
                    task_id,
                    document_id,
                    str(task_file),
                    json.dumps(task["requirements"], ensure_ascii=False),
                ),
            )
            counts["tasks"] += 1
            for manifest_file in (task_file.parent / "runs").glob("*/manifest.json"):
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                run_id = manifest["run_id"]
                db.execute(
                    "INSERT OR REPLACE INTO run_index VALUES (?,?,?,?,?)",
                    (
                        run_id,
                        task_id,
                        manifest["status"],
                        manifest["started_at"],
                        str(manifest_file),
                    ),
                )
                counts["runs"] += 1
                records_file = manifest_file.parent / "business_records.json"
                if records_file.exists():
                    records = json.loads(records_file.read_text(encoding="utf-8"))
                    evidence = records.get("evidence")
                    fact = records.get("parameter_fact")
                    if evidence:
                        db.execute(
                            "INSERT OR REPLACE INTO evidence_index VALUES (?,?,?,?,?)",
                            (
                                evidence["id"],
                                task_id,
                                evidence["document_id"],
                                evidence["pdf_page"],
                                str(records_file),
                            ),
                        )
                    if fact:
                        db.execute(
                            "INSERT OR REPLACE INTO fact_index VALUES (?,?,?,?,?)",
                            (
                                fact["id"],
                                task_id,
                                fact["part_number"],
                                fact["parameter"],
                                str(records_file),
                            ),
                        )
                report_file = manifest_file.parent / "report.json"
                if report_file.exists():
                    db.execute(
                        "INSERT OR REPLACE INTO report_index VALUES (?,?,?,?)",
                        (run_id, task_id, manifest["status"], str(report_file)),
                    )
            reviews_file = task_file.parent / "reviews.sqlite"
            if reviews_file.exists():
                with sqlite3.connect(reviews_file.resolve().as_uri() + "?mode=ro", uri=True) as old:
                    for revision, request_id in old.execute(
                        "SELECT revision, request_id FROM reviews"
                    ):
                        db.execute(
                            "INSERT OR REPLACE INTO review_index VALUES (?,?,?,?)",
                            (task_id, revision, request_id, str(reviews_file)),
                        )
                        counts["reviews"] += 1
    return counts


def library_summary(*, root: Path = LIBRARY_DIR) -> dict:
    """
    作用：给出资料库和旧业务索引的可读计数。
    输入：root - 资料库目录。
    输出：dict，文档、页、器件卡及任务记录数量。
    """
    with _connect(root) as db:
        names = (
            "documents",
            "document_parts",
            "pages",
            "part_cards",
            "task_index",
            "run_index",
            "evidence_index",
            "fact_index",
            "review_index",
            "report_index",
        )
        return {name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in names}
