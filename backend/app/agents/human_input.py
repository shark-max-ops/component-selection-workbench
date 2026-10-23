"""在模型执行前持久化等待事项，使用 LangGraph 中断恢复。"""

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.types import interrupt


class HumanInputError(ValueError):
    """
    作用：向终端提供可直接展示的等待或恢复操作提示。
    输入：message - 安全的业务提示。
    输出：人工输入错误对象。
    """


class HumanInputState(AgentState, total=False):
    """
    作用：保存待回答事项与最近一次恢复内容。
    输入：human_request - 等待事项；human_response - 恢复内容。
    输出：可持久化的图状态。
    """

    human_request: dict | None
    human_response: dict | None


class AwaitHumanInput(AgentMiddleware):
    """
    作用：在显式等待点暂停，并将恢复答案传给后续模型。
    输入：无
    输出：人工输入中间件。
    """

    state_schema = HumanInputState

    def before_agent(self, state, runtime):
        """
        作用：等待宿主通过 Command 恢复当前事项。
        输入：state - 图状态；runtime - 框架上下文。
        输出：清除等待事项并记录答案的状态更新，或无。
        """
        request = state.get("human_request")
        if not request:
            return None
        # 中断前不执行写入；恢复时此节点会从头重放。
        answer = interrupt(request)
        return {
            "human_request": None,
            "human_response": {"request": request, "answer": answer},
            "messages": [
                HumanMessage(
                    content=(
                        f"已回答等待事项：{request['question']}。用户回复：{answer}。"
                        "保留原需求，审核结果以宿主业务记录和 /inputs/review.json 为准。"
                    )
                )
            ],
        }


def pending_interrupts(checkpoint):
    """
    作用：从检查点待处理写入中读取真实中断。
    输入：checkpoint - SqliteSaver 返回的检查点元组。
    输出：包含中断 ID 和等待内容的列表。
    """
    if checkpoint is None:
        return []
    # interrupt 由框架写入 pending_writes，不用业务布尔标记模拟暂停。
    return [
        {"id": item.id, "value": item.value}
        for _, channel, values in checkpoint.pending_writes or []
        if channel == "__interrupt__"
        for item in values
    ]
