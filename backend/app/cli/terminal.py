"""使用中文命令与对话操作供电核验任务。"""

import json
from pathlib import Path

from app.services.task_service import ROOT, create_task, get_task_status, load_task, send_message

HELP = """/new [电压]：创建任务，默认 3.3 V；可用 /new 3.3 | PDF路径 指定资料
/tasks：列出任务   /open 任务ID：打开任务
/status：查看状态   /evidence：查看证据   /report：查看程序结果
/help：帮助   /exit：退出
普通中文消息：发送给当前任务并执行。当前仅核验 ADS1115 供电。
接口、分辨率、通道和采样能力尚未核验；新任务采用固定示例需求。"""


def show_event(event, write=print):
    """
    作用：把真实工具和运行事件显示为简短中文进度。
    输入：事件字典 event 和输出函数 write。
    输出：无返回值；显示事件序号、类型和必要位置。
    """
    # 只展示可观察事件，不生成思维过程或估算百分比。
    labels = {
        "run_started": "运行开始",
        "run_finished": "运行结束",
        "tool_started": "工具开始",
        "tool_finished": "工具结束",
        "tool_failed": "工具失败",
    }
    payload = event["payload"]
    write(
        f"[{event['sequence']}] {labels[event['type']]} "
        f"{payload.get('tool', '')} {payload.get('path', '')} "
        f"{payload.get('status', payload.get('reason', ''))}"
    )


def run_terminal(*, root: Path = ROOT, read=input, write=print):
    """
    作用：运行终端命令循环，复用应用服务管理任务和中文对话。
    输入：任务根目录 root、输入函数 read、输出函数 write。
    输出：无返回值；用户退出或中断时结束循环。
    """
    # 选中的任务只保存在当前终端，重启后使用 /open 找回。
    current = None
    write(HELP)
    while True:
        try:
            line = read(f"{current or '未打开任务'}> ").strip()
            if not line:
                continue
            command, _, argument = line.partition(" ")
            argument = argument.strip()

            # 帮助和退出不读取业务状态，也不调用模型。
            if command == "/exit":
                write("已退出；已完成任务可用 /open 再次打开。")
                return
            if command == "/help":
                write(HELP)
                continue

            # 创建只固定输入；用户发送普通消息后才执行模型。
            if command == "/new":
                voltage, separator, pdf = argument.partition("|")
                meta = create_task(
                    voltage=voltage.strip() or "3.3",
                    root=root,
                    pdf=Path(pdf.strip().strip('"')) if separator else None,
                )
                current = meta["task_id"]
                write(f"已创建 {current}；仅供电核验，未审核。输入“开始核验”执行。")
                continue
            if command == "/tasks":
                tasks = sorted(root.glob("task-*/task.json")) if root.exists() else []
                write("\n".join(path.parent.name for path in tasks) or "暂无任务。")
                continue
            if command == "/open":
                meta = load_task(argument, root=root)
                current = meta["task_id"]
                write(f"已打开 {current}；当前仅供电核验。")
                continue

            # 拼错命令直接提示，避免把命令文本误发给付费模型。
            if command.startswith("/") and command not in {"/status", "/evidence", "/report"}:
                write("未知命令，请输入 /help。")
                continue
            if current is None:
                write("请先 /new 或 /open 任务ID。")
                continue

            # 查询直接读取保存的记录；报告显示结构化程序结果。
            if command in {"/status", "/evidence", "/report"}:
                status = get_task_status(current, root=root)
                key = {"/evidence": "evidence", "/report": "constraint_result"}.get(command)
                value = status if key is None else status["adc_records"].get(key)
                write(json.dumps(value, ensure_ascii=False, indent=2) if value else "尚无记录。")
                write("范围：仅供电；其他要求尚未核验。")
                continue

            # 对话才进入实际执行，并分别展示模型解释与程序核验。
            result = send_message(
                current, line, root=root, on_event=lambda event: show_event(event, write)
            )
            write("模型答复：" + str(result["reply"]))
            write("程序核验：" + json.dumps(result["report"], ensure_ascii=False, indent=2))
            write("范围：仅供电；其他要求尚未核验。")
        except (EOFError, KeyboardInterrupt):
            # 执行中断不承诺任意位置恢复；服务已记录失败清单。
            write("终端已退出；执行中断可能留下未完成节点，暂不支持自动恢复。")
            return
        except Exception as exc:
            # 不展示可能含配置的异常内容，也不把执行失败当作器件不满足。
            write(f"操作失败（{type(exc).__name__}）。请检查任务ID、输入或运行归档。")
