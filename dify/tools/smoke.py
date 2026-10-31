"""等价冒烟：按 DSL 中完全相同的三个请求，验证「记忆 → 生成 → 写回」回路。

用法：MINIMAX_API_KEY=xxx python3 tools/smoke.py "老板，还记得我上次点什么吗？"
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

PC = os.environ.get("POWERCONTEXT_URL", "http://127.0.0.1:8000")
SCOPE = os.environ.get("POWERCONTEXT_SCOPE_ID", "scp_7f4cqa0evmrpw7hrqa0a9mh5t2")
MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M3.1-Flash")
URL = os.environ.get("MINIMAX_API_URL", "https://api.minimaxi.com/v1/chat/completions")
KEY = os.environ.get("MINIMAX_API_KEY", "")
SYSTEM = (
    "你是「记忆酒馆」的老板，开了四十年店的老酒保。说话简短、有点冷幽默，每次回答不超过 80 个汉字。\n"
    "下面是你记得的、和这位客人有关的旧事（可能为空）：\n{mem}\n"
    "规则：记忆里出现过这位客人的偏好、点过的酒或心事，就自然带进对话，不要生硬复述；"
    "记忆为空就当第一次见，问他一句来历；不要解释你是 AI。"
)


def post(url: str, payload: dict, headers: dict | None = None) -> dict:
    """用 curl 发请求：本机 macOS 的 Python 信任链会被代理拦截，curl 更稳。"""
    command = ["curl", "-sS", "-X", "POST", url, "-H", "Content-Type: application/json"]
    for name, value in (headers or {}).items():
        command += ["-H", f"{name}: {value}"]
    command += ["-d", json.dumps(payload, ensure_ascii=False)]
    output = subprocess.run(command, check=True, capture_output=True, text=True, timeout=180).stdout
    return json.loads(output)


def main() -> int:
    ask = sys.argv[1] if len(sys.argv) > 1 else "老板，还记得我上次点什么吗？"
    if not KEY:
        print("需要 MINIMAX_API_KEY", file=sys.stderr)
        return 2

    recall = post(f"{PC}/v1/memory/search", {"scope_id": SCOPE, "query": ask, "limit": 5, "mode": "auto"})
    hits = recall.get("hits", [])
    print(f"[1] 回忆命中 {len(hits)} 条")
    memory = "\n".join(f"- {str(h.get('text', '')).replace(chr(10), ' ')[:160]}" for h in hits) or "（没有任何记录）"

    reply = post(
        URL,
        {"model": MODEL, "messages": [
            {"role": "system", "content": SYSTEM.format(mem=memory)},
            {"role": "user", "content": ask},
        ], "temperature": 0.8, "max_tokens": 512},
        {"Authorization": f"Bearer {KEY}"},
    )
    answer = reply["choices"][0]["message"]["content"].strip()
    print(f"[2] 客人：{ask}")
    print(f"    老板：{answer}")

    post(f"{PC}/v1/memory/remember", {
        "scope_id": SCOPE,
        "kind": "tavern_turn",
        "text": f"客人说：{ask} / 老板答：{answer}",
        "reason": "记住酒馆里这一轮对话，供下次进店复用",
    })
    print("[3] 已写回 PowerContext")

    again = post(f"{PC}/v1/memory/search", {"scope_id": SCOPE, "query": ask, "limit": 5, "mode": "auto"})
    print(f"[4] 再查一次，命中 {len(again.get('hits', []))} 条（应大于等于上一次）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
