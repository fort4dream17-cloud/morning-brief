"""Run the existing market bot with OpenAI and resolve the Telegram chat ID."""

import os
import sys
from typing import Any

import requests
from openai import OpenAI

import daily_market_bot as bot

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-5-mini"
CLIENT = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


def openai_call(prompt: str, max_tokens: int = 4096, system: str | None = None) -> str | None:
    if not CLIENT:
        bot.logger.error("OPENAI_API_KEY is missing")
        return None
    try:
        response = CLIENT.responses.create(
            model=OPENAI_MODEL,
            instructions=system or (
                "Respond precisely. Never invent facts, numbers, sources, or links. "
                "Write the requested global market briefing in Korean."
            ),
            input=prompt,
            max_output_tokens=max_tokens,
        )
        return (response.output_text or "").strip() or None
    except Exception as exc:
        bot.logger.exception("OpenAI API error: %s", exc)
        return None


def resolve_telegram_chat_id() -> str | None:
    configured = (os.getenv("CHAT_ID") or "").strip()
    if configured.lstrip("-").isdigit():
        return configured
    token = (os.getenv("TELEGRAM_TOKEN") or "").strip()
    if not token:
        return configured or None
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates", timeout=15
        )
        response.raise_for_status()
        updates: list[dict[str, Any]] = response.json().get("result", [])
        for update in reversed(updates):
            event = update.get("message") or update.get("channel_post")
            chat_id = (event or {}).get("chat", {}).get("id")
            if chat_id is not None:
                return str(chat_id)
    except Exception as exc:
        bot.logger.warning("Could not auto-resolve Telegram chat ID: %s", exc)
    return configured or None


def main() -> None:
    bot.CHAT_ID = resolve_telegram_chat_id()
    bot._CLAUDE_CLIENT = CLIENT
    bot._claude_call = openai_call
    if len(sys.argv) > 1 and sys.argv[1] == "tech":
        bot.run_tech_news_bot()
    else:
        bot.main()


if __name__ == "__main__":
    main()
