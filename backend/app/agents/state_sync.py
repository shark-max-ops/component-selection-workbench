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
    作用：合并业务记录。
    输入：已有记录和本次更新。
    输出：合并后的记录。
    要点：重复值复用，冲突值拒绝，空值不覆盖已有记录。
    """
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
    作用：增加业务记录字段，并处理同一步多个工具的状态更新。
    输入：类定义字段或当前调用上下文，见下方说明。
    输出：原有处理结果；具体返回值或副作用见下方实现。
    """

    adc_records: Annotated[dict[str, Any], merge_adc_records]


def snapshot_session(session: SupplySession) -> dict:
    """
    作用：输入运行时业务记录，输出可写入检查点的普通 JSON 数据。
    输入：session，含义见字段和下方说明。
    输出：原有处理结果；具体返回值或副作用见下方实现。
    """
    return {
        "evidence": (session.evidence.model_dump(mode="json") if session.evidence else None),
        "parameter_fact": (session.fact.model_dump(mode="json") if session.fact else None),
        "constraint_result": (session.result.model_dump(mode="json") if session.result else None),
    }


def restore_session(state: dict, session: SupplySession) -> None:
    """
    作用：根据已保存图状态重建运行时对象。
    输入：图状态和本次创建的 SupplySession。
    输出：原地更新运行时对象。
    边界：没有记录时不恢复；不执行新的业务核验。
    """
    records = state.get("adc_records")
    if records is None:
        return

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
    作用：在业务工具返回后同步图状态，在下一次执行前恢复业务对象。
    输入：类定义字段或当前调用上下文，见下方说明。
    输出：原有处理结果；具体返回值或副作用见下方实现。
    """

    state_schema = PersistentAdcState

    def __init__(self, session: SupplySession):
        """
        作用：输入当前运行时对象，绑定本任务的业务记录镜像。
        输入：session，含义见字段和下方说明。
        输出：原有处理结果；具体返回值或副作用见下方实现。
        """
        self.session = session

    def before_agent(self, state, runtime):
        """
        作用：每次 invoke 开始时，从线程状态恢复业务对象。
        输入：state、runtime，含义见字段和下方说明。
        输出：原有处理结果；具体返回值或副作用见下方实现。
        """
        restore_session(state, self.session)
        return None

    def wrap_tool_call(self, request, handler):
        """
        作用：执行真实工具，再将消息与业务记录交回图运行时。
        输入：request、handler，含义见字段和下方说明。
        输出：原有处理结果；具体返回值或副作用见下方实现。
        """
        response = handler(request)

        if request.tool_call["name"] not in {
            "read_adc_supply_evidence",
            "check_adc_supply",
        }:
            return response

        if not isinstance(response, ToolMessage):
            raise TypeError("当前两个业务工具应返回普通工具消息。")

        return Command(
            update={
                "messages": [response],
                "adc_records": snapshot_session(self.session),
            }
        )
