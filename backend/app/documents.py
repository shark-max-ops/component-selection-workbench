"""登记 PDF 内容快照、逐页提取文本，并读取首个 ADS111x 供电参数。"""

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pymupdf

from app.contracts import Evidence, NumericValue, ParameterFact


@dataclass(frozen=True)
class RegisteredDocument:
    """保存一次导入结果。

    pages 的索引从 0 开始，Evidence 中的 PDF 页序从 1 开始。
    """

    sha256: str
    version: str
    source_url: str
    directory: Path
    pages: tuple[str, ...]

    @property
    def id(self) -> str:
        """根据完整文件哈希生成稳定文档 ID，不依赖文件名。"""
        return f"doc-{self.sha256}"


def register_pdf(
    pdf_path: Path,
    source_url: str,
    storage: Path,
) -> RegisteredDocument:
    """登记并提取 PDF。

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


def extract_ads1115_supply(
    document: RegisteredDocument,
) -> tuple[Evidence, ParameterFact]:
    """提取指定手册推荐工作条件中的 ADS1115 供电范围。

    输入：本次从 PDF 提取出的文档对象。
    输出：真实原文证据，以及默认未人工审核的参数声明。
    边界：只支持 SBAS444E；版本、表头或行格式不匹配就停止。
    """
    if document.version != "SBAS444E" or "ADS1115" not in document.pages[0]:
        raise ValueError("当前提取器只支持含 ADS1115 的 SBAS444E 手册。")

    # 先限定章节，不能在整页直接搜索供电数字。
    section_pattern = re.compile(
        r"5\.3\s+Recommended Operating Conditions\b(.*?)"
        r"5\.4\s+Thermal Information",
        re.DOTALL,
    )

    row_pattern = re.compile(
        r"Power\s+supply\s+\(VDD\s+to\s+GND\)\s+"
        r"(?P<lower>\d+(?:\.\d+)?)\s+"
        r"(?P<upper>\d+(?:\.\d+)?)\s+V\b"
    )

    matches = []

    for number, text in enumerate(document.pages, start=1):
        for section in section_pattern.finditer(text):
            quote = (
                section.group(0)
                .rsplit(
                    "5.4",
                    maxsplit=1,
                )[0]
                .strip()
            )

            # 仅匹配时折叠空白；证据仍保留实际提取的文本。
            normalized = " ".join(quote.split())
            rows = list(row_pattern.finditer(normalized))

            if "MIN NOM MAX UNIT" in normalized and len(rows) == 1:
                matches.append((number, quote, rows[0]))

    if len(matches) != 1:
        raise ValueError("未找到唯一、格式匹配的推荐供电表；请检查逐页文本。")

    number, quote, row = matches[0]

    evidence = Evidence(
        id=f"ev-{document.sha256}-p{number}-supply",
        document_id=document.id,
        document_version=document.version,
        document_sha256=document.sha256,
        source_url=document.source_url,
        pdf_page=number,
        printed_page=None,
        section="5.3 Recommended Operating Conditions",
        quote=quote,
    )

    fact = ParameterFact(
        id=f"fact-{document.sha256}-ADS1115-supply",
        part_number="ADS1115",
        parameter="supply_voltage",
        value=NumericValue(
            lower=Decimal(row.group("lower")),
            upper=Decimal(row.group("upper")),
            unit="V",
        ),
        specification="specified",
        conditions=("推荐工作条件中的 VDD 对 GND 供电范围；只核验此电压项。"),
        evidence_ids=[evidence.id],
        reviewed=False,
    )

    return evidence, fact
