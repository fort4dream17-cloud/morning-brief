"""Resolve and persist the Telegram destination without logging its ID."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


CACHE_FILE = Path(".telegram-chat-id")
TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
CONFIGURED_CHAT_ID = (os.getenv("CHAT_ID") or "").strip()


def api(method: str, params: dict[str, str] | None = None) -> dict:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is missing")
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    if params:
        url += "?" + urlencode(params)
    request = Request(url, headers={"User-Agent": "morning-brief"})
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def valid_chat(chat_id: str) -> bool:
    try:
        return api("getChat", {"chat_id": chat_id}).get("ok") is True
    except Exception:
        return False


def resolve() -> str:
    candidates = [CONFIGURED_CHAT_ID]
    if CACHE_FILE.exists():
        candidates.append(CACHE_FILE.read_text(encoding="utf-8").strip())

    for candidate in candidates:
        if candidate and valid_chat(candidate):
            return candidate

    updates = api("getUpdates").get("result", [])
    for update in reversed(updates):
        event = update.get("message") or update.get("channel_post")
        chat_id = (event or {}).get("chat", {}).get("id")
        if chat_id is not None:
            return str(chat_id)
    raise RuntimeError(
        "Telegram chat not found. Send /start to @Teletelerobot_bot and retry."
    )


def main() -> None:
    chat_id = resolve()
    CACHE_FILE.write_text(chat_id, encoding="utf-8")
    github_env = os.getenv("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as output:
            output.write(f"CHAT_ID={chat_id}\n")
    print("Telegram destination resolved and cached.")


if __name__ == "__main__":
    main()
