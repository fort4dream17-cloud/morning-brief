"""Send a Korean weekly global market review to Telegram."""

from __future__ import annotations

import html
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()

KST = ZoneInfo("Asia/Seoul")
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
CONFIGURED_CHAT_ID = (os.getenv("CHAT_ID") or "").strip()
FRED_API_KEY = (os.getenv("FRED_API_KEY") or "").strip()
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-5-mini").strip()

MARKETS = {
    "Dow Jones": "^DJI",
    "S&P 500": "^GSPC",
    "Nasdaq": "^IXIC",
    "Russell 2000": "^RUT",
    "SOX": "^SOX",
    "미 10년물": "^TNX",
    "Dollar Index": "DX-Y.NYB",
    "WTI": "CL=F",
    "Gold": "GC=F",
    "Bitcoin": "BTC-USD",
}


def require_environment() -> None:
    missing = [
        name
        for name, value in {
            "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
            "OPENAI_API_KEY": OPENAI_API_KEY,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"필수 GitHub Secret 없음: {', '.join(missing)}")


def resolve_chat_id() -> str:
    if CONFIGURED_CHAT_ID:
        response = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChat",
            params={"chat_id": CONFIGURED_CHAT_ID},
            timeout=15,
        )
        if response.ok and response.json().get("ok") is True:
            return CONFIGURED_CHAT_ID

    response = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
        timeout=15,
    )
    response.raise_for_status()
    for update in reversed(response.json().get("result", [])):
        event = update.get("message") or update.get("channel_post")
        chat_id = (event or {}).get("chat", {}).get("id")
        if chat_id is not None:
            print("Configured CHAT_ID is invalid; using the latest bot chat.")
            return str(chat_id)
    raise RuntimeError(
        "텔레그램 채팅을 찾지 못했습니다. @Teletelerobot_bot에 메시지를 먼저 보내주세요."
    )


def send_telegram(chat_id: str, text: str) -> None:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > 3900 and current:
            chunks.append(current.rstrip())
            current = ""
        current += line
    if current.strip():
        chunks.append(current.rstrip())

    for index, chunk in enumerate(chunks):
        message = chunk if index == 0 else "(계속)\n" + chunk
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        try:
            result = response.json()
        except ValueError:
            result = {}
        if not response.ok or result.get("ok") is not True:
            detail = result.get("description") or response.text[:300]
            raise RuntimeError(f"Telegram 전송 실패: {detail}")


def market_snapshot() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, ticker in MARKETS.items():
        try:
            history = yf.Ticker(ticker).history(
                period="1mo",
                interval="1d",
                auto_adjust=False,
            )
            closes = history["Close"].dropna()
            if len(closes) < 2:
                continue
            start_index = max(0, len(closes) - 6)
            start = float(closes.iloc[start_index])
            latest = float(closes.iloc[-1])
            rows.append(
                {
                    "name": name,
                    "value": latest,
                    "weekly_change": (latest / start - 1) * 100,
                }
            )
        except Exception as exc:
            print(f"Market data warning ({name}): {exc}")
    if not rows:
        raise RuntimeError("주간 시장 데이터를 수집하지 못했습니다.")
    return rows


def fred_latest(series_id: str, limit: int = 2) -> list[float]:
    if not FRED_API_KEY:
        return []
    response = requests.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params={
            "series_id": series_id,
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "sort_order": "desc",
            "limit": limit,
        },
        timeout=20,
    )
    response.raise_for_status()
    values = []
    for item in response.json().get("observations", []):
        value = item.get("value")
        if value not in (None, "."):
            values.append(float(value))
    return values


def liquidity_snapshot() -> dict[str, Any]:
    try:
        walcl = fred_latest("WALCL")
        tga = fred_latest("WTREGEN")
        rrp = fred_latest("RRPONTSYD")
        if not walcl or not tga or not rrp:
            return {}

        current = walcl[0] - tga[0] - rrp[0] * 1000
        previous = None
        if len(walcl) > 1 and len(tga) > 1 and len(rrp) > 1:
            previous = walcl[1] - tga[1] - rrp[1] * 1000
        return {
            "fed_assets_bn": walcl[0] / 1000,
            "tga_bn": tga[0] / 1000,
            "rrp_bn": rrp[0],
            "net_liquidity_bn": current / 1000,
            "net_weekly_change_bn": (
                (current - previous) / 1000 if previous is not None else None
            ),
        }
    except Exception as exc:
        print(f"FRED warning: {exc}")
        return {}


def format_market_lines(rows: list[dict[str, Any]]) -> str:
    lines = []
    for row in rows:
        value = row["value"]
        if row["name"] == "미 10년물":
            value_text = f"{value:.3f}%"
        elif row["name"] in {"WTI", "Gold", "Bitcoin"}:
            value_text = f"${value:,.2f}"
        else:
            value_text = f"{value:,.2f}"
        lines.append(
            f"• {row['name']}: {value_text} ({row['weekly_change']:+.2f}%)"
        )
    return "\n".join(lines)


