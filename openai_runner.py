"""Run the existing market bot with OpenAI and strict Telegram delivery checks."""

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
    if configured:
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{token}/getChat",
                params={"chat_id": configured},
                timeout=15,
            )
            if response.ok and response.json().get("ok") is True:
                bot.logger.info("Using configured Telegram chat ID")
                return configured
            bot.logger.warning(
                "Configured Telegram chat ID is invalid; using the latest bot chat"
            )
        except Exception as exc:
            bot.logger.warning("Could not validate configured Telegram chat ID: %s", exc)
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
                bot.logger.info("Using latest Telegram bot chat")
                return str(chat_id)
    except Exception as exc:
        bot.logger.warning("Could not auto-resolve Telegram chat ID: %s", exc)
    return None


def _telegram_post(message: str, parse_mode: str = "HTML") -> None:
    token = (os.getenv("TELEGRAM_TOKEN") or "").strip()
    chat_id = str(bot.CHAT_ID or "").strip()
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_TOKEN or CHAT_ID is missing")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    response = requests.post(url, json=payload, timeout=bot.TELEGRAM_TIMEOUT)
    if response.status_code != 200 and parse_mode:
        payload["parse_mode"] = ""
        response = requests.post(url, json=payload, timeout=bot.TELEGRAM_TIMEOUT)

    try:
        result = response.json()
    except ValueError:
        result = {}
    if response.status_code != 200 or result.get("ok") is not True:
        description = result.get("description") or response.text[:300]
        raise RuntimeError(
            f"Telegram send failed ({response.status_code}): {description}"
        )


def strict_send_telegram(message: str) -> None:
    if not message or len(message.strip()) < bot.MIN_MESSAGE_LEN:
        return
    _telegram_post(message)


def strict_send_telegram_plain(message: str) -> None:
    if not message or len(message.strip()) < bot.MIN_MESSAGE_LEN:
        return

    formatted = bot._apply_bold_headers(message)
    max_len = 4000
    chunks: list[str] = []
    current = ""
    for line in formatted.splitlines(keepends=True):
        if len(current) + len(line) > max_len and current:
            chunks.append(current.rstrip())
            current = ""
        current += line
    if current.strip():
        chunks.append(current.rstrip())

    for index, chunk in enumerate(chunks):
        text = chunk if index == 0 else "(계속)\n" + chunk
        _telegram_post(text)


def install_telegram_overrides() -> None:
    bot.send_telegram = strict_send_telegram
    bot.send_telegram_plain = strict_send_telegram_plain


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    try:
        return bot._normalize_item(item)
    except Exception:
        return dict(item)


