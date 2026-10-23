"""通过 uv 运行的资料库登记与查询入口。"""

import argparse
import json
from pathlib import Path

from app.infrastructure.library import (
    compare_markitdown,
    enrich_sections,
    import_pdf,
    index_legacy_tasks,
    library_summary,
    list_documents,
    list_pages,
    list_part_cards,
    save_part_card,
)


def main() -> None:
    """
    作用：解析资料库命令并显示简短结果。
    输入：命令行参数。
    输出：无；向终端输出 JSON 结果。
    """
    parser = argparse.ArgumentParser(description="器件资料库")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("import", help="登记一份 PDF")
    add.add_argument("pdf", type=Path)
    add.add_argument("--manufacturer", required=True)
    add.add_argument("--kind", required=True)
    add.add_argument("--part", action="append", required=True)
    add.add_argument("--number", required=True)
    add.add_argument("--revision", required=True)
    add.add_argument("--url", required=True)
    commands.add_parser("list", help="列出资料")
    pages = commands.add_parser("pages", help="列出页面解析状态")
    pages.add_argument("document_id")
    compare = commands.add_parser("compare", help="试验 MarkItDown 转换")
    compare.add_argument("document_id")
    card = commands.add_parser("card", help="保存器件短简介")
    card.add_argument("part_number")
    card.add_argument("source_document_id")
    card.add_argument("summary")
    commands.add_parser("cards", help="列出器件简介")
    commands.add_parser("index-legacy", help="只读旧任务并建立业务元信息索引")
    commands.add_parser("enrich-sections", help="补记页面章节标题提示")
    commands.add_parser("summary", help="资料与旧记录数量摘要")
    args = parser.parse_args()
    if args.command == "import":
        result = import_pdf(
            args.pdf,
            manufacturer=args.manufacturer,
            kind=args.kind,
            parts=args.part,
            document_number=args.number,
            revision=args.revision,
            source_url=args.url,
        )
    elif args.command == "list":
        result = list_documents()
    elif args.command == "pages":
        result = list_pages(args.document_id)
    elif args.command == "compare":
        result = compare_markitdown(args.document_id)
    elif args.command == "cards":
        result = list_part_cards()
    elif args.command == "index-legacy":
        result = index_legacy_tasks()
    elif args.command == "enrich-sections":
        result = {"processed_chunks": enrich_sections()}
    elif args.command == "summary":
        result = library_summary()
    else:
        save_part_card(args.part_number, args.summary, args.source_document_id)
        result = {"saved": args.part_number}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
