from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import textwrap
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import requests


KST = ZoneInfo("Asia/Seoul")
UTC = ZoneInfo("UTC")
NOTION_VERSION = "2022-06-28"


MARKET_SYMBOLS = [
    ("Dow", "^DJI", "index", None),
    ("S&P 500", "^GSPC", "index", None),
    ("Nasdaq", "^IXIC", "index", None),
    ("Russell 2000", "^RUT", "index", None),
    ("SOX", "^SOX", "index", None),
    ("VIX", "^VIX", "vol", None),
    ("US 2Y Yield", "^IRX", "rates", "yield_x10"),
    ("US 5Y Yield", "^FVX", "rates", "yield_x10"),
    ("US 10Y Yield", "^TNX", "rates", "yield_x10"),
    ("US 30Y Yield", "^TYX", "rates", "yield_x10"),
    ("DXY", "DX-Y.NYB", "fx", None),
    ("USD/JPY", "JPY=X", "fx", None),
    ("EUR/USD", "EURUSD=X", "fx", None),
    ("USD/KRW", "KRW=X", "fx", None),
    ("WTI", "CL=F", "commodity", None),
    ("Brent", "BZ=F", "commodity", None),
    ("Gold", "GC=F", "commodity", None),
    ("Bitcoin", "BTC-USD", "crypto", None),
]


EQUITY_SYMBOLS = [
    ("NVIDIA", "NVDA"),
    ("Microsoft", "MSFT"),
    ("Apple", "AAPL"),
    ("Alphabet", "GOOGL"),
    ("Amazon", "AMZN"),
    ("Meta", "META"),
    ("Tesla", "TSLA"),
    ("Broadcom", "AVGO"),
    ("Micron", "MU"),
    ("TSMC ADR", "TSM"),
]


