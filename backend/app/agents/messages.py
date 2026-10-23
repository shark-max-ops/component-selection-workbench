"""从工具消息统计真实读取记录。"""

from langchain_core.messages import AIMessage, ToolMessage


def successful_reads(messages: list) -> set[str]:
    """
    作用：提取工具实际成功读取的虚拟文件路径。
    输入：messages - 运行消息列表。
    输出：set[str]，成功读取的文件路径集合。
    """
    # 先收集成功工具响应的调用 ID，排除模型提出但未成功执行的请求。
    successful_ids = {
        item.tool_call_id
        for item in messages
        if isinstance(item, ToolMessage) and item.status == "success"
    }

    paths = set()

    # 将成功响应与原读取请求对应，提取实际使用的文件路径。
    for message in messages:
        if not isinstance(message, AIMessage):
            continue

        for call in message.tool_calls:
            if call["name"] == "read_file" and call["id"] in successful_ids:
                args = call["args"]
                path = args.get(
                    "file_path",
                    args.get("path"),
                )
                if isinstance(path, str):
                    paths.add(path)

    return paths
