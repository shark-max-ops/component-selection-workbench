"""脚本模型提出固定请求，真实框架执行文件工具及权限检查。"""

import pytest
from deepagents.backends.utils import create_file_data, file_data_to_string
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage

import app.agents.harness as harness

SEED = {
    "/inputs/task.json": '{"voltage": "6"}',
    "/documents/pages/0004.txt": "Original document text.",
    "/skills/adc-supply-verification/SKILL.md": "Original skill text.",
    "/outputs/report.json": "Original report.",
    "/large_tool_results/demo.txt": "Example offloaded result.",
    "/conversation_history/demo.txt": "Example archived history.",
    "/outside.txt": "Outside the allowlist.",
}


class OfflineToolModel(FakeMessagesListChatModel):
    """
    作用：回放预设工具请求以隔离远程模型服务。
    输入：responses - 固定模型响应列表；其他父类模型字段。
    输出：OfflineToolModel 离线模型对象。
    """

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """
        作用：接受工具绑定并保留模型响应游标。
        输入：tools - 工具列表；tool_choice - 工具选择策略；kwargs - 其他绑定选项。
        输出：当前离线模型对象。
        """
        return self

    def get_num_tokens_from_messages(self, messages, tools=None):
        """
        作用：提供测试用的粗略消息计数。
        输入：messages - 消息列表；tools - 可选工具列表。
        输出：int，测试用的粗略 token 数量。
        """
        return 1 + sum(len(str(item.content)) for item in messages) // 4


@pytest.fixture
def run_operation(monkeypatch):
    """
    作用：替换模型依赖并提供文件工具执行函数。
    输入：monkeypatch - pytest 依赖替换工具。
    输出：执行单个工具请求的 run 函数。
    """
    # 关闭外部追踪并替换配置读取，测试仅使用离线模型。
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setattr(harness, "load_settings", lambda: None)

    def run(name, args):
        """
        作用：执行预设文件工具请求并提取最终虚拟文件。
        输入：name - 工具名称；args - 工具参数。
        输出：tuple，工具响应和最终文件内容字典。
        """
        # 固定模型只发出一个工具请求，文件操作仍由真实框架执行。
        model = OfflineToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": name,
                            "args": args,
                            "id": "permission-test",
                        }
                    ],
                ),
                AIMessage(content="Finished scripted request."),
            ]
        )

        monkeypatch.setattr(
            harness,
            "create_chat_model",
            lambda _: model,
        )

        agent = harness.create_workbench_agent()

        state = agent.invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "Run the scripted request.",
                    }
                ],
                "files": {path: create_file_data(text) for path, text in SEED.items()},
            },
            config={"recursion_limit": 8},
        )

        # 匹配本次调用的唯一工具响应，并导出最终工作区供断言使用。
        replies = [
            item
            for item in state["messages"]
            if isinstance(item, ToolMessage) and item.tool_call_id == "permission-test"
        ]

        assert len(replies) == 1
        assert replies[0].name == name

        files = {path: file_data_to_string(data) for path, data in state["files"].items()}

        return replies[0], files

    return run


@pytest.mark.parametrize(
    "path",
    [
        "/inputs/task.json",
        "/documents/pages/0004.txt",
        "/skills/adc-supply-verification/SKILL.md",
        "/outputs/report.json",
        "/large_tool_results/demo.txt",
        "/conversation_history/demo.txt",
    ],
)
def test_allowed_read(run_operation, path):
    """
    作用：读取已有文件成功，内容未变。
    输入：run_operation - 文件工具执行函数；path - 待测虚拟文件路径。
    输出：无
    """
    reply, files = run_operation(
        "read_file",
        {"file_path": path},
    )

    assert reply.status == "success"
    assert SEED[path] in str(reply.content)
    assert files == SEED


def test_skill_directory_can_be_listed(run_operation):
    """
    作用：目录本身也要能匹配读取规则。
    输入：run_operation - 文件工具执行函数。
    输出：无
    """
    reply, files = run_operation(
        "ls",
        {"path": "/skills"},
    )

    assert reply.status == "success"
    assert "adc-supply-verification" in str(reply.content)
    assert files == SEED


@pytest.mark.parametrize(
    "path",
    ["/outputs/new.json", "/outputs/nested/new.json"],
)
def test_allowed_create(run_operation, path):
    """
    作用：允许在报告目录及子目录创建文件。
    输入：run_operation - 文件工具执行函数；path - 待测虚拟文件路径。
    输出：无
    """
    reply, files = run_operation(
        "write_file",
        {"file_path": path, "content": "new"},
    )

    assert reply.status == "success"
    assert files == SEED | {path: "new"}


def test_allowed_overwrite(run_operation):
    """
    作用：允许覆盖已有报告，不能因框架不支持覆盖而误判权限。
    输入：run_operation - 文件工具执行函数。
    输出：无
    """
    reply, files = run_operation(
        "write_file",
        {
            "file_path": "/outputs/report.json",
            "content": "updated",
        },
    )

    assert reply.status == "success"
    assert files == SEED | {"/outputs/report.json": "updated"}


@pytest.mark.parametrize(
    "path",
    [
        "/inputs/task.json",
        "/documents/pages/0004.txt",
        "/skills/adc-supply-verification/SKILL.md",
        "/outside.txt",
        "/large_tool_results/demo.txt",
    ],
)
def test_denied_overwrite_preserves_files(run_operation, path):
    """
    作用：拒绝覆盖只读或未开放文件，检查整个工作区没有变化。
    输入：run_operation - 文件工具执行函数；path - 待测虚拟文件路径。
    输出：无
    """
    reply, files = run_operation(
        "write_file",
        {"file_path": path, "content": "changed"},
    )

    assert reply.status == "error"
    assert "permission" in str(reply.content).lower()
    assert files == SEED


def test_denied_edit_preserves_input(run_operation):
    """
    作用：拒绝通过 edit_file 修改需求电压。
    输入：run_operation - 文件工具执行函数。
    输出：无
    """
    reply, files = run_operation(
        "edit_file",
        {
            "file_path": "/inputs/task.json",
            "old_string": "6",
            "new_string": "3.3",
        },
    )

    assert reply.status == "error"
    assert "permission" in str(reply.content).lower()
    assert files == SEED


@pytest.mark.parametrize(
    "path",
    ["/documents/pages/0004.txt", "/documents"],
)
def test_denied_delete_preserves_documents(run_operation, path):
    """
    作用：删除文件或整目录均被拒绝，原文保留。
    输入：run_operation - 文件工具执行函数；path - 待测虚拟文件路径。
    输出：无
    """
    reply, files = run_operation(
        "delete",
        {"file_path": path},
    )

    assert reply.status == "error"
    assert "permission" in str(reply.content).lower()
    assert files == SEED


def test_denied_read_existing_outside_file(run_operation):
    """
    作用：未开放文件即使存在也不能读取。
    输入：run_operation - 文件工具执行函数。
    输出：无
    """
    reply, files = run_operation(
        "read_file",
        {"file_path": "/outside.txt"},
    )

    assert reply.status == "error"
    assert "permission" in str(reply.content).lower()
    assert SEED["/outside.txt"] not in str(reply.content)
    assert files == SEED


def test_denied_create_outside(run_operation):
    """
    作用：全局拒绝规则阻止在未知位置新建文件。
    输入：run_operation - 文件工具执行函数。
    输出：无
    """
    reply, files = run_operation(
        "write_file",
        {
            "file_path": "/unknown/new.json",
            "content": "changed",
        },
    )

    assert reply.status == "error"
    assert "permission" in str(reply.content).lower()
    assert files == SEED