RSS_FEEDS = [
    ("CNBC Markets", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("CNBC Economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("MarketWatch Top Stories", "https://feeds.marketwatch.com/marketwatch/topstories/"),
]


@dataclass
class Quote:
    label: str
    symbol: str
    group: str
    close: float | None
    previous_close: float | None
    change: float | None
    pct_change: float | None
    as_of: str
    source: str


def request_json(url: str, *, params: dict[str, str] | None = None, timeout: int = 20) -> dict[str, Any]:
    headers = {"User-Agent": "morning-brief/1.0"}
    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def yahoo_quote(label: str, symbol: str, group: str, transform: str | None = None) -> Quote:
    encoded = urllib.parse.quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}"
    data = request_json(url, params={"range": "10d", "interval": "1d"})
    result = data["chart"]["result"][0]
    timestamps = result.get("timestamp") or []
    closes = result["indicators"]["quote"][0].get("close") or []
    pairs = [(ts, close) for ts, close in zip(timestamps, closes) if close is not None]
    if len(pairs) < 2:
        raise ValueError(f"not enough close data for {symbol}")

    prev_ts, prev_close = pairs[-2]
    last_ts, close = pairs[-1]

    if transform == "yield_x10":
        close = close / 10
        prev_close = prev_close / 10

    change = close - prev_close
    pct_change = (change / prev_close * 100) if prev_close else None
    as_of = dt.datetime.fromtimestamp(last_ts, UTC).astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
    return Quote(label, symbol, group, close, prev_close, change, pct_change, as_of, "Yahoo Finance")


def collect_quotes() -> tuple[list[Quote], list[str]]:
    quotes: list[Quote] = []
    errors: list[str] = []
    for label, symbol, group, transform in MARKET_SYMBOLS:
        try:
            quotes.append(yahoo_quote(label, symbol, group, transform))
        except Exception as exc:
            errors.append(f"{label}({symbol}): {exc}")
    for label, symbol in EQUITY_SYMBOLS:
        try:
            quotes.append(yahoo_quote(label, symbol, "equity", None))
        except Exception as exc:
            errors.append(f"{label}({symbol}): {exc}")
    return quotes, errors


def fetch_headlines(limit: int = 16) -> list[dict[str, str]]:
    headlines: list[dict[str, str]] = []
    seen: set[str] = set()
    headers = {"User-Agent": "morning-brief/1.0"}
    for source, url in RSS_FEEDS:
        try:
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            for item in root.findall(".//item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub_date = (item.findtext("pubDate") or "").strip()
                key = title.lower()
                if not title or key in seen:
                    continue
                seen.add(key)
                headlines.append({"source": source, "title": title, "link": link, "published": pub_date})
                if len(headlines) >= limit:
                    return headlines
        except Exception as exc:
            headlines.append(
                {
                    "source": source,
                    "title": f"[수집 실패] {source}: {type(exc).__name__}",
                    "link": url,
                    "published": "",
                }
            )
    return headlines[:limit]


def fmt_num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{value:,.{digits}f}"


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.2f}%"


def fmt_bp(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:+.1f}bp"


def quote_row(q: Quote) -> str:
    if q.group == "rates":
        return f"| {q.label} | {fmt_num(q.close, 3)}% | {fmt_bp(q.change)} | {q.as_of} | {q.source} |"
    if q.label in {"VIX", "DXY", "USD/JPY", "EUR/USD", "USD/KRW"}:
        return f"| {q.label} | {fmt_num(q.close, 3)} | {fmt_num(q.change, 3)} / {fmt_pct(q.pct_change)} | {q.as_of} | {q.source} |"
    if q.group in {"commodity", "crypto", "equity", "index"}:
        return f"| {q.label} | {fmt_num(q.close, 2)} | {fmt_num(q.change, 2)} / {fmt_pct(q.pct_change)} | {q.as_of} | {q.source} |"
    return f"| {q.label} | {fmt_num(q.close, 2)} | {fmt_num(q.change, 2)} | {q.as_of} | {q.source} |"


def find_quote(quotes: list[Quote], label: str) -> Quote | None:
    return next((q for q in quotes if q.label == label), None)


def infer_regime(quotes: list[Quote]) -> dict[str, Any]:
    spx = find_quote(quotes, "S&P 500")
    ndx = find_quote(quotes, "Nasdaq")
    rut = find_quote(quotes, "Russell 2000")
    vix = find_quote(quotes, "VIX")
    ten = find_quote(quotes, "US 10Y Yield")
    dxy = find_quote(quotes, "DXY")
    wti = find_quote(quotes, "WTI")

    score = 0
    if spx and spx.pct_change is not None:
        score += 2 if spx.pct_change > 0.4 else -2 if spx.pct_change < -0.4 else 0
    if ndx and ndx.pct_change is not None:
        score += 1 if ndx.pct_change > 0.5 else -1 if ndx.pct_change < -0.5 else 0
    if rut and rut.pct_change is not None:
        score += 1 if rut.pct_change > 0.5 else -1 if rut.pct_change < -0.5 else 0
    if vix and vix.pct_change is not None:
        score += 1 if vix.pct_change < -3 else -1 if vix.pct_change > 3 else 0
    if ten and ten.change is not None:
        score += 1 if ten.change < -0.03 else -1 if ten.change > 0.03 else 0
    if dxy and dxy.pct_change is not None:
        score += 1 if dxy.pct_change < -0.25 else -1 if dxy.pct_change > 0.25 else 0

    regime = "Risk-on" if score >= 2 else "Risk-off" if score <= -2 else "Mixed"
    rates_signal = "Bullish" if ten and ten.change is not None and ten.change < -0.03 else "Bearish" if ten and ten.change is not None and ten.change > 0.03 else "Neutral"
    fx_signal = "KRW Positive" if dxy and dxy.pct_change is not None and dxy.pct_change < -0.2 else "KRW Negative" if dxy and dxy.pct_change is not None and dxy.pct_change > 0.2 else "Neutral"
    commodity_signal = "Inflationary" if wti and wti.pct_change is not None and wti.pct_change > 1 else "Disinflationary" if wti and wti.pct_change is not None and wti.pct_change < -1 else "Neutral"
    korea_bias = "Positive" if regime == "Risk-on" and fx_signal != "KRW Negative" else "Negative" if regime == "Risk-off" and fx_signal == "KRW Negative" else "Mixed"
    action = ["Add"] if regime == "Risk-on" else ["Hedge", "Trim"] if regime == "Risk-off" else ["Hold", "Wait"]
    conviction = "High" if abs(score) >= 4 else "Medium" if abs(score) >= 2 else "Low"

    primary_driver: list[str] = []
    if ten and ten.change is not None and abs(ten.change) >= 0.03:
        primary_driver.append("Rates")
    if dxy and dxy.pct_change is not None and abs(dxy.pct_change) >= 0.2:
        primary_driver.append("FX")
    if wti and wti.pct_change is not None and abs(wti.pct_change) >= 1:
        primary_driver.append("Oil")
    if not primary_driver:
        primary_driver.append("Positioning")

    return {
        "regime": regime,
        "rates_signal": rates_signal,
        "fx_signal": fx_signal,
        "commodity_signal": commodity_signal,
        "korea_bias": korea_bias,
        "action": action,
        "conviction": conviction,
        "primary_driver": primary_driver,
        "score": score,
    }


def markdown_table(quotes: list[Quote], groups: list[str]) -> str:
    rows = ["| 항목 | 수치 | 변화 | 기준시각 | 출처 |", "| --- | ---: | ---: | --- | --- |"]
    for q in quotes:
        if q.group in groups:
            rows.append(quote_row(q))
    return "\n".join(rows)


def deterministic_brief(today: dt.date, us_close_date: dt.date, quotes: list[Quote], headlines: list[dict[str, str]], regime: dict[str, Any], errors: list[str]) -> dict[str, str]:
    spx = find_quote(quotes, "S&P 500")
    ten = find_quote(quotes, "US 10Y Yield")
    dxy = find_quote(quotes, "DXY")
    wti = find_quote(quotes, "WTI")
    sox = find_quote(quotes, "SOX")

    conclusion = (
        f"{regime['regime']} 성격의 장세. "
        f"S&P 500 {fmt_pct(spx.pct_change if spx else None)}, "
        f"10Y {fmt_bp(ten.change if ten else None)}, "
        f"DXY {fmt_pct(dxy.pct_change if dxy else None)}, "
        f"WTI {fmt_pct(wti.pct_change if wti else None)}가 핵심 변수입니다."
    )

    top_lines = []
    for idx, item in enumerate(headlines[:5], 1):
        top_lines.append(f"{idx}. {item['title']} ({item['source']})")

    semis_note = "SOX " + (fmt_pct(sox.pct_change) if sox else "N/A")
    body = f"""# PM Snapshot
- **결론:** {conclusion}
- **Regime:** {regime['regime']} / **Korea Bias:** {regime['korea_bias']} / **Action:** {', '.join(regime['action'])}
- **Primary Driver:** {', '.join(regime['primary_driver'])}
- **Data cutoff:** {dt.datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}

# Cross-Asset Dashboard
## Index / Vol / Rates / FX / Commodity
{markdown_table(quotes, ['index', 'vol', 'rates', 'fx', 'commodity', 'crypto'])}

## Key Equities
{markdown_table(quotes, ['equity'])}

# Market Interpretation
- 금리 신호: {regime['rates_signal']}. 10년물 금리 변화가 성장주 멀티플과 한국 금리 민감주에 미치는 영향을 우선 확인합니다.
- FX 신호: {regime['fx_signal']}. 달러 방향은 외국인 수급과 원화 민감 업종에 직접 연결됩니다.
- 원자재 신호: {regime['commodity_signal']}. 유가 급등락은 인플레이션 기대와 에너지/운송/화학 마진을 동시에 건드립니다.
- 반도체 체크: {semis_note}. Nasdaq 대비 SOX 상대강도가 한국 반도체 오프닝 톤의 첫 번째 단서입니다.

# Top 5 Headlines
{chr(10).join(top_lines) if top_lines else '- 공개 RSS 헤드라인 수집 실패'}

# Korea Read-through
- 반도체: SOX, NVIDIA, Micron, Broadcom의 당일 등락률을 한국 HBM/메모리/후공정 체인에 연결해서 봅니다.
- 인터넷/성장주: 미국 10년물과 Nasdaq의 조합이 멀티플 민감도를 좌우합니다.
- 조선/방산/에너지: WTI/Brent와 지정학 헤드라인이 강하면 방어적 수급이 붙을 수 있습니다.
- 환율 민감주: DXY와 USD/KRW 방향이 외국인 선물/현물 수급의 첫 신호입니다.

# Portfolio Implication
- 기본 액션: {', '.join(regime['action'])}
- 확신도: {regime['conviction']}
- 리스크 예산은 지수 방향보다 금리, 달러, SOX 상대강도, 유가의 조합으로 배분합니다.

# Questions for Morning Meeting
1. 금리 변화가 성장주 멀티플 부담인지, 경기 기대 개선인지 구분되는가?
2. SOX와 Mag 7 주도력이 한국 반도체/AI 밸류체인으로 이어질 수 있는가?
3. 달러와 유가 조합이 오늘 한국장 업종 로테이션을 어느 방향으로 밀 가능성이 큰가?

# Source Map
- Market data: Yahoo Finance chart API
- Headlines: CNBC, Yahoo Finance, MarketWatch RSS
- Unconfirmed / missing: {', '.join(errors[:8]) if errors else '없음'}
"""

    return {
        "title": f"{today:%Y-%m-%d} 모닝 시황",
        "conclusion": conclusion,
        "top_headlines": "\n".join(top_lines[:5]),
        "market_driver_news": top_lines[0] if top_lines else "",
        "source_map": "Market data: Yahoo Finance chart API; Headlines: CNBC/Yahoo Finance/MarketWatch RSS",
        "unconfirmed": "\n".join(errors),
        "body": body,
    }


def openai_enhance(base: dict[str, str], quotes: list[Quote], headlines: list[dict[str, str]], regime: dict[str, Any]) -> dict[str, str]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return base

    model = os.environ.get("OPENAI_MODEL") or "gpt-4.1-mini"
    prompt = {
        "base_brief": base["body"],
        "regime": regime,
        "headlines": headlines[:10],
        "instruction": (
            "Rewrite the Korean morning market brief for a global equity portfolio manager. "
            "Keep exact numbers from the base brief, do not invent data, preserve source caveats, "
            "and make the format concise: PM Snapshot, Dashboard Notes, Top News, Korea Read-through, Portfolio Implication, Questions."
        ),
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You write precise Korean market briefs. Never fabricate prices, dates, sources, or links."},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        "temperature": 0.2,
    }
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        if content:
            base["body"] = content + "\n\n# Source Map\n" + base["source_map"]
    except Exception as exc:
        base["body"] += f"\n\n# OpenAI Enhancement\n- OpenAI 요약 실패: {type(exc).__name__}: {exc}\n"
    return base


def notion_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def notion_request(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    for attempt in range(3):
        response = requests.request(method, url, headers=notion_headers(token), json=payload, timeout=30)
        if response.status_code == 429:
            time.sleep(2 + attempt)
            continue
        response.raise_for_status()
        return response.json()
    response.raise_for_status()
    return response.json()


def rich_text(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": text[:2000]}}]


def notion_select(name: str | None) -> dict[str, str] | None:
    return {"name": name} if name else None


def notion_multi(names: list[str]) -> list[dict[str, str]]:
    return [{"name": name} for name in names]


def page_properties(brief: dict[str, str], today: dt.date, us_close_date: dt.date, regime: dict[str, Any]) -> dict[str, Any]:
    return {
        "Brief": {"title": rich_text(brief["title"])},
        "Date": {"date": {"start": today.isoformat()}},
        "US Close Date": {"date": {"start": us_close_date.isoformat()}},
        "Data Cutoff KST": {"date": {"start": dt.datetime.now(KST).isoformat()}},
        "Regime": {"select": notion_select(regime["regime"])},
        "Primary Driver": {"multi_select": notion_multi(regime["primary_driver"])},
        "Rates Signal": {"select": notion_select(regime["rates_signal"])},
        "FX Signal": {"select": notion_select(regime["fx_signal"])},
        "Commodity Signal": {"select": notion_select(regime["commodity_signal"])},
        "Korea Bias": {"select": notion_select(regime["korea_bias"])},
        "Action": {"multi_select": notion_multi(regime["action"])},
        "Conviction": {"select": notion_select(regime["conviction"])},
        "News Urgency": {"select": notion_select("Important")},
        "News Basis": {"select": notion_select("Market Price Driver")},
        "Bloomberg Check": {"select": notion_select("Unavailable")},
        "Source Confidence": {"select": notion_select("Single source")},
        "Sources Checked": {"multi_select": notion_multi(["Yahoo Finance", "CNBC"])},
        "News Tags": {"multi_select": notion_multi(["Macro"])},
        "One-line Conclusion": {"rich_text": rich_text(brief["conclusion"])},
        "Top 5 Headlines": {"rich_text": rich_text(brief["top_headlines"])},
        "Market Driver News": {"rich_text": rich_text(brief["market_driver_news"])},
        "Source Map": {"rich_text": rich_text(brief["source_map"])},
        "Unconfirmed Items": {"rich_text": rich_text(brief["unconfirmed"] or "없음")},
        "Telegram Sent": {"checkbox": False},
        "Status": {"status": {"name": "완료"}},
    }


def markdown_to_blocks(markdown: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    lines = markdown.splitlines()
    buffer: list[str] = []

    def flush_paragraph() -> None:
        nonlocal buffer
        if not buffer:
            return
        text = "\n".join(buffer).strip()
        buffer = []
        if not text:
            return
        for chunk in chunk_text(text, 1800):
            blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text(chunk)}})

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            flush_paragraph()
            blocks.append({"object": "block", "type": "heading_1", "heading_1": {"rich_text": rich_text(stripped[2:])}})
        elif stripped.startswith("## "):
            flush_paragraph()
            blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": rich_text(stripped[3:])}})
        elif stripped.startswith("### "):
            flush_paragraph()
            blocks.append({"object": "block", "type": "heading_3", "heading_3": {"rich_text": rich_text(stripped[4:])}})
        elif stripped.startswith("- "):
            flush_paragraph()
            blocks.append({"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rich_text(stripped[2:])}})
        elif stripped and all(part.strip().startswith("|") or part.strip().startswith("---") for part in [stripped]):
            buffer.append(line)
        elif not stripped:
            flush_paragraph()
        else:
            buffer.append(line)
    flush_paragraph()
    return blocks[:90]


def chunk_text(text: str, size: int) -> list[str]:
    chunks = []
    remaining = text
    while len(remaining) > size:
        split_at = remaining.rfind("\n", 0, size)
        if split_at < size // 2:
            split_at = size
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def telegram_chunks(text: str, size: int = 3600) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in text.split("\n"):
        addition = len(paragraph) + 1
        if current and current_len + addition > size:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(paragraph)
        current_len += addition
    if current:
        chunks.append("\n".join(current))
    return chunks


def send_telegram(brief: dict[str, str], notion_url: str | None = None) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")
        return

    header = brief["title"]
    if notion_url:
        header += f"\nNotion: {notion_url}"

    message = f"{header}\n\n{brief['body']}"
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in telegram_chunks(message):
        response = requests.post(
            api_url,
            data={
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": "true",
            },
            timeout=30,
        )
        response.raise_for_status()
    print("Telegram sent.")


def find_existing_page(token: str, database_id: str, today: dt.date) -> str | None:
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    payload = {"filter": {"property": "Date", "date": {"equals": today.isoformat()}}, "page_size": 1}
    data = notion_request("POST", url, token, payload)
    results = data.get("results", [])
    return results[0]["id"] if results else None


def archive_page(token: str, page_id: str) -> None:
    notion_request("PATCH", f"https://api.notion.com/v1/pages/{page_id}", token, {"archived": True})


def create_notion_page(token: str, database_id: str, properties: dict[str, Any], body: str) -> dict[str, Any]:
    payload = {
        "parent": {"database_id": database_id},
        "properties": properties,
        "children": markdown_to_blocks(body),
    }
    return notion_request("POST", "https://api.notion.com/v1/pages", token, payload)


def run(dry_run: bool = False) -> None:
    now = dt.datetime.now(KST)
    today = now.date()
    us_close_date = today - dt.timedelta(days=1)

    quotes, errors = collect_quotes()
    headlines = fetch_headlines()
    regime = infer_regime(quotes)
    brief = deterministic_brief(today, us_close_date, quotes, headlines, regime, errors)
    brief = openai_enhance(brief, quotes, headlines, regime)

    output_dir = "out"
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"morning_brief_{today.isoformat()}.md")
    with open(output_path, "w", encoding="utf-8") as file:
        file.write(brief["body"])

    if dry_run:
        print(brief["body"])
        print(f"\nSaved: {output_path}")
        return

    token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DATABASE_ID") or "b9307fc89d3e438986c7c0341cc1984d"
    if not token:
        raise SystemExit("Missing NOTION_TOKEN")

    existing = find_existing_page(token, database_id, today)
    if existing:
        archive_page(token, existing)

    created = create_notion_page(token, database_id, page_properties(brief, today, us_close_date, regime), brief["body"])
    notion_url = created.get("url")
    print(f"Created Notion page: {notion_url}")
    send_telegram(brief, notion_url)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
