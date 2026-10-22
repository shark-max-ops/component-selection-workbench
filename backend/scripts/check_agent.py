"""保留旧命令入口；实现位于 scripts.demos.check_agent。"""

from scripts.demos.check_agent import main

if __name__ == "__main__":
    # 转交原演示流程，保持模块启动命令兼容。
    main()
