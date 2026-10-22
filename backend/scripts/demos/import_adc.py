"""导入本地 ADS1115 PDF，保存证据、参数和首条供电核验结果。"""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from app.adc.extractors import extract_ads1115_supply
from app.adc.rules import check_supply_voltage
from app.config import RUNTIME_DIR
from app.contracts import AdcRequirements
from app.infrastructure.documents import register_pdf

SOURCE_URL = "https://www.ti.com/lit/ds/symlink/ads1115.pdf"


def main() -> None:
    """
    作用：运行导入与供电演示。
    输入：PDF 路径、需求电压和可选的人工已核对标记。
    输出：文档版本目录，以及每次独立保存的 JSON 结果。
    边界：不调用模型；--reviewed 仅在实际核对 PDF 后使用。
    """
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--pdf",
        type=Path,
        default=RUNTIME_DIR / "raw" / "ads1115.pdf",
    )
    parser.add_argument("--voltage", default="3.3")
    parser.add_argument(
        "--reviewed",
        action="store_true",
        help="已人工核对供电表及型号适用性",
    )

    args = parser.parse_args()

    # 先验证需求，非法电压不会触发后面的文件写入。
    requirements = AdcRequirements(
        id=f"req-{uuid4().hex}",
        candidate_parts=["ADS1115"],
        supply_voltage_v=args.voltage,
        interface="I2C",
        min_resolution_bits=12,
        required_channels=4,
        min_samples_per_channel_sps="1000",
    )

    document = register_pdf(
        args.pdf,
        SOURCE_URL,
        RUNTIME_DIR / "documents",
    )

    evidence, fact = extract_ads1115_supply(document)
    fact.reviewed = args.reviewed

    result = check_supply_voltage(
        requirements,
        fact,
        evidence,
        document,
    )

    # 每次运行保留一份快照，不覆盖之前的判定。
    run_dir = RUNTIME_DIR / "runs" / requirements.id
    run_dir.mkdir(parents=True, exist_ok=False)

    payload = {
        "requirements": requirements.model_dump(mode="json"),
        "evidence": evidence.model_dump(mode="json"),
        "parameter_fact": fact.model_dump(mode="json"),
        "constraint_result": result.model_dump(mode="json"),
    }

    result_path = run_dir / "supply_check.json"
    result_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"版本：{document.version}；页数：{len(document.pages)}")
    print(f"PDF 页序：{evidence.pdf_page}；印刷页码：未登记")
    print(f"供电参数：{fact.value}")
    print(f"证据摘录：\n{evidence.quote}")
    print(f"判定：{result.status}；{result.reason}")

    for problem in result.unresolved:
        print(f"待确认：{problem}")

    print(f"文档目录：{document.directory}")
    print(f"结果文件：{result_path}")


if __name__ == "__main__":
    main()
