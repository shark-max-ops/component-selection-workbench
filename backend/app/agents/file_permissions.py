"""声明工作台虚拟文件的读写边界。"""

from deepagents import FilesystemPermission


def build_file_permissions() -> list[FilesystemPermission]:
    """
    作用：构造有序文件权限。
    输入：无。
    输出：本次 Agent 使用的新规则列表。
    要点：具体允许在前，全局拒绝在后；只控制内置文件工具。
    """
    return [
        FilesystemPermission(
            operations=["read"],
            paths=[
                "/inputs",
                "/inputs/**",
                "/documents",
                "/documents/**",
                "/skills",
                "/skills/**",
                "/outputs",
                "/outputs/**",
                "/large_tool_results",
                "/large_tool_results/**",
                "/conversation_history",
                "/conversation_history/**",
            ],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=["/outputs", "/outputs/**"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/**"],
            mode="deny",
        ),
    ]
