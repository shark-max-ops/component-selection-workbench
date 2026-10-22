"""使用合成文本检查供电规则；不依赖网络和真实 PDF 文件。"""

from dataclasses import replace
from pathlib import Path

import pytest

from app.adc.extractors import extract_ads1115_supply
from app.adc.rules import check_supply_voltage
from app.contracts import AdcRequirements
from app.infrastructure.documents import RegisteredDocument


@pytest.fixture
def supply_case():
    """构造合成案例。

    输入：无。
    输出：需求、文档、证据和已审核的参数声明。
    边界：文本仅模拟提取器所需格式，不作为真实器件证据。
    """
    text = (
        "ADS1115 SBAS444E\n"
        "5.3 Recommended Operating Conditions\n"
        "MIN NOM MAX UNIT\n"
        "POWER SUPPLY\n"
        "Power supply (VDD to GND) 2 5.5 V\n"
        "5.4 Thermal Information\n"
    )

    document = RegisteredDocument(
        sha256="1" * 64,
        version="SBAS444E",
        source_url="https://example.com/synthetic.pdf",
        directory=Path("synthetic"),
        pages=(text,),
    )

    requirements = AdcRequirements(
        id="req-test",
        candidate_parts=["ADS1115"],
        supply_voltage_v="3.3",
        interface="I2C",
        min_resolution_bits=12,
        required_channels=4,
    )

    evidence, fact = extract_ads1115_supply(document)
    fact.reviewed = True

    return requirements, document, evidence, fact


@pytest.mark.parametrize(
    ("voltage", "expected"),
    [
        ("1.8", "not_satisfied"),
        ("2", "satisfied"),
        ("3.3", "satisfied"),
        ("5.5", "satisfied"),
        ("6", "not_satisfied"),
    ],
)
def test_supply_boundaries(supply_case, voltage, expected):
    """包含边界值；超出推荐范围时不能通过。"""
    requirements, document, evidence, fact = supply_case
    requirements.supply_voltage_v = voltage

    result = check_supply_voltage(requirements, fact, evidence, document)

    assert result.status == expected


@pytest.mark.parametrize(
    "specification",
    ["typical", "absolute_maximum", "unknown"],
)
def test_unsuitable_specification(supply_case, specification):
    """不能把典型值、绝对最大额定值或未知类别当成正常供电依据。"""
    requirements, document, evidence, fact = supply_case
    fact.specification = specification

    result = check_supply_voltage(requirements, fact, evidence, document)

    assert result.status == "unknown"


def test_unreviewed_fact(supply_case):
    """未审核参数保持待确认。"""
    requirements, document, evidence, fact = supply_case
    fact.reviewed = False

    result = check_supply_voltage(requirements, fact, evidence, document)

    assert result.status == "unknown"


def test_changed_value(supply_case):
    """即使修改后的范围能覆盖需求，也不能绕过原文一致性检查。"""
    requirements, document, evidence, fact = supply_case
    fact.value.upper = "7"

    result = check_supply_voltage(requirements, fact, evidence, document)

    assert result.status == "unknown"


def test_wrong_reference(supply_case):
    """引用另一个证据 ID 时不能通过。"""
    requirements, document, evidence, fact = supply_case
    fact.evidence_ids = ["another-evidence"]

    result = check_supply_voltage(requirements, fact, evidence, document)

    assert result.status == "unknown"


def test_unsupported_version(supply_case):
    """不能沿用旧提取器直接核验另一版本。"""
    requirements, document, evidence, fact = supply_case
    other_version = replace(document, version="SBAS444F")

    result = check_supply_voltage(requirements, fact, evidence, other_version)

    assert result.status == "unknown"
