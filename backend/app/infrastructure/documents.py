"""登记 PDF 内容快照、归档原件并提取逐页文本。"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf


@dataclass(frozen=True)
class RegisteredDocument:
    """
    作用：保存登记后的文档版本和逐页文本。
    输入：sha256 - 文件哈希；version - 版本；source_url - 来源网址；
          directory - 存储目录；pages - 逐页文本。
    输出：RegisteredDocument 文档对象。
    """

    # pages 的索引从 0 开始，Evidence 中的 PDF 页序从 1 开始。

    sha256: str
    version: str
    source_url: str
    directory: Path
    pages: tuple[str, ...]

    @property
    def id(self) -> str:
        """
        作用：根据完整文件哈希生成稳定文档 ID。
        输入：self - 当前文档对象。
        输出：str，带 doc- 前缀的文档 ID。
        """
        return f"doc-{self.sha256}"


def register_pdf(
    pdf_path: Path,
    source_url: str,
    storage: Path,
) -> RegisteredDocument:
    """
    作用：登记 PDF 内容快照并提取逐页文本。
    输入：pdf_path - PDF 路径；source_url - 来源网址；storage - 版本存储目录。
    输出：RegisteredDocument，已登记的文档对象。
    """
    # 不执行 OCR；来源网址属于登记信息，不代表程序已验证来源。
    # 读取内容并计算版本哈希，供原件归档与一致性校验使用。
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

    # 按 PDF 页序保存逐页文本，供证据定位使用。
    page_dir = directory / "pages"
    page_dir.mkdir(exist_ok=True)

    for number, text in enumerate(pages, start=1):
        (page_dir / f"{number:04d}.txt").write_text(
            text,
            encoding="utf-8",
        )

    # 组织文档对象和登记清单，记录提取方式及页码基准。
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
