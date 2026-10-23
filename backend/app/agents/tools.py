"""将现有供电业务函数接入 Agent，并保留宿主侧的实际业务记录。"""

from dataclasses import dataclass

from langchain_core.tools import BaseTool, tool

from app.adc.extractors import extract_ads1115_supply
from app.adc.rules import check_supply_voltage
from app.contracts import (
    AdcRequirements,
    ConstraintResult,
    Contract,
    Evidence,
    ParameterFact,
    Text,
)
from app.infrastructure.documents import RegisteredDocument


class EvidenceQuery(Contract):
    """
    作用：约束供电证据查询工具的输入。
    输入：document_id - 文档 ID；part_number - 器件型号。
    输出：EvidenceQuery 参数对象。
    """

    document_id: Text
    part_number: Text


class SupplyQuery(Contract):
    """
    作用：约束供电核验工具的引用输入。
    输入：fact_id - 参数声明 ID；evidence_id - 证据 ID。
    输出：SupplyQuery 参数对象。
    """

    fact_id: Text
    evidence_id: Text


@dataclass
class SupplySession:
    """
    作用：保存宿主侧供电证据、参数和判定。
    输入：evidence - 可选证据；fact - 可选参数声明；result - 可选判定。
    输出：SupplySession 运行时业务对象。
    """

    # 由宿主程序持有，模型不能通过修改虚拟文件直接修改此对象。

    evidence: Evidence | None = None
    fact: ParameterFact | None = None
    result: ConstraintResult | None = None


def build_adc_tools(
    requirements: AdcRequirements,
    document: RegisteredDocument,
    reviewed: bool = False,
) -> tuple[list[BaseTool], SupplySession]:
    """
    作用：创建绑定需求和文档的供电业务工具。
    输入：requirements - 需求；document - 已登记文档；reviewed - 宿主审核状态。
    输出：tuple[list[BaseTool], SupplySession]，工具列表和宿主业务对象。
    """
    # 仅覆盖 ADS1115 供电，不提供任意路径或代码执行入口。
    # 使用需求副本，避免调用方后续修改原对象影响当前任务。
    fixed_requirements = requirements.model_copy(deep=True)
    session = SupplySession()

    @tool(args_schema=EvidenceQuery)
    def read_adc_supply_evidence(
        document_id: str,
        part_number: str,
    ) -> dict:
        """
        作用：读取指定文档中的 ADS1115 推荐供电证据。
        输入：document_id - 文档 ID；part_number - 器件型号。
        输出：dict，成功时包含证据、参数及页面路径，失败时包含错误说明。
        """
        # 文档与型号必须同时落在本次任务的固定范围内。
        if document_id != document.id or part_number != "ADS1115":
            return {
                "ok": False,
                "error": "文档 ID 或型号不在本次工具范围内。",
            }

        if part_number not in fixed_requirements.candidate_parts:
            return {
                "ok": False,
                "error": "型号不在本次需求候选中。",
            }

        # 提取原文和参数，格式不匹配时返回可解释的工具错误。
        try:
            evidence, fact = extract_ads1115_supply(document)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        # 审核状态来自宿主，不接受模型传入。
        fact.reviewed = reviewed
        session.evidence = evidence
        session.fact = fact

        return {
            "ok": True,
            "evidence": evidence.model_dump(mode="json"),
            "parameter_fact": fact.model_dump(mode="json"),
            "page_path": (f"/documents/pages/{evidence.pdf_page:04d}.txt"),
        }

    @tool(args_schema=SupplyQuery)
    def check_adc_supply(
        fact_id: str,
        evidence_id: str,
    ) -> dict:
        """
        作用：对已读取的供电参数执行固定需求下的核验。
        输入：fact_id - 参数声明 ID；evidence_id - 证据 ID。
        输出：dict，成功时包含约束判定，失败时包含错误说明。
        """
        # 必须先读取证据，并使用同一次读取返回的引用 ID。
        if session.fact is None or session.evidence is None:
            return {
                "ok": False,
                "error": "请先读取本次供电证据。",
            }

        if fact_id != session.fact.id or evidence_id != session.evidence.id:
            return {
                "ok": False,
                "error": "引用 ID 与本次已读取记录不一致。",
            }

        # 由宿主固定需求执行规则，模型只提供引用。
        session.result = check_supply_voltage(
            fixed_requirements,
            session.fact,
            session.evidence,
            document,
        )

        return {
            "ok": True,
            "constraint_result": session.result.model_dump(mode="json"),
        }

    return [
        read_adc_supply_evidence,
        check_adc_supply,
    ], session
