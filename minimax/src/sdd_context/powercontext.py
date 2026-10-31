from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import TaskState


class PowerContextError(RuntimeError):
    pass


class PowerContextClient:
    """Minimal client for PowerContext's public Memory API."""

    def __init__(self, base_url: str, scope_id: str, api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.scope_id = scope_id
        self.api_key = api_key

    def save_state(self, state: TaskState) -> dict[str, Any]:
        refreshed = replace(state, updated_at=datetime.now(UTC).isoformat())
        text = f"sdd-state:{state.task_id}\n{json.dumps(refreshed.to_dict(), ensure_ascii=False, sort_keys=True)}"
        return self._post(
            "/v1/memory/remember",
            {
                "scope_id": self.scope_id,
                "kind": "sdd_task_state",
                "text": text,
                "reason": "Persist MiniMax SDD task progress for restart recovery",
            },
        )

    def load_state(self, task_id: str) -> TaskState | None:
        response = self._post(
            "/v1/memory/search",
            {"scope_id": self.scope_id, "query": f"sdd-state:{task_id}", "limit": 20, "mode": "auto"},
        )
        candidates: list[TaskState] = []
        prefix = f"sdd-state:{task_id}\n"
        for hit in response.get("hits", []):
            text = str(hit.get("text", ""))
            if not text.startswith(prefix):
                continue
            try:
                candidates.append(TaskState.from_dict(json.loads(text[len(prefix) :])))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return max(candidates, key=lambda item: item.updated_at) if candidates else None

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise PowerContextError(f"PowerContext returned HTTP {exc.code}: {details}") from exc
        except URLError as exc:
            raise PowerContextError(f"Cannot reach PowerContext at {self.base_url}: {exc.reason}") from exc
        return json.loads(body) if body else {}
