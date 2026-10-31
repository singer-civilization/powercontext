from __future__ import annotations

from collections import defaultdict

from .models import SearchHit, TaskState

MAX_DOCUMENT_CHARS = 4000
_LANGUAGES = {".py": "python", ".md": "markdown", ".toml": "toml", ".json": "json"}


def _language(path: str) -> str:
    for suffix, name in _LANGUAGES.items():
        if path.endswith(suffix):
            return name
    return "text"


def render_context(objective: str, hits: list[SearchHit], state: TaskState | None) -> str:
    grouped: dict[str, list[SearchHit]] = defaultdict(list)
    for hit in hits:
        grouped[hit.kind].append(hit)

    lines = ["# SDD Engineering Context", "", "## Objective", objective.strip(), ""]
    labels = (("spec", "Applicable specification"), ("code", "Code and symbols"), ("test", "Tests"))
    for kind, title in labels:
        lines.extend((f"## {title}", ""))
        selected = grouped.get(kind, [])
        if not selected:
            lines.append("- No matching document retrieved.")
        for hit in selected:
            symbol = f" symbols={hit.symbol}" if hit.symbol else ""
            lines.append(f"- [{hit.citation}] version={hit.version}{symbol} score={hit.score:.0f}: {hit.excerpt}")
            if hit.content:
                lines.extend(("", f"```{_language(hit.path)}", hit.content.rstrip()[:MAX_DOCUMENT_CHARS], "```"))
        lines.append("")

    lines.extend(("## Persisted progress (PowerContext)", ""))
    if state is None:
        lines.append("- No prior state recovered.")
    else:
        lines.append(f"- task_id: {state.task_id}")
        lines.append(f"- updated_at: {state.updated_at}")
        lines.append(f"- completed: {', '.join(state.completed) or '(none)'}")
        lines.append(f"- todo: {', '.join(state.todo) or '(none)'}")
        for name, result in sorted(state.validation.items()):
            lines.append(f"- validation.{name}: {result}")
    lines.append("")
    return "\n".join(lines)
