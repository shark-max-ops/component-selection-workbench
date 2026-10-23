"""创建持久化 ADC 任务，或在原线程中继续已完成的任务。"""

import argparse
import json
from pathlib import Path

from app.services.task_service import create_task, get_task_status, load_task, send_message


def main() -> None:
    """
    作用：通过命令行创建、查看或续聊持久化任务。
    输入：无
    输出：无
    """
    # 定义命令行输入，业务处理交给应用服务。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--voltage")
    parser.add_argument("--reviewed", action="store_true")
    parser.add_argument("--pdf", type=Path)
    parser.add_argument(
        "--message",
        default="复述原供电判定并说明未核验范围，不修改原任务。",
    )
    # 解析并检查参数组合，非法输入不会触发业务执行。
    args = parser.parse_args()

    if args.show and not args.task:
        parser.error("--show 需要 --task。")

    if args.task and (args.voltage is not None or args.pdf is not None or args.reviewed):
        parser.error("续接不修改需求、文档或审核状态；这些变化需新建任务。")

    # 已有任务直接加载；没有任务标识时创建固定快照。
    if args.task:
        meta = load_task(args.task)
    else:
        meta = create_task(voltage=args.voltage or "3.3", reviewed=args.reviewed, pdf=args.pdf)
    task_id = meta["task_id"]
    # 查询只打印已有状态，不进入模型执行流程。
    if args.show:
        print(json.dumps(get_task_status(task_id), ensure_ascii=False, indent=2))
        return
    print(f"task_id：{task_id}\nthread_id：{meta['thread_id']}", flush=True)
    # 将消息交给服务执行，随后向用户展示结果。
    result = send_message(task_id, args.message)
    print(f"run_id：{result['manifest']['run_id']}")
    print(f"持久化任务执行通过；供电判定：{result['report']['constraint_result']['status']}")


if __name__ == "__main__":
    main()
