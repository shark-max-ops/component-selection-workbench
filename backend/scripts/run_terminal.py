"""终端入口：在 backend 下运行 uv run python -m scripts.run_terminal。"""

from app.cli.terminal import run_terminal

if __name__ == "__main__":
    # 终端只负责交互，任务与核验逻辑由应用服务复用。
    run_terminal()
