from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import home_dir
from .store import VaultStore, iter_source_files


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="researchvault",
        description="ResearchVault：笔记/论文速查（嵌入式 seekdb）",
    )
    parser.add_argument("--version", action="version", version=f"researchvault {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="递归导入 .md/.txt 笔记并切块入库")
    add.add_argument("paths", nargs="+", type=Path, metavar="path")

    search = sub.add_parser("search", help="关键词打分检索，输出 文件:行号 + 摘录")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--json", action="store_true", dest="as_json")

    stats = sub.add_parser("stats", help="文档数、块数、最近导入时间")
    stats.add_argument("--json", action="store_true", dest="as_json")
    return parser


def cmd_add(store: VaultStore, args: argparse.Namespace) -> int:
    base = Path.cwd()
    imported = unchanged = missing = 0
    for target in args.paths:
        files = iter_source_files(target)
        if not files:
            print(f"跳过（不存在或没有 .md/.txt）: {target}", file=sys.stderr)
            missing += 1
            continue
        for file_path in files:
            result = store.upsert_file(file_path, base_dir=base)
            if result.status == "unchanged":
                print(f"= {result.rel_path}（内容未变，跳过）")
                unchanged += 1
            else:
                print(f"+ {result.rel_path}（{result.chunks} 块）")
                imported += 1
    print(f"导入 {imported} 个文件，跳过 {unchanged} 个未变化文件" + (f"，{missing} 个路径无效" if missing else ""))
    return 0


def cmd_search(store: VaultStore, args: argparse.Namespace) -> int:
    hits = store.search(args.query, limit=args.limit)
    if args.as_json:
        print(json.dumps([hit.to_dict() for hit in hits], ensure_ascii=False, indent=2))
        return 0
    if not hits:
        print(f"没有匹配“{args.query}”的内容。")
        return 0
    for hit in hits:
        print(f"{hit.file}:{hit.line}  [{hit.score:.1f}]  {hit.excerpt}")
    return 0


def cmd_stats(store: VaultStore, args: argparse.Namespace) -> int:
    stats = store.stats()
    if args.as_json:
        print(json.dumps(
            {"documents": stats.documents, "chunks": stats.chunks, "last_imported_at": stats.last_imported_at},
            ensure_ascii=False, indent=2,
        ))
        return 0
    print(f"文档数: {stats.documents}")
    print(f"块数:   {stats.chunks}")
    print(f"最近导入: {stats.last_imported_at}")
    print(f"数据目录: {home_dir()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"add": cmd_add, "search": cmd_search, "stats": cmd_stats}
    with VaultStore() as store:
        return handlers[args.command](store, args)


if __name__ == "__main__":
    sys.exit(main())
