"""验证容易导致误判的数据边界；测试材料全部为合成数据。"""

import pytest
from pydantic import ValidationError

from app.contracts import (
    AdcRequirements,
    ConstraintResult,
    Evidence,
    NumericValue,
    ParameterFact,
)


def make_requirements(**changes) -> AdcRequirements:
    """构造合成需求。

    输入：需要覆盖的字段。
    输出：经过验证的需求对象。
    要点：TEST_ADC 是测试名称，不对应真实器件规格。
    """
    data = {
        "id": "req-test",
        "candidate_parts": ["TEST_ADC"],
        "supply_voltage_v": "3.3",
        "interface": "I2C",
        "min_resolution_bits": 12,
        "required_channels": 4,
    }
    return AdcRequirements.model_validate(data | changes)


def test_missing_conditions_stay_unknown():
    """缺失条件保持未知，JSON 序列化前后含义保持一致。"""
    item = make_requirements()

    assert item.simultaneous_sampling is None
    assert item.input_mode is None
    assert item.min_samples_per_channel_sps is None

    restored = AdcRequirements.model_validate_json(item.model_dump_json())
    assert restored == item


@pytest.mark.parametrize(
    "changes",
    [
        {"required_channels": True},
        {"input_min_v": "3", "input_max_v": "1"},
        {"supply_voltage_v": "NaN"},
        {"candidate_parts": ["TEST_ADC", "TEST_ADC"]},
        {"unexpected": 1},
    ],
)
def test_bad_requirements_rejected(changes):
    """拒绝布尔通道数、倒置范围、非有限值、重复候选和未知字段。"""
    with pytest.raises(ValidationError):
        make_requirements(**changes)


def test_wrong_parameter_unit_rejected():
    """供电参数不能使用采样率单位。"""
    with pytest.raises(ValidationError):
        ParameterFact(
            id="fact-test",
            part_number="TEST_ADC",
            parameter="supply_voltage",
            value=NumericValue(lower="2", upper="5", unit="SPS"),
            conditions="合成测试数据",
            evidence_ids=["ev-test"],
        )


def test_pdf_page_zero_rejected():
    """证据页序从 1 开始，避免与解析器的零起始索引混用。"""
    with pytest.raises(ValidationError):
        Evidence(
            id="ev-test",
            document_id="doc-test",
            document_version="test",
            document_sha256="0" * 64,
            source_url="https://example.com/test.pdf",
            pdf_page=0,
            quote="Synthetic test evidence.",
        )


@pytest.mark.parametrize(
    "status",
    ["satisfied", "not_satisfied", "conflict"],
)
def test_decisions_need_references(status):
    """明确判定缺少依据时必须报错。"""
    with pytest.raises(ValidationError):
        ConstraintResult(
            id="result-test",
            requirement_id="req-test",
            part_number="TEST_ADC",
            parameter="sample_rate",
            status=status,
            reason="合成判定",
        )


def test_unknown_requires_explanation():
    """待确认必须指出缺失信息。"""
    with pytest.raises(ValidationError):
        ConstraintResult(
            id="result-test",
            requirement_id="req-test",
            part_number="TEST_ADC",
            parameter="sample_rate",
            status="unknown",
            reason="尚未完成调查",
        )
