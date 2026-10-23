"""从指定型号手册提取参数与证据。"""

import re
from decimal import Decimal

from app.contracts import Evidence, NumericValue, ParameterFact
from app.infrastructure.documents import RegisteredDocument


def extract_ads1115_supply(
    document: RegisteredDocument,
) -> tuple[Evidence, ParameterFact]:
    """
    作用：提取 ADS1115 推荐供电范围及原文证据。
    输入：document - 已登记的文档对象。
    输出：tuple[Evidence, ParameterFact]，原文证据与默认未审核的参数声明。
    """
    # 只支持 SBAS444E；版本、表头或行格式不匹配就停止。
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
