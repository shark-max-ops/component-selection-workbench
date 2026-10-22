"""记录真实工具事件，供终端展示和运行复查。"""

import json
from datetime import UTC, datetime
from threading import Lock

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.types import Command


class RunEvents(AgentMiddleware):
    """
    作用：将真实工具调用转换为有序事件，并保存到当前运行目录。
    输入：任务标识、运行标识、事件文件路径和可选展示回调。
    输出：工具中间件；事件逐行写入 JSONL，不保存内部思维或完整工具内容。
    """

    def __init__(self, task_id, run_id, path, on_event=None):
        """
        作用：绑定本次运行的事件出口。
        输入：任务与运行标识、文件路径、可选回调 on_event。
        输出：初始化后的事件记录器。
        """
        # 每次运行从 1 编号；锁保证并行工具写入时不混行。
        self.task_id, self.run_id = task_id, run_id
        self.path, self.on_event = path, on_event
        self.sequence = 0
        self.lock = Lock()

    def emit(self, event_type, **payload):
        """
        作用：保存并展示一条真实事件。
        输入：事件类型和必要载荷。
        输出：无返回值；追加事件文件并通知展示回调。
        """
        # 先落盘再通知界面，界面故障不能破坏业务执行。
        with self.lock:
            self.sequence += 1
            event = dict(
                task_id=self.task_id,
                run_id=self.run_id,
                sequence=self.sequence,
                time=datetime.now(UTC).isoformat(),
                type=event_type,
                payload=payload,
            )
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            if self.on_event:
                try:
                    self.on_event(event)
                except Exception:
                    # 展示失败不改变工具结果；磁盘事件仍可复查。
                    pass

    def wrap_tool_call(self, request, handler):
        """
        作用：围绕真实工具执行记录成对的开始和终止事件。
        输入：框架工具请求 request 与真实执行器 handler。
        输出：未经改写的工具响应；异常记录后继续抛出。
        """
        # 只展示名称、调用标识和文件位置，不记录完整工具参数。
        call = request.tool_call
        detail = dict(tool=call["name"], call_id=call["id"])
        path = call["args"].get("file_path")
        if isinstance(path, str):
            detail["path"] = path
        self.emit("tool_started", **detail)
        try:
            response = handler(request)
        except (Exception, KeyboardInterrupt) as exc:
            self.emit("tool_failed", **detail, reason=type(exc).__name__)
            raise

        # 部分业务中间件返回 Command，从中取消息判断执行状态。
        messages = (
            response.update.get("messages", [])
            if isinstance(response, Command) and isinstance(response.update, dict)
            else [response]
        )
        failed = any(isinstance(item, ToolMessage) and item.status == "error" for item in messages)
        self.emit(
            "tool_failed" if failed else "tool_finished",
            **detail,
            reason="工具返回错误，详见 messages.json" if failed else "执行结束",
        )
        return response
