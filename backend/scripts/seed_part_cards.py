"""为 B1 样本资料写入三张可浏览的中文器件简介。"""

from app.infrastructure.library import list_documents, save_part_card


def main() -> None:
    """
    作用：按已登记手册的编号和修订保存 B1 器件简介。
    输入：无，读取当前资料库。
    输出：无，写入数据库并导出 Markdown。
    """
    documents = {(d["document_number"], d["revision"]): d for d in list_documents()}
    cards = (
        (
            "ADS1115",
            "TI 的 16 位 I²C 模数转换器，可选择四路单端或两路差分输入。"
            "适合先列为多路、较高分辨率需求的候选；供电范围、每通道速率和切换条件要再查手册。",
            ("SBAS444", "E"),
        ),
        (
            "ADS1015",
            "TI 的 12 位 I²C 模数转换器，可选择四路单端或两路差分输入。"
            "与 ADS1115 在分辨率和最高数据率上不同；具体约束要再查手册。",
            ("SBAS473", "F"),
        ),
        (
            "MCP3208",
            "Microchip 的 12 位 SPI 模数转换器，提供八个输入通道。"
            "最高吞吐量与供电等条件有关；多通道需求需核对切换和每通道有效速率。",
            ("DS21298", "E"),
        ),
    )
    # 每张简介都引用已导入的当前手册，不从自由文本猜测来源。
    for part, summary, key in cards:
        if key not in documents:
            raise ValueError(f"缺少 {part} 的来源手册：{key}")
        save_part_card(part, summary, documents[key]["id"])


if __name__ == "__main__":
    main()
