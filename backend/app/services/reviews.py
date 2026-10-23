"""宿主人工审核记录：用 SQLite 事务串行写入，保留每版快照。"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def task_write(task_dir: Path):
    """
    作用：在事务中串行执行同任务写操作。
    输入：task_dir - 任务目录。
    输出：上下文管理器，进入后提供持有写锁的 SQLite 连接。
    """
    # 立即申请当前任务的写锁，锁冲突时交由调用方处理。
    connection = sqlite3.connect(task_dir / "reviews.sqlite", timeout=0)
    try:
        connection.execute("BEGIN IMMEDIATE")
        # 初始化审核表，由版本主键与请求唯一约束保护有效记录。
        connection.execute(
            "CREATE TABLE IF NOT EXISTS reviews ("
            "revision INTEGER PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, "
            "payload TEXT NOT NULL, record TEXT NOT NULL)"
        )
        # 让调用方在同一事务内完成写入，成功后统一提交。
        yield connection
        connection.commit()
    # 异常时撤销本事务写入，随后释放连接和任务锁。
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def read_reviews(task_dir: Path) -> list[dict]:
    """
    作用：只读获取已提交的审核历史。
    输入：task_dir - 任务目录。
    输出：list[dict]，按版本排序的审核记录；没有审核时为空列表。
    """
    # 查询不创建审核库，也不为尚无审核表的任务生成记录。
    database = task_dir / "reviews.sqlite"
    if not database.exists():
        return []
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='reviews'"
        ).fetchone()
        if not exists:
            return []
        # 按版本顺序返回已提交快照，供状态查询和审核历史展示使用。
        return [
            json.loads(row[0])
            for row in connection.execute("SELECT record FROM reviews ORDER BY revision")
        ]
