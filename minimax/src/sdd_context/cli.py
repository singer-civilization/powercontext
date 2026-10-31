from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .context import render_context
from .minimax_client import MiniMaxClient
from .models import TaskState
from .powercontext import PowerContextClient
from .seekdb_store import SeekDBDocumentStore

DEFAULT_PATTERNS = ("specs/**/*.md", "sample_project/src/**/*.py", "sample_project/tests/**/*.py")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdd-context", description="Build evidence-backed SDD context")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="MiniMax project root")
    parser.add_argument("--db", type=Path, help="embedded seekdb data directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("ingest", help="import specs, source and tests into seekdb")

    context_parser = subparsers.add_parser("context", help="retrieve and render engineering context")
    context_parser.add_argument("objective")
    context_parser.add_argument("--task-id", default="task-filter")
    context_parser.add_argument("--without-powercontext", action="store_true")
    context_parser.add_argument("--output", type=Path)

    save_parser = subparsers.add_parser("state-save", help="persist task progress in PowerContext")
    save_parser.add_argument("--file", type=Path, required=True)

    load_parser = subparsers.add_parser("state-load", help="recover task progress from PowerContext")
    load_parser.add_argument("task_id")

    generate_parser = subparsers.add_parser("generate", help="ask MiniMax for a context-grounded git patch")
    generate_parser.add_argument("objective")
    generate_parser.add_argument("--task-id", default="task-filter")
    generate_parser.add_argument("--output", type=Path, required=True)
    return parser


def project_files(root: Path) -> list[Path]:
    return sorted({path for pattern in DEFAULT_PATTERNS for path in root.glob(pattern) if path.is_file()})


def powercontext_client() -> PowerContextClient:
    scope_id = os.environ.get("POWERCONTEXT_SCOPE_ID", "")
    if not scope_id:
        raise SystemExit("POWERCONTEXT_SCOPE_ID is required for state operations")
    return PowerContextClient(
        os.environ.get("POWERCONTEXT_URL", "http://127.0.0.1:8000"),
        scope_id,
        os.environ.get("POWERCONTEXT_API_KEY", ""),
    )


def load_context(root: Path, db_path: Path, objective: str, task_id: str, use_powercontext: bool) -> str:
    state = powercontext_client().load_state(task_id) if use_powercontext else None
    with SeekDBDocumentStore(db_path) as store:
        hits = store.search(objective, limit=12)
    return render_context(objective, hits, state)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    db_path = (args.db or root / ".sdd-context" / "seekdb").resolve()

    if args.command == "ingest":
        files = project_files(root)
        with SeekDBDocumentStore(db_path) as store:
            documents = store.ingest(root, files)
        print(json.dumps({"database": str(db_path), "documents": len(documents)}, ensure_ascii=False))
        return 0

    if args.command == "state-save":
        state = TaskState.from_dict(json.loads(args.file.read_text(encoding="utf-8")))
        result = powercontext_client().save_state(state)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "state-load":
        state = powercontext_client().load_state(args.task_id)
        print(json.dumps(state.to_dict() if state else None, ensure_ascii=False, indent=2))
        return 0 if state else 1

    if args.command == "context":
        output = load_context(root, db_path, args.objective, args.task_id, not args.without_powercontext)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output, encoding="utf-8")
        else:
            print(output)
        return 0

    if args.command == "generate":
        context = load_context(root, db_path, args.objective, args.task_id, True)
        client = MiniMaxClient(
            os.environ.get("MINIMAX_API_KEY", ""),
            os.environ.get("MINIMAX_MODEL", "MiniMax-M3.1-Flash"),
            os.environ.get("MINIMAX_API_URL", "https://api.minimaxi.com/v1/chat/completions"),
            int(os.environ.get("MINIMAX_MAX_TOKENS", "16384")),
        )
        result = client.generate_patch(context, args.objective)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result.content, encoding="utf-8")
        metadata = {
            "provider": "MiniMax",
            "model": result.model,
            "request_id": result.request_id,
            "usage": result.usage,
            "context_sha256": __import__("hashlib").sha256(context.encode()).hexdigest(),
            "patch": str(args.output),
        }
        args.output.with_suffix(args.output.suffix + ".json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(metadata, ensure_ascii=False, indent=2))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
