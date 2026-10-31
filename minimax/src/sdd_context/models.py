from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class Document:
    kind: str
    path: str
    content: str
    symbol: str = ""
    checksum: str = ""
    version: int = 1


@dataclass(frozen=True)
class SearchHit:
    kind: str
    path: str
    symbol: str
    checksum: str
    version: int
    score: float
    excerpt: str
    line: int
    content: str = ""

    @property
    def citation(self) -> str:
        location = f"{self.path}:{self.line}"
        return f"{location}#{self.checksum[:12]}"


@dataclass
class TaskState:
    task_id: str
    objective: str
    completed: list[str] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)
    validation: dict[str, str] = field(default_factory=dict)
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TaskState:
        return cls(
            task_id=str(value["task_id"]),
            objective=str(value["objective"]),
            completed=[str(item) for item in value.get("completed", [])],
            todo=[str(item) for item in value.get("todo", [])],
            validation={str(key): str(item) for key, item in value.get("validation", {}).items()},
            updated_at=str(value.get("updated_at") or datetime.now(UTC).isoformat()),
        )
