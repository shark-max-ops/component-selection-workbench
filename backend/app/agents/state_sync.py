"""把供电业务记录纳入图状态，使其能随检查点保存和恢复。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from app.contracts import ConstraintResult, Evidence, ParameterFact

if TYPE_CHECKING:
    from app.agents.tools import SupplySession


def merge_adc_records(current: dict, incoming: dict) -> dict:
    """
    作用：合并业务快照并拒绝冲突记录。
    输入：current - 已有记录；incoming - 待合并记录。
    输出：dict，合并后的业务记录。
    """
    # 重复值复用，冲突值拒绝，空值不覆盖已有记录。
    merged = dict(current)

    for key, value in incoming.items():
        if value is None:
            continue
        if key in merged and merged[key] != value:
            raise ValueError(f"同一任务的业务记录发生冲突：{key}")
        merged[key] = value

    return merged


class PersistentAdcState(AgentState, total=False):
    """
    作用：为 Agent 状态增加带冲突校验的业务记录字段。
    输入：AgentState 字段；adc_records - 业务记录字典。
    输出：包含供电业务快照的图状态。
    """

    adc_records: Annotated[dict[str, Any], merge_adc_records]


def snapshot_session(session: SupplySession) -> dict:
    """
    作用：把运行时供电对象转换为可持久化快照。
    输入：session - 当前 SupplySession 对象。
    输出：dict，包含证据、参数和判定的 JSON 兼容数据。
    """
    return {
        "evidence": (session.evidence.model_dump(mode="json") if session.evidence else None),
        "parameter_fact": (session.fact.model_dump(mode="json") if session.fact else None),
        "constraint_result": (session.result.model_dump(mode="json") if session.result else None),
    }


def restore_session(state: dict, session: SupplySession) -> None:
    """
    作用：从图状态恢复供电业务对象并校验引用。
    输入：state - 图状态；session - 待恢复的 SupplySession 对象。
    输出：无
    """
    # 没有记录时不恢复；不执行新的业务核验。
    records = state.get("adc_records")
    if records is None:
        return

    # 将快照重新校验为契约对象，缺失记录保留为空。
    session.evidence = (
        Evidence.model_validate(records["evidence"]) if records.get("evidence") else None
    )
    session.fact = (
        ParameterFact.model_validate(records["parameter_fact"])
        if records.get("parameter_fact")
        else None
    )
    session.result = (
        ConstraintResult.model_validate(records["constraint_result"])
        if records.get("constraint_result")
        else None
    )

    # 核对证据、参数和判定的引用链，防止恢复时串用记录。
    if session.fact is not None and (
        session.evidence is None or session.fact.evidence_ids != [session.evidence.id]
    ):
        raise ValueError("恢复的参数与证据引用不一致。")

    if session.result is not None and (
        session.fact is None
        or session.result.fact_ids != [session.fact.id]
        or session.result.evidence_ids != session.fact.evidence_ids
        or session.result.part_number != session.fact.part_number
    ):
        raise ValueError("恢复的判定与业务记录引用不一致。")


class PersistAdcRecords(AgentMiddleware):
    """
    作用：在工具执行及对话开始时同步供电业务状态。
    输入：session - 当前 SupplySession 对象。
    输出：PersistAdcRecords 业务状态中间件对象。
    """

    state_schema = PersistentAdcState

    def __init__(self, session: SupplySession):
        """
        作用：绑定当前任务的运行时供电对象。
        输入：session - 当前 SupplySession 对象。
        输出：无
        """
        self.session = session

    def before_agent(self, state, runtime):
        """
        作用：在 Agent 执行前恢复运行时业务对象。
        输入：state - 图状态；runtime - 框架运行上下文。
        输出：无
        """
        restore_session(state, self.session)
        return None

    def wrap_tool_call(self, request, handler):
        """
        作用：执行工具并将供电业务快照写回图状态。
        输入：request - 工具请求；handler - 实际执行器。
        输出：普通工具的原始响应，或携带业务状态更新的 Command。
        """
        # 非供电工具保留原响应，供电工具继续同步业务状态。
        response = handler(request)

        if request.tool_call["name"] not in {
            "read_adc_supply_evidence",
            "check_adc_supply",
        }:
            return response

        if not isinstance(response, ToolMessage):
            raise TypeError("当前两个业务工具应返回普通工具消息。")

        # 将工具消息与宿主业务快照一并交回图运行时。
        return Command(
            update={
                "messages": [response],
                "adc_records": snapshot_session(self.session),
            }
        )
