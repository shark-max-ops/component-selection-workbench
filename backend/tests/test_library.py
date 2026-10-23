"""验证资料库的去重、版本和页面来源。"""

import json
import sqlite3
from pathlib import Path

import pymupdf

from app.infrastructure.library import (
    compare_markitdown,
    import_pdf,
    index_legacy_tasks,
    list_documents,
    list_pages,
    list_part_cards,
    save_part_card,
)


def _pdf(path: Path, text: str, *, blank: bool = False) -> None:
    """
    作用：生成小型合成 PDF 以检查登记行为。
    输入：path - 文件路径；text - 页面文字；blank - 是否附加空页。
    输出：无。
    """
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    if blank:
        document.new_page()
    document.save(path)
    document.close()


def test_library_reuses_same_bytes_and_keeps_revisions(tmp_path: Path) -> None:
    """
    作用：检查相同字节复用，新修订与其他资料独立保存。
    输入：tmp_path - pytest 临时目录。
    输出：无。
    """
    root = tmp_path / "library"
    source = tmp_path / "a.pdf"
    _pdf(source, "Supply voltage 2.0 V to 5.5 V")
    options = dict(
        manufacturer="TI",
        kind="datasheet",
        parts=["ADS1115"],
        document_number="SBAS444",
        revision="E",
        source_url="https://ti.com/a",
    )
    first = import_pdf(source, root=root, **options)
    second = import_pdf(source, root=root, **options)
    assert not first["reused"] and second["reused"]
    assert first["id"] == second["id"]
    assert list_pages(first["id"], root=root)[0]["status"] == "parsed"
    assert "Supply voltage" in (root / "extracted" / first["id"][4:] / "page-0001.txt").read_text()

    _pdf(source, "Supply voltage 2.1 V to 5.5 V")
    options["revision"] = "F"
    newer = import_pdf(source, root=root, **options)
    assert newer["id"] != first["id"]
    assert {row["revision"] for row in list_documents(root=root)} == {"E", "F"}
    save_part_card(
        "ADS1115", "16-bit ADC; verify every requirement in the PDF", newer["id"], root=root
    )
    assert list_part_cards(root=root)[0]["source_document_id"] == newer["id"]
    card = root / "cards" / "ADS1115.md"
    assert card.exists()
    assert "SBAS444 F" in card.read_text(encoding="utf-8")
    assert "../originals/" in card.read_text(encoding="utf-8")


def test_empty_page_has_explicit_status(tmp_path: Path) -> None:
    """
    作用：检查空白或扫描疑似页不会显示为已解析。
    输入：tmp_path - pytest 临时目录。
    输出：无。
    """
    source = tmp_path / "with-blank.pdf"
    _pdf(source, "Chapter 1", blank=True)
    result = import_pdf(
        source,
        manufacturer="Example",
        kind="application_note",
        parts=["ADC1", "ADC2"],
        document_number="AN1",
        revision="1",
        source_url="https://example.com/an1",
        root=tmp_path / "library",
    )
    assert result["parse_status"] == "partial"
    pages = list_pages(result["id"], root=tmp_path / "library")
    assert [page["status"] for page in pages] == ["parsed", "empty_or_scanned"]


def test_markitdown_result_is_not_claimed_as_page_evidence(tmp_path: Path) -> None:
    """
    作用：检查 Markdown 试验可以保存，但不能假称有可靠页码。
    输入：tmp_path - pytest 临时目录。
    输出：无。
    """
    source = tmp_path / "sample.pdf"
    _pdf(source, "Supply voltage 2.0 V to 5.5 V")
    root = tmp_path / "library"
    document = import_pdf(
        source,
        manufacturer="Example",
        kind="datasheet",
        parts=["ADC1"],
        document_number="DS1",
        revision="A",
        source_url="https://example.com/ds1",
        root=root,
    )
    result = compare_markitdown(document["id"], root=root)
    assert result["status"] == "converted_unmapped"
    assert result["page_mapping"] == "unmapped"
    assert (root / result["output_path"]).exists()


def test_broken_pdf_is_registered_as_failure(tmp_path: Path) -> None:
    """
    作用：检查转换错误留在目录中供查询，而非被当作成功。
    输入：tmp_path - pytest 临时目录。
    输出：无。
    """
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"not a PDF")
    root = tmp_path / "library"
    result = import_pdf(
        source,
        manufacturer="Example",
        kind="datasheet",
        parts=["ADC1"],
        document_number="DS1",
        revision="1",
        source_url="https://example.com/ds1",
        root=root,
    )
    assert result["parse_status"] == "failed"
    assert list_documents(root=root)[0]["parse_error"]
    assert list_pages(result["id"], root=root) == []


def test_legacy_index_reads_without_moving_records(tmp_path: Path) -> None:
    """
    作用：检查旧任务只读入索引，重复执行不产生重复元记录。
    输入：tmp_path - pytest 临时目录。
    输出：无。
    """
    task = tmp_path / "tasks" / "task-1"
    run = task / "runs" / "run-1"
    run.mkdir(parents=True)
    (task / "task.json").write_text(
        json.dumps(
            {
                "task_id": "task-1",
                "document_sha256": "a" * 64,
                "requirements": {"supply_voltage_v": "3.3"},
            }
        ),
        encoding="utf-8",
    )
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "status": "passed",
                "started_at": "2026-01-01",
            }
        ),
        encoding="utf-8",
    )
    (run / "report.json").write_text("{}", encoding="utf-8")
    reviews = sqlite3.connect(task / "reviews.sqlite")
    reviews.execute("CREATE TABLE reviews (revision INTEGER, request_id TEXT)")
    reviews.execute("INSERT INTO reviews VALUES (1, 'review-1')")
    reviews.commit()
    reviews.close()
    root = tmp_path / "library"
    assert index_legacy_tasks(root=root, tasks_root=tmp_path / "tasks") == {
        "tasks": 1,
        "runs": 1,
        "reviews": 1,
    }
    index_legacy_tasks(root=root, tasks_root=tmp_path / "tasks")
    with sqlite3.connect(root / "catalog.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM task_index").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM report_index").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM review_index").fetchone()[0] == 1
    assert (task / "task.json").exists()
