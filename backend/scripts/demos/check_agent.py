"""检查配置、普通模型调用及 DeepAgents 的真实工具执行。"""

import secrets

from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from app.config import load_settings
from app.infrastructure.llm import create_chat_model


def main() -> None:
    """
    作用：运行模型与 Agent 检查。
    输入：backend/.env 中的模型配置。
    输出：终端中的阶段结果；失败时抛出异常并返回非零退出码。
    要点：会产生真实 API 调用；通过工具返回的随机标记验证执行。
    边界：本检查不证明 PDF 解析、选型准确性或故障恢复已经实现。
    """
    settings = load_settings()
    print("[1/3] 配置检查通过")
    print(f"当前模型：{settings.llm_model}")

    model = create_chat_model(settings)

    # 先单独检查普通对话，便于区分连接故障和工具调用故障。
    reply = model.invoke("这是连接检查，请用一句简短中文回答：连接成功。")
    if not isinstance(reply, AIMessage) or not reply.content:
        raise RuntimeError("模型没有返回有效的普通对话内容。")

    print("[2/3] 普通模型调用通过")

    # 标记仅保存在本地工具闭包中，不提前放进提示词。
    probe_token = secrets.token_hex(16)

    @tool
    def get_runtime_probe() -> str:
        """
        作用：读取本次运行的检查标记。
        输入：无。
        输出：当前进程生成的随机字符串。
        要点：只用于验证工具执行，不读取文件或访问网络。
        """
        return probe_token

    agent = create_deep_agent(
        model=model,
        tools=[get_runtime_probe],
        system_prompt=(
            "你正在执行工具调用检查。"
            "必须直接调用 get_runtime_probe 获取本次运行标记，"
            "读取工具返回值后，用一句简短中文说明检查完成。"
            "不要委派任务，不要使用文件工具，也不要编造工具结果。"
        ),
    )

    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "请调用 get_runtime_probe，完成本次工具检查。",
                }
            ]
        },
        # 限制图执行步数，防止异常情况下持续循环。
        config={"recursion_limit": 12},
    )

    messages = result["messages"]

    # 检查真实工具返回，而非模型最终回答中的成功声明。
    probe_succeeded = any(
        isinstance(message, ToolMessage)
        and message.name == "get_runtime_probe"
        and message.status == "success"
        and message.content == probe_token
        for message in messages
    )

    if not probe_succeeded:
        raise RuntimeError("未找到有效的检查工具返回：模型可能未调用工具，或工具执行失败。")

    final_message = messages[-1]
    if (
        not isinstance(final_message, AIMessage)
        or final_message.tool_calls
        or not final_message.content
    ):
        raise RuntimeError("工具执行后，Agent 未返回完整的最终回复。")

    print("[3/3] DeepAgents 工具调用通过")
    print(f"Agent 回复：{final_message.content}")
    print("检查完成：配置、模型连接、工具执行均通过。")


if __name__ == "__main__":
    main()
