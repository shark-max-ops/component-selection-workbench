"""登记 PDF 内容快照、逐页提取文本，并读取首个 ADS111x 供电参数。"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf


@dataclass(frozen=True)
class RegisteredDocument:
    """
    作用：保存一次导入结果。
    输入：类定义字段或当前调用上下文，见下方说明。
    输出：原有处理结果；具体返回值或副作用见下方实现。
    pages 的索引从 0 开始，Evidence 中的 PDF 页序从 1 开始。
    """

    sha256: str
    version: str
    source_url: str
    directory: Path
    pages: tuple[str, ...]

    @property
    def id(self) -> str:
        """
        作用：根据完整文件哈希生成稳定文档 ID，不依赖文件名。
        输入：类定义字段或当前调用上下文，见下方说明。
        输出：原有处理结果；具体返回值或副作用见下方实现。
        """
        return f"doc-{self.sha256}"


def register_pdf(
    pdf_path: Path,
    source_url: str,
    storage: Path,
) -> RegisteredDocument:
    """
    作用：登记并提取 PDF。
    输入：本地 PDF、登记的来源网址、版本存储目录。
    输出：文档对象、原文件副本、登记信息和每页文本。
    边界：不执行 OCR；来源网址属于登记信息，不代表程序已验证来源。
    """
    data = pdf_path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()

    # 哈希与解析使用同一份字节，避免原文件变化导致两者对不上。
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        if pdf.needs_pass or len(pdf) == 0:
            raise ValueError("PDF 加密或没有页面，当前无法处理。")

        pages = tuple(page.get_text("text", sort=True) for page in pdf)

    if not any(page.strip() for page in pages):
        raise ValueError("没有提取到文字；可能需要 OCR，当前不继续生成证据。")

    # 当前只识别这组 TI 手册的文档编号，不能识别时保留 unknown。
    match = re.search(r"\bSBAS444[A-Z]?\b", pages[0])
    version = match.group(0) if match else "unknown"

    directory = storage / digest
    directory.mkdir(parents=True, exist_ok=True)
    snapshot = directory / "source.pdf"

    # 同一内容复用同一目录；已归档原件不能被静默覆盖。
    if snapshot.exists():
        saved_digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        if saved_digest != digest:
            raise ValueError("已归档 PDF 的哈希不一致，请检查文件是否被修改。")
    else:
        snapshot.write_bytes(data)

    page_dir = directory / "pages"
    page_dir.mkdir(exist_ok=True)

    for number, text in enumerate(pages, start=1):
        (page_dir / f"{number:04d}.txt").write_text(
            text,
            encoding="utf-8",
        )

    document = RegisteredDocument(
        sha256=digest,
        version=version,
        source_url=source_url,
        directory=directory,
        pages=pages,
    )

    manifest = {
        "document_id": document.id,
        "sha256": digest,
        "version": version,
        "source_url": source_url,
        "page_count": len(pages),
        "extractor": "pymupdf",
        "extractor_version": pymupdf.__version__,
        "text_order": "sort=True",
        "pdf_page_base": 1,
    }

    (directory / "document.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return document
