"""验证业务工具边界；使用合成文本，不调用模型或网络。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agents.tools import build_adc_tools
from app.contracts import AdcRequirements
from app.infrastructure.documents import RegisteredDocument


def make_case(
    voltage="3.3",
    reviewed=False,
    version="SBAS444E",
):
    """
    作用：构造供电工具测试所需的合成任务。
    输入：voltage - 供电电压；reviewed - 审核状态；version - 合成文档版本。
    输出：tuple，需求、文档、工具字典及宿主业务对象。
    """
    # 合成文本只用于测试，不是真实器件证据。
    # 合成指定章节和表头，隔离真实 PDF 与网络依赖。
    text = (
        f"ADS1115 {version}\n"
        "5.3 Recommended Operating Conditions\n"
        "MIN NOM MAX UNIT\n"
        "POWER SUPPLY\n"
        "Power supply (VDD to GND) 2 5.5 V\n"
        "5.4 Thermal Information\n"
    )

    document = RegisteredDocument(
        sha256="1" * 64,
        version=version,
        source_url="https://example.com/synthetic.pdf",
        directory=Path("synthetic"),
        pages=(text,),
    )

    # 用固定候选和需求创建绑定当前文档的业务工具。
    requirements = AdcRequirements(
        id="req-test",
        candidate_parts=["ADS1115"],
        supply_voltage_v=voltage,
        interface="I2C",
        min_resolution_bits=12,
        required_channels=4,
    )

    tools, session = build_adc_tools(
        requirements,
        document,
        reviewed=reviewed,
    )

    return (
        requirements,
        document,
        {item.name: item for item in tools},
        session,
    )


def read_and_check(tools, document):
    """
    作用：先读取证据再执行供电核验。
    输入：tools - 工具字典；document - 合成文档。
    输出：dict，供电核验工具的真实返回结果。
    """
    # 先取得真实工具返回的引用，再提交给核验工具。
    evidence = tools["read_adc_supply_evidence"].invoke(
        {
            "document_id": document.id,
            "part_number": "ADS1115",
        }
    )
    assert evidence["ok"] is True

    return tools["check_adc_supply"].invoke(
        {
            "fact_id": evidence["parameter_fact"]["id"],
            "evidence_id": evidence["evidence"]["id"],
        }
    )


@pytest.mark.parametrize(
    ("voltage", "reviewed", "status"),
    [
        ("3.3", False, "unknown"),
        ("3.3", True, "satisfied"),
        ("6", True, "not_satisfied"),
    ],
)
def test_tool_business_results(voltage, reviewed, status):
    """
    作用：工具执行成功与业务条件满足分开判断。
    输入：voltage - 待测供电电压；reviewed - 宿主审核状态；status - 待测或期望的判定状态。
    输出：无
    """
    _, document, tools, session = make_case(
        voltage=voltage,
        reviewed=reviewed,
    )

    response = read_and_check(tools, document)

    assert response["ok"] is True
    assert response["constraint_result"]["status"] == status
    assert session.result.status == status


def test_check_requires_evidence():
    """
    作用：尚未读取证据时，拒绝直接执行核验。
    输入：无
    输出：无
    """
    _, _, tools, session = make_case()

    response = tools["check_adc_supply"].invoke({"fact_id": "missing", "evidence_id": "missing"})

    assert response["ok"] is False
    assert session.result is None


def test_wrong_reference_rejected():
    """
    作用：读取证据后仍不能使用其他引用。
    输入：无
    输出：无
    """
    _, document, tools, session = make_case()

    tools["read_adc_supply_evidence"].invoke(
        {
            "document_id": document.id,
            "part_number": "ADS1115",
        }
    )
    response = tools["check_adc_supply"].invoke({"fact_id": "wrong", "evidence_id": "wrong"})

    assert response["ok"] is False
    assert session.result is None


def test_model_cannot_supply_review_flag():
    """
    作用：拒绝模型在工具参数中追加审核状态。
    输入：无
    输出：无
    """
    _, document, tools, _ = make_case()

    with pytest.raises(ValidationError):
        tools["read_adc_supply_evidence"].invoke(
            {
                "document_id": document.id,
                "part_number": "ADS1115",
                "reviewed": True,
            }
        )


def test_unsupported_version():
    """
    作用：指定手册提取器不适用于其他版本时返回明确错误。
    输入：无
    输出：无
    """
    _, document, tools, session = make_case(version="SBAS444F")

    response = tools["read_adc_supply_evidence"].invoke(
        {
            "document_id": document.id,
            "part_number": "ADS1115",
        }
    )

    assert response["ok"] is False
    assert session.fact is None


def test_requirements_are_snapshotted():
    """
    作用：调用方修改原需求，不应改变已创建工具的需求副本。
    输入：无
    输出：无
    """
    requirements, document, tools, _ = make_case(reviewed=True)
    requirements.supply_voltage_v = "6"

    response = read_and_check(tools, document)

    assert response["constraint_result"]["status"] == "satisfied"
    assert "3.3 V" in response["constraint_result"]["reason"]
