"""Run the existing market bot with OpenAI and resolve the Telegram chat ID."""

import json
import os
import re
import sys
from typing import Any

import requests
from openai import OpenAI

import daily_market_bot as bot

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-5-mini"
CLIENT = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

KOREAN_SYSTEM = (
    "You are a senior global equity portfolio manager writing for Korean investors. "
    "Respond in Korean only. Keep tickers, company names, and source names in English "
    "only when needed. Never invent facts, numbers, sources, or links."
)


def openai_call(prompt: str, max_tokens: int = 4096, system: str | None = None) -> str | None:
    if not CLIENT:
        bot.logger.error("OPENAI_API_KEY is missing")
        return None
    try:
        response = CLIENT.responses.create(
            model=OPENAI_MODEL,
            instructions=system or KOREAN_SYSTEM,
            input=prompt,
            max_output_tokens=max_tokens,
        )
        return (response.output_text or "").strip() or None
    except Exception as exc:
        bot.logger.exception("OpenAI API error: %s", exc)
        return None


def resolve_telegram_chat_id() -> str | None:
    configured = (os.getenv("CHAT_ID") or "").strip()
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
                if configured and str(chat_id) != configured:
                    bot.logger.warning(
                        "TELEGRAM_CHAT_ID differs from latest bot chat; using latest chat_id"
                    )
                return str(chat_id)
    except Exception as exc:
        bot.logger.warning("Could not auto-resolve Telegram chat ID: %s", exc)
    return configured or None


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    try:
        return bot._normalize_item(item)
    except Exception:
        return dict(item)


def openai_sort_and_summarize_news(
    items: list[dict[str, Any]], top_n: int
) -> list[dict[str, Any]]:
    """Rank news and force Korean summaries with a JSON contract."""
    if not CLIENT or not items:
        return [{**normalize_item(it), "summary": it.get("title", "")} for it in items[:top_n]]

    rows = []
    for i, it in enumerate(items[:120], 1):
        rows.append(
            {
                "idx": i,
                "source": it.get("source") or "",
                "title": it.get("title") or "",
                "description": str(it.get("description") or "")[:220],
            }
        )

    prompt = (
        f"Select the top {top_n} market-moving news items for Korean equity investors. "
        "For each selected item, translate and summarize it into one natural Korean sentence. "
        "Return JSON array only. Each item must be exactly "
        '{"idx": original_number, "summary": "Korean sentence"}. '
        "The summary must be Korean, 45 to 80 Korean characters when possible. "
        "Do not copy the English title. Do not add facts, numbers, or links. "
        "Keep tickers/company/source names in English only when necessary.\n\n"
        f"NEWS_ITEMS_JSON:\n{json.dumps(rows, ensure_ascii=False)}"
    )

    raw = openai_call(prompt, max_tokens=5000, system=KOREAN_SYSTEM) or ""
    match = re.search(r"\[[\s\S]*\]", raw)
    ordered: list[dict[str, Any]] = []
    seen: set[int] = set()

    if match:
        try:
            parsed = json.loads(match.group(0))
            for entry in parsed:
                idx = int(entry.get("idx", 0))
                summary = str(entry.get("summary") or "").strip()
                if 1 <= idx <= len(items) and idx not in seen and summary:
                    seen.add(idx)
                    base = normalize_item(items[idx - 1])
                    base["summary"] = summary[:180]
                    ordered.append(base)
        except Exception as exc:
            bot.logger.warning("OpenAI news JSON parse failed: %s", exc)

    if ordered:
        return ordered[:top_n]

    bot.logger.warning("OpenAI news summary fallback used; translating titles one by one")
    fallback: list[dict[str, Any]] = []
    for it in items[:top_n]:
        title = str(it.get("title") or "").strip()
        source = str(it.get("source") or "").strip()
        prompt_one = (
            "Translate and summarize this news headline into one Korean sentence. "
            "Output only the Korean sentence. Do not add facts.\n"
            f"[{source}] {title}"
        )
        summary = openai_call(prompt_one, max_tokens=180, system=KOREAN_SYSTEM) or title
        base = normalize_item(it)
        base["summary"] = summary.strip()[:180]
        fallback.append(base)
    return fallback


def install_openai_overrides() -> None:
    bot._CLAUDE_CLIENT = CLIENT
    bot._claude_call = openai_call
    bot._claude_sort_and_summarize = openai_sort_and_summarize_news

    original_generate_summary = bot._generate_summary
    original_generate_ideas = bot._generate_ideas

    def generate_summary_with_more_room(*args: Any, **kwargs: Any) -> str:
        previous = bot._claude_call

        def roomy_call(prompt: str, max_tokens: int = 4096, system: str | None = None) -> str | None:
            return openai_call(prompt, max(max_tokens, 6500), system or KOREAN_SYSTEM)

        bot._claude_call = roomy_call
        try:
            return original_generate_summary(*args, **kwargs)
        finally:
            bot._claude_call = previous

    def generate_ideas_with_more_room(*args: Any, **kwargs: Any) -> str:
        previous = bot._claude_call

        def roomy_call(prompt: str, max_tokens: int = 4096, system: str | None = None) -> str | None:
            return openai_call(prompt, max(max_tokens, 2500), system or KOREAN_SYSTEM)

        bot._claude_call = roomy_call
        try:
            return original_generate_ideas(*args, **kwargs)
        finally:
            bot._claude_call = previous

    bot._generate_summary = generate_summary_with_more_room
    bot._generate_ideas = generate_ideas_with_more_room


def main() -> None:
    bot.CHAT_ID = resolve_telegram_chat_id()
    install_openai_overrides()
    if len(sys.argv) > 1 and sys.argv[1] == "tech":
        bot.run_tech_news_bot()
    else:
        bot.main()


if __name__ == "__main__":
    main()
