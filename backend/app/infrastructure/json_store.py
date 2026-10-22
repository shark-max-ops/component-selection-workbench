"""JSON 文件归档。"""

import json
from pathlib import Path


def save_json(path: Path, data: object) -> None:
    """
    作用：保存 UTF-8 JSON；父目录须由调用方创建。
    输入：目标文件路径 path 和待保存数据 data。
    输出：无返回值；将 JSON 写入指定文件。
    """
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
