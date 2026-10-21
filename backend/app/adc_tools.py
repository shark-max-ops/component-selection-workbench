"""将现有供电业务函数接入 Agent，并保留宿主侧的实际业务记录。"""

from dataclasses import dataclass

from langchain_core.tools import BaseTool, tool

from app.contracts import (
    AdcRequirements,
    ConstraintResult,
    Contract,
    Evidence,
    ParameterFact,
    Text,
)
from app.documents import RegisteredDocument, extract_ads1115_supply
from app.rules import check_supply_voltage


class EvidenceQuery(Contract):
    """证据查询参数；继承禁止未知字段的配置。"""

    document_id: Text
    part_number: Text


class SupplyQuery(Contract):
    """核验只接受引用 ID，不接受模型传入电压或审核状态。"""

    fact_id: Text
    evidence_id: Text


@dataclass
class SupplySession:
    """保存本次实际工具产物。

    由宿主程序持有，模型不能通过修改虚拟文件直接修改此对象。
    """

    evidence: Evidence | None = None
    fact: ParameterFact | None = None
    result: ConstraintResult | None = None


def build_adc_tools(
    requirements: AdcRequirements,
    document: RegisteredDocument,
    reviewed: bool = False,
) -> tuple[list[BaseTool], SupplySession]:
    """创建绑定本次任务的业务工具。

    输入：需求、文档和用户声明的人工审核状态。
    输出：工具列表及宿主业务记录。
    边界：仅覆盖 ADS1115 供电，不提供任意路径或代码执行入口。
    """
    # 使用需求副本，避免调用方后续修改原对象影响当前任务。
    fixed_requirements = requirements.model_copy(deep=True)
    session = SupplySession()

    @tool(args_schema=EvidenceQuery)
    def read_adc_supply_evidence(
        document_id: str,
        part_number: str,
    ) -> dict:
        """读取指定文档中 ADS1115 的推荐供电证据与参数声明。"""
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
        """对已读取的参数和证据执行固定需求下的供电核验。"""
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