def openai_sort_and_summarize_news(
    items: list[dict[str, Any]], top_n: int
) -> list[dict[str, Any]]:
    """Rank news and force Korean summaries with robust parse fallbacks."""
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

    def build_ordered(pairs: list[tuple[int, str]]) -> list[dict[str, Any]]:
        ordered: list[dict[str, Any]] = []
        seen: set[int] = set()
        for idx, summary in pairs:
            summary = str(summary or "").strip()
            if 1 <= idx <= len(items) and idx not in seen and summary:
                seen.add(idx)
                base = normalize_item(items[idx - 1])
                base["summary"] = summary[:180]
                ordered.append(base)
        return ordered[:top_n]

    def parse_json_pairs(raw_text: str) -> list[tuple[int, str]]:
        match = re.search(r"\[[\s\S]*\]", raw_text)
        if not match:
            return []
        parsed = json.loads(match.group(0))
        pairs: list[tuple[int, str]] = []
        for entry in parsed:
            pairs.append((int(entry.get("idx", 0)), str(entry.get("summary") or "")))
        return pairs

    def parse_line_pairs(raw_text: str) -> list[tuple[int, str]]:
        pairs: list[tuple[int, str]] = []
        for line in raw_text.splitlines():
            line = line.strip()
            if not line:
                continue
            match = re.match(
                r"^\s*(?:idx|item|news|no\.?|원문|뉴스)?\s*#?\s*(\d{1,3})\s*(?:번)?\s*(?:[|,.\-:：)]|\s+)\s*(.+?)\s*$",
                line,
                re.IGNORECASE,
            )
            if match:
                pairs.append((int(match.group(1)), match.group(2).strip()))
        return pairs

    def heuristic_rank(source_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        priority_groups = [
            (
                100,
                [
                    "fed", "fomc", "powell", "yield", "treasury", "rate", "rates",
                    "inflation", "cpi", "ppi", "jobs", "payroll", "ecb", "boj",
                    "central bank", "dollar", "dxy",
                ],
            ),
            (
                85,
                [
                    "war", "iran", "israel", "ukraine", "russia", "china", "tariff",
                    "trade", "sanction", "hormuz", "oil", "opec", "geopolitical",
                ],
            ),
            (
                70,
                [
                    "earnings", "guidance", "revenue", "profit", "forecast", "upgrade",
                    "downgrade", "merger", "acquisition", "m&a", "buyback",
                ],
            ),
            (
                60,
                [
                    "nvidia", "nvda", "ai", "semiconductor", "chip", "tsmc", "amd",
                    "broadcom", "micron", "memory", "datacenter", "data center",
                    "energy", "power", "uranium",
                ],
            ),
            (
                40,
                [
                    "gdp", "retail sales", "housing", "consumer", "manufacturing",
                    "ism", "pmi", "credit", "debt", "deficit",
                ],
            ),
        ]
        ranked = []
        for pos, item in enumerate(source_items):
            text = " ".join(
                [
                    str(item.get("source") or ""),
                    str(item.get("title") or ""),
                    str(item.get("description") or ""),
                ]
            ).lower()
            score = 0
            for weight, keywords in priority_groups:
                hits = sum(1 for keyword in keywords if keyword in text)
                if hits:
                    score += weight + min(hits, 4) * 5
            source = str(item.get("source") or "").lower()
            if any(name in source for name in ["bloomberg", "reuters", "cnbc", "marketwatch", "ft"]):
                score += 8
            ranked.append((score, -pos, item))
        ranked.sort(reverse=True, key=lambda row: (row[0], row[1]))
        return [row[2] for row in ranked]

    raw = openai_call(prompt, max_tokens=5000, system=KOREAN_SYSTEM) or ""
    try:
        ordered = build_ordered(parse_json_pairs(raw))
    except Exception as exc:
        bot.logger.warning("OpenAI news JSON parse failed: %s", exc)
        ordered = []

    if ordered:
        bot.logger.info("OpenAI news JSON ranking parsed: %d items", len(ordered))
        return ordered[:top_n]

    compact_lines = []
    for i, it in enumerate(items[:120], 1):
        compact_lines.append(
            f"{i}. [{it.get('source') or ''}] {it.get('title') or ''}"
        )
    pipe_prompt = (
        f"Rank the top {top_n} market-moving news items for Korean equity investors. "
        "Output exactly one item per line in this format: original_number|Korean summary. "
        "The Korean summary must be one natural Korean sentence. "
        "Do not output JSON, bullets, code fences, headings, explanations, or English-only titles. "
        "Do not add facts, numbers, or links.\n\n"
        "Priority order: Fed/rates/inflation/central banks first; geopolitics/trade/tariffs/sanctions second; "
        "major earnings/M&A/guidance third; semiconductors/AI/energy fourth; other macro/industry last.\n\n"
        "NEWS LIST:\n" + "\n".join(compact_lines)
    )
    raw_pipe = openai_call(pipe_prompt, max_tokens=3500, system=KOREAN_SYSTEM) or ""
    ordered = build_ordered(parse_line_pairs(raw_pipe))
    if ordered:
        bot.logger.info("OpenAI news pipe ranking parsed: %d items", len(ordered))
        return ordered[:top_n]

    bot.logger.warning("OpenAI news ranking parse failed; using heuristic ranking + one-by-one translation")
    fallback: list[dict[str, Any]] = []
    for it in heuristic_rank(items)[:top_n]:
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
    install_telegram_overrides()
    install_openai_overrides()
    if len(sys.argv) > 1 and sys.argv[1] == "tech":
        bot.run_tech_news_bot()
    else:
        bot.main()


if __name__ == "__main__":
    main()
