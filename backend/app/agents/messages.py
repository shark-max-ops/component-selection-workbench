"""从工具消息统计真实读取记录。"""

from langchain_core.messages import AIMessage, ToolMessage


def successful_reads(messages: list) -> set[str]:
    """
    作用：提取实际成功的文件读取路径。
    输入：运行中的消息列表 messages。
    输出：实际读取成功的虚拟文件路径集合。
    """
    successful_ids = {
        item.tool_call_id
        for item in messages
        if isinstance(item, ToolMessage) and item.status == "success"
    }

    paths = set()

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
