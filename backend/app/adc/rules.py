"""检查指定候选的供电电压是否落在推荐范围内。"""

from app.adc.extractors import extract_ads1115_supply
from app.contracts import (
    AdcRequirements,
    ConstraintResult,
    Evidence,
    NumericValue,
    ParameterFact,
)
from app.infrastructure.documents import RegisteredDocument


def check_supply_voltage(
    requirements: AdcRequirements,
    fact: ParameterFact,
    evidence: Evidence,
    document: RegisteredDocument,
) -> ConstraintResult:
    """
    作用：核验需求电压是否处于推荐供电范围内。
    输入：requirements - 需求；fact - 参数声明；evidence - 原文证据；document - 已登记文档。
    输出：ConstraintResult，供电单项判定及未解决项。
    """
    # 只支持本轮提取器；不代表整机适用，不汇总多来源冲突。
    problems = []

    if fact.part_number not in requirements.candidate_parts:
        problems.append("参数所属型号不在需求候选中")

    if fact.parameter != "supply_voltage":
        problems.append("提供的参数不是供电电压")

    if fact.specification not in {"specified", "guaranteed"}:
        problems.append("典型值、绝对最大额定值或未知规格不能用于本规则")

    if not fact.reviewed:
        problems.append("尚未对照 PDF 人工核对参数及适用条件")

    # 从当前导入文本重新提取，检查证据、数值、条件和引用是否串错。
    try:
        expected_evidence, expected_fact = extract_ads1115_supply(document)

        if evidence != expected_evidence:
            problems.append("证据与当前文档提取结果不一致")

        ignored = {"reviewed", "specification"}
        actual_data = fact.model_dump(exclude=ignored)
        expected_data = expected_fact.model_dump(exclude=ignored)

        if actual_data != expected_data:
            problems.append("参数、适用条件或证据引用与原文提取结果不一致")

    except ValueError as exc:
        problems.append(str(exc))

    value = fact.value

    if not isinstance(value, NumericValue) or value.unit != "V":
        problems.append("供电参数必须使用 V 单位的数值范围")
    elif value.lower is None or value.upper is None:
        problems.append("推荐供电范围上下限不完整")

    status = "unknown"
    reason = "依据尚不完整，暂不进行明确判定。"

    if not problems:
        # 前面的检查已确保类型和上下限完整。
        assert isinstance(value, NumericValue)
        assert value.lower is not None and value.upper is not None

        voltage = requirements.supply_voltage_v
        within = value.lower <= voltage <= value.upper

        status = "satisfied" if within else "not_satisfied"
        reason = f"需求 {voltage} V，推荐供电范围 [{value.lower}, {value.upper}] V；" + (
            "该电压项满足。" if within else "该电压项不满足。"
        )

    return ConstraintResult(
        id=f"result-{requirements.id}-{fact.id}",
        requirement_id=requirements.id,
        part_number=fact.part_number,
        parameter="supply_voltage",
        status=status,
        reason=reason,
        fact_ids=[fact.id],
        evidence_ids=[evidence.id],
        unresolved=problems,
    )
