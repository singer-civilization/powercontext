from __future__ import annotations

import json
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import certifi


class MiniMaxError(RuntimeError):
    pass


DEFAULT_MAX_TOKENS = 16384


@dataclass(frozen=True)
class MiniMaxResult:
    model: str
    content: str
    request_id: str
    usage: dict[str, Any]


class MiniMaxClient:
    """OpenAI-compatible MiniMax chat client with no SDK lock-in."""

    def __init__(self, api_key: str, model: str, api_url: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> None:
        if not api_key:
            raise MiniMaxError("MINIMAX_API_KEY is required for a live generation")
        self.api_key = api_key
        self.model = model
        self.api_url = api_url
        self.max_tokens = max_tokens

    def generate_patch(self, context: str, objective: str) -> MiniMaxResult:
        system = (
            "You are the implementation model in an SDD workflow. Reply with one unified git diff and nothing "
            "else: no prose, no explanation, no code fences. The diff must apply to the files shown in the "
            "engineering context with their exact current content. Do not touch files outside sample_project/."
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": f"Objective:\n{objective}\n\nEngineering context:\n{context}"},
            ],
            "temperature": 1.0,
            "max_tokens": self.max_tokens,
        }
        request = Request(
            self.api_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            with urlopen(request, timeout=180, context=ssl_context) as response:
                body = json.loads(response.read().decode("utf-8"))
                request_id = response.headers.get("x-request-id", "")
        except HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise MiniMaxError(f"MiniMax returned HTTP {exc.code}: {_error_message(details)}") from exc
        except URLError as exc:
            raise MiniMaxError(f"Cannot reach MiniMax: {exc.reason}") from exc
        choice = (body.get("choices") or [{}])[0] if isinstance(body, dict) else {}
        message = choice.get("message") if isinstance(choice, dict) else None
        content = (message or {}).get("content") if isinstance(message, dict) else None
        if not content:
            finish = choice.get("finish_reason") if isinstance(choice, dict) else None
            if finish == "length":
                raise MiniMaxError(
                    f"MiniMax stopped at the {self.max_tokens}-token limit before writing the diff; "
                    "raise MINIMAX_MAX_TOKENS"
                )
            raise MiniMaxError(
                f"MiniMax rejected the request: {_error_message(json.dumps(body, ensure_ascii=False))[:400]}"
            )
        patch = _strip_code_fence(str(content))
        if not _looks_like_diff(patch):
            head = patch.strip().splitlines()[0][:160] if patch.strip() else ""
            raise MiniMaxError(f"MiniMax did not return a unified diff: {head!r}")
        return MiniMaxResult(
            model=str(body.get("model", self.model)),
            content=patch,
            request_id=request_id or str(body.get("id", "")),
            usage=dict(body.get("usage", {})),
        )


def _looks_like_diff(content: str) -> bool:
    head = content.strip().splitlines()[0] if content.strip() else ""
    return head.startswith(("diff --git", "--- a/", "--- "))


def _error_message(details: str) -> str:
    """Return the human-readable reason from a MiniMax error envelope."""
    try:
        payload = json.loads(details)
    except (TypeError, ValueError):
        return details.strip()
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        base = payload.get("base_resp")
        if isinstance(base, dict) and base.get("status_msg"):
            return f"{base.get('status_code')} {base['status_msg']}"
    return details.strip()


def _strip_code_fence(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        return "\n".join(lines[1:-1]).strip() + "\n"
    return stripped + ("" if stripped.endswith("\n") else "\n")