def format_liquidity(liquidity: dict[str, Any]) -> str:
    if not liquidity:
        return "• FRED 유동성 데이터 수집 실패"
    change = liquidity.get("net_weekly_change_bn")
    change_text = "N/A" if change is None else f"{change:+,.1f}B"
    return "\n".join(
        [
            f"• Fed 총자산: ${liquidity['fed_assets_bn']:,.1f}B",
            f"• TGA: ${liquidity['tga_bn']:,.1f}B",
            f"• RRP: ${liquidity['rrp_bn']:,.1f}B",
            f"• 추정 순유동성: ${liquidity['net_liquidity_bn']:,.1f}B",
            f"• 순유동성 주간 변화: {change_text}",
        ]
    )


def generate_analysis(
    rows: list[dict[str, Any]],
    liquidity: dict[str, Any],
) -> str:
    client = OpenAI(api_key=OPENAI_API_KEY)
    market_text = format_market_lines(rows)
    liquidity_text = format_liquidity(liquidity)
    prompt = f"""
다음 숫자만 근거로 한국 기관투자자용 글로벌 주간 시황을 작성하라.
확인되지 않은 뉴스나 숫자는 만들지 마라. 모든 문장은 한국어로 작성하라.

[주간 시장 데이터]
{market_text}

[연준 유동성]
{liquidity_text}

아래 형식을 정확히 지켜라.

1. 이번 주 한 줄 결론
두 문장 이내.

2. 자산별 해석
주식, 금리·달러, 원자재, 유동성을 각각 짧게 해석.

3. 포트폴리오 시사점
글로벌 주식 포트폴리오 관점의 리스크와 기회를 4개 항목으로 정리.

4. 다음 주 체크리스트
확정된 일정이라고 단정하지 말고, 관찰해야 할 변수 5개를 제시.

전체 1,600자 이내. 과도한 수사나 면책 문구는 쓰지 마라.
""".strip()
    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=(
            "당신은 20년 경력의 글로벌 주식 포트폴리오 매니저다. "
            "숫자를 정확히 읽고 간결한 한국어 투자 브리핑을 작성한다."
        ),
        input=prompt,
        max_output_tokens=2600,
    )
    result = (response.output_text or "").strip()
    if not result:
        raise RuntimeError("OpenAI 주간 분석 결과가 비어 있습니다.")
    return result


def main() -> None:
    require_environment()
    chat_id = resolve_chat_id()
    markets = market_snapshot()
    liquidity = liquidity_snapshot()
    analysis = generate_analysis(markets, liquidity)
    now = datetime.now(KST)
    report = "\n\n".join(
        [
            f"GLOBAL WEEKLY REVIEW ({now:%Y-%m-%d} KST)",
            "📊 주간 자산 흐름\n" + format_market_lines(markets),
            "💧 연준 유동성\n" + format_liquidity(liquidity),
            "📝 PM 주간 해석\n" + analysis,
        ]
    )
    send_telegram(chat_id, html.unescape(report))
    print("Weekly market review sent successfully.")


if __name__ == "__main__":
    main()
"""
Weekly Bot
─────────────────────────────────────────────────────────────────
봇 내용은 추후 구현 예정.

환경 변수 (Lambda / .env):
  TELEGRAM_TOKEN, CHAT_ID
  ANTHROPIC_API_KEY
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ─── 환경 변수 ────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")


# ─── Telegram ────────────────────────────────────────────────────────────────
def send_telegram(text: str) -> None:
    import requests
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.error("TELEGRAM_TOKEN 또는 CHAT_ID 없음")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        logger.error("Telegram 전송 실패: %s", e)


# ─── 유효성 검사 ──────────────────────────────────────────────────────────────
def _validate_env() -> bool:
    missing = [k for k in ["TELEGRAM_TOKEN", "CHAT_ID"] if not os.environ.get(k)]
    if missing:
        logger.error("필수 환경 변수 없음: %s", missing)
        return False
    return True


# ─── Weekly Bot 본체 (미구현) ─────────────────────────────────────────────────
def run_weekly_bot() -> None:
    """Weekly Bot 메인 로직 — 추후 구현."""
    now_str = (datetime.now() + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")
    logger.info("Weekly Bot 실행: %s KST", now_str)
    # TODO: 봇 내용 구현
    send_telegram(f"📅 <b>WEEKLY BOT ({now_str} KST)</b>\n\n(준비 중)")


# ─── 진입점 ──────────────────────────────────────────────────────────────────
def main() -> None:
    if not _validate_env():
        return
    run_weekly_bot()
    logger.info("Weekly Bot 전송 완료")


def lambda_handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    """
    AWS Lambda 진입점.

    EventBridge 규칙:
      {"bot_type": "weekly"} → cron(5 22 ? * SAT *)  토 07:05 KST

    필수 환경 변수: TELEGRAM_TOKEN, CHAT_ID, ANTHROPIC_API_KEY
    """
    load_dotenv()
    bot_type = (event or {}).get("bot_type", "weekly")
    logger.info("Lambda 실행: bot_type=%s", bot_type)

    try:
        main()
        return {"statusCode": 200, "body": "Weekly Bot 완료"}
    except Exception as e:
        logger.exception("Lambda 오류: %s", e)
        return {"statusCode": 500, "body": str(e)}
