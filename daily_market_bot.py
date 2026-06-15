"""
Daily Market Bot (업그레이드 버전)
─────────────────────────────────────────────────────────────────
변경 사항:
  1. Gemini API → Claude API (claude-sonnet-4-6) 전환
  2. NEWS PART: ts2 별도 파트 제거 → 전 출처 통합, 관심도 TOP30 (3메시지 × 10건)
  3. 뉴스 출처 대폭 확장 (주식/경제/정치/거시 전방위)
  4. SUMMARY PART: 20년차 시니어 PM/애널리스트 페르소나 + 투자 아이디어 섹션 추가

환경 변수 (Lambda / .env):
  TELEGRAM_TOKEN, CHAT_ID
  ANTHROPIC_API_KEY
  FRED_API_KEY
"""

import html
import os
import re
import logging
from calendar import timegm
from datetime import datetime, timedelta, timezone
from time import mktime
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
import feedparser
import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ─── 로깅 ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("yfinance").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

os.environ.setdefault("YF_CACHE_DIR", "/tmp")

# ─── 환경 변수 ───────────────────────────────────────────────────────────────
load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
CHAT_ID          = os.getenv("CHAT_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
FRED_API_KEY     = os.getenv("FRED_API_KEY")

# Claude API 클라이언트
CLAUDE_MODEL = "claude-sonnet-4-6"
_CLAUDE_CLIENT = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# ─── 상수 ────────────────────────────────────────────────────────────────────
TELEGRAM_TIMEOUT = 15
FRED_TIMEOUT     = 20
NEWS_TIMEOUT     = 15
MIN_MESSAGE_LEN  = 10

# 뉴스 파트 설정
NEWS_TOTAL       = 30   # 전체 선별 건수
NEWS_BATCH       = 10   # 메시지 1개당 건수 (3메시지)
SUMMARY_BATCH    = 10   # Claude 요약 배치 크기

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ─── 지수 & 매크로 ─────────────────────────────────────────────────────────
INDEX_TICKERS = {
    "DOW":              "^DJI",
    "S&P500":           "^GSPC",
    "NASDAQ":           "^IXIC",
    "RUSSELL":          "^RUT",
    "🇪🇺 EUROSTOXX":   "^STOXX50E",
    "🇩🇪 DAX":         "^GDAXI",
    "🇫🇷 CAC":         "^FCHI",
    "🇬🇧 FTSE":        "^FTSE",
    "🇰🇷 KOSPI":       "^KS11",
    "🇰🇷 KOSDAQ":      "^KQ11",
    "🇯🇵 NIKKEI":      "^N225",
    "🇹🇼 TWSE":        "^TWII",
    "🇨🇳 상해종합":     "000001.SS",
    "🇭🇰 HANGSENG":    "^HSI",
}

INDEX_GROUPS = [
    ("🇺🇸 [미국]",  ["DOW", "S&P500", "NASDAQ", "RUSSELL"]),
    ("🇪🇺 [유럽]",  ["🇪🇺 EUROSTOXX", "🇩🇪 DAX", "🇫🇷 CAC", "🇬🇧 FTSE"]),
    ("🌏 [아시아]", ["🇰🇷 KOSPI", "🇯🇵 NIKKEI", "🇹🇼 TWSE", "🇨🇳 상해종합", "🇭🇰 HANGSENG"]),
]

# ─── Asia / US+EU 분리 상수 ───────────────────────────────────────────────────
_ASIA_INDEX_KEYS = {"🇰🇷 KOSPI", "🇰🇷 KOSDAQ", "🇯🇵 NIKKEI", "🇹🇼 TWSE", "🇨🇳 상해종합", "🇭🇰 HANGSENG"}
# 아시아 3개 파트 출력 순서 (한국 / 일본 / 중국·홍콩·대만)
_ASIA_INDEX_GROUPS = [
    ("🇰🇷 [한국]",       ["🇰🇷 KOSPI", "🇰🇷 KOSDAQ"]),
    ("🇯🇵 [일본]",       ["🇯🇵 NIKKEI"]),
    ("🇨🇳 [중국·아시아]", ["🇨🇳 상해종합", "🇭🇰 HANGSENG", "🇹🇼 TWSE"]),
]
ASIA_INDEX_TICKERS  = {k: v for k, v in INDEX_TICKERS.items() if k in _ASIA_INDEX_KEYS}
US_EU_INDEX_TICKERS = {k: v for k, v in INDEX_TICKERS.items() if k not in _ASIA_INDEX_KEYS}
US_EU_INDEX_GROUPS  = [
    ("🇺🇸 [미국]", ["DOW", "S&P500", "NASDAQ", "RUSSELL"]),
    ("🇪🇺 [유럽]", ["🇪🇺 EUROSTOXX", "🇩🇪 DAX", "🇫🇷 CAC", "🇬🇧 FTSE"]),
]

# 아시아 특징주 풀 (한국 KRX + 일본 TSE 대형주)
ASIA_WATCH_LIST: dict[str, str] = {
    # ── 한국 (KRX) ──
    "005930.KS": "삼성전자",    "000660.KS": "SK하이닉스",
    "373220.KS": "LG에너지솔루션", "005380.KS": "현대차",
    "000270.KS": "기아",        "005490.KS": "POSCO홀딩스",
    "035420.KS": "NAVER",       "035720.KS": "카카오",
    "068270.KS": "셀트리온",    "105560.KS": "KB금융",
    "055550.KS": "신한지주",    "066570.KS": "LG전자",
    "006400.KS": "삼성SDI",     "207940.KS": "삼성바이오로직스",
    "051910.KS": "LG화학",      "003550.KS": "LG",
    # ── 일본 (TSE) ──
    "7203.T": "Toyota",         "6758.T": "Sony",
    "9984.T": "SoftBank",       "9983.T": "Fast Retailing",
    "6861.T": "Keyence",        "8306.T": "MUFG",
    "4063.T": "Shin-Etsu",      "7974.T": "Nintendo",
    "8035.T": "Tokyo Electron", "6367.T": "Daikin",
    "6098.T": "Recruit",        "9433.T": "KDDI",
    "6954.T": "Fanuc",          "4519.T": "Chugai Pharma",
}

MACRO_TICKERS = {
    "₿ BTC":       "BTC-USD",
    "💵 DXY":      "DX-Y.NYB",
    "🛢️ OIL":      "CL=F",
    "🏗️ COPPER":   "HG=F",
    "🟡 GOLD":     "GC=F",
    "⚪ SILVER":   "SI=F",
    "🌾 WHEAT":    "ZW=F",
    "🌽 CORN":     "ZC=F",
    "⛽ NAT GAS":  "NG=F",
}

MACRO_ORDER = ["₿ BTC", "💵 DXY", "🛢️ OIL", "⛽ NAT GAS", "🏗️ COPPER",
               "🟡 GOLD", "⚪ SILVER", "🌾 WHEAT", "🌽 CORN"]

FRED_SERIES = {
    "WALCL":    True,
    "WTREGEN":  True,
    "RRPONTSYD": True,
    "SOFR":     False,
    "IORB":     False,
}

# ─── 뉴스 소스 (대폭 확장) ──────────────────────────────────────────────────
# 주식/시장
FINVIZ_HOME_URL  = "https://finviz.com/"
FINVIZ_NEWS_URL  = "https://finviz.com/news.ashx"

# 통합 RSS 피드 (주식+경제+정치+거시)
RSS_FEEDS: list[tuple[str, str]] = [
    # ── 직접 RSS (가장 안정적) ──────────────────────────────────────
    # Reuters - 직접 RSS
    ("Reuters Top",          "https://feeds.reuters.com/reuters/topNews"),
    ("Reuters Business",     "https://feeds.reuters.com/reuters/businessNews"),
    ("Reuters Markets",      "https://feeds.reuters.com/reuters/companyNews"),
    # CNBC - 직접 RSS
    ("CNBC Top",             "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("CNBC Finance",         "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
    ("CNBC Economy",         "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("CNBC Earnings",        "https://www.cnbc.com/id/15839135/device/rss/rss.html"),
    # MarketWatch - 직접 RSS
    ("MarketWatch Top",      "https://feeds.content.dowjones.io/public/rss/mw_top"),
    ("MarketWatch Economy",  "https://feeds.content.dowjones.io/public/rss/mw_economy"),
    ("MarketWatch Markets",  "https://feeds.content.dowjones.io/public/rss/mw_marketpulse"),
    # Yahoo Finance - 직접 RSS
    ("Yahoo Finance",        "https://finance.yahoo.com/news/rss/"),
    # Seeking Alpha - 직접 RSS
    ("Seeking Alpha MktCurr","https://seekingalpha.com/market-currents.xml"),
    ("Seeking Alpha News",   "https://seekingalpha.com/news.xml"),
    # AP - 직접 RSS
    ("AP Business",          "https://rsshub.app/apnews/topics/business"),
    ("AP Economy",           "https://rsshub.app/apnews/topics/economy"),
    # Investing.com - 직접 RSS
    ("Investing.com",        "https://www.investing.com/rss/news.rss"),
    ("Investing.com Economy","https://www.investing.com/rss/news_285.rss"),
    # The Verge / Ars Technica (Tech)
    ("The Verge",            "https://www.theverge.com/rss/index.xml"),
    ("Ars Technica",         "https://feeds.arstechnica.com/arstechnica/index"),
    # Politico - 직접 RSS
    ("Politico Economy",     "https://rss.politico.com/economy.xml"),
    # FT - 직접 RSS (무료 헤드라인)
    ("FT Markets",           "https://www.ft.com/markets?format=rss"),
    ("FT World",             "https://www.ft.com/world?format=rss"),
    # Nikkei Asia - Google News (직접 RSS 없음)
    ("Nikkei Asia",          "https://news.google.com/rss/search?q=site:asia.nikkei.com+when:24h&hl=en-US&gl=US&ceid=US:en"),
    # SCMP - Google News
    ("South China Morning",  "https://news.google.com/rss/search?q=site:scmp.com+economy+when:24h&hl=en-US&gl=US&ceid=US:en"),
    # 주제별 Google News (백업용)
    ("Fed/Rates",            "https://news.google.com/rss/search?q=federal+reserve+interest+rate+when:24h&hl=en-US&gl=US&ceid=US:en"),
    ("Tariff/Trade",         "https://news.google.com/rss/search?q=tariff+trade+war+sanctions+when:24h&hl=en-US&gl=US&ceid=US:en"),
    ("Earnings",             "https://news.google.com/rss/search?q=earnings+results+guidance+beat+miss+when:24h&hl=en-US&gl=US&ceid=US:en"),
]

# SUMMARY 전용 추가 스크래핑
SUMMARY_EXTRA_FEEDS: list[tuple[str, str]] = [
    ("Bloomberg", "https://feeds.bloomberg.com/markets/news.rss"),
    ("Reuters",   "https://www.reuters.com/markets/rss/"),
]

FOREX_BONDS_KEYS = [
    "EUR/USD", "USD/JPY", "GBP/USD", "BTC/USD",
    "10-Year Treasury", "30-Year Treasury",
]

HOURS_24 = timedelta(hours=24)
KST = ZoneInfo("Asia/Seoul")
NEW_YORK = ZoneInfo("America/New_York")


def _latest_completed_us_session(
    now_utc: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return the latest completed US regular session as UTC datetimes."""
    now_utc = now_utc or datetime.now(timezone.utc)
    now_et = now_utc.astimezone(NEW_YORK)
    session_date = now_et.date()
    session_end = datetime.combine(
        session_date,
        datetime.min.time().replace(hour=16),
        tzinfo=NEW_YORK,
    )

    if now_et < session_end:
        session_date -= timedelta(days=1)
    while session_date.weekday() >= 5:
        session_date -= timedelta(days=1)

    session_start = datetime.combine(
        session_date,
        datetime.min.time().replace(hour=9, minute=30),
        tzinfo=NEW_YORK,
    )
    session_end = datetime.combine(
        session_date,
        datetime.min.time().replace(hour=16),
        tzinfo=NEW_YORK,
    )
    return session_start.astimezone(timezone.utc), session_end.astimezone(timezone.utc)


def _us_session_label() -> str:
    start_utc, _ = _latest_completed_us_session()
    return f"{start_utc.astimezone(NEW_YORK):%Y-%m-%d} 09:30-16:00 ET"


# ═══════════════════════════════════════════════════════════════════════════════
# ─── TECH NEWS BOT (2) 상수 & 함수 ────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

# Tech News Bot: 각 run이 12h 창을 커버 (AM 10시→22시~10시, PM 22시→10시~22시)
# → 두 run 간 자연적 중복 차단 (별도 URL 캐시 불필요)
TECH_NEWS_HOURS = 12

SEMI_SOURCES: list[tuple[str, str]] = [
    # 전문 반도체 매체 - 직접 RSS
    ("DigiTimes",                  "https://news.google.com/rss/search?q=site:digitimes.com+when:12h&hl=en-US&gl=US&ceid=US:en"),
    ("DigiTimes Asia",             "https://news.google.com/rss/search?q=site:digitimes.asia+when:12h&hl=en-US&gl=US&ceid=US:en"),
    ("TrendForce",                 "https://news.google.com/rss/search?q=site:trendforce.com+when:12h&hl=en-US&gl=US&ceid=US:en"),
    ("SemiAnalysis",               "https://news.google.com/rss/search?q=site:semianalysis.com+when:12h&hl=en-US&gl=US&ceid=US:en"),
    ("THE ELEC",                   "https://news.google.com/rss/search?q=site:thelec.net+when:12h&hl=en-US&gl=US&ceid=US:en"),
    ("Semiconductor Engineering",  "https://semiengineering.com/feed/"),
    ("EE Times",                   "https://www.eetimes.com/feed/"),
    ("Nikkei Asia Tech",           "https://news.google.com/rss/search?q=site:asia.nikkei.com+semiconductor+OR+chip+when:12h&hl=en-US&gl=US&ceid=US:en"),
]

TECH_RSS_FEEDS_BOT2: list[tuple[str, str]] = [
    ("The Verge",        "https://www.theverge.com/rss/index.xml"),
    ("Wired",            "https://www.wired.com/feed/rss"),
    ("TechCrunch",       "https://techcrunch.com/feed/"),
    ("Ars Technica",     "https://feeds.arstechnica.com/arstechnica/index"),
    ("MIT Tech Review",  "https://www.technologyreview.com/feed/"),
    ("VentureBeat AI",   "https://venturebeat.com/ai/feed/"),
    ("Tom's Hardware",   "https://www.tomshardware.com/feeds/all"),
]

_TECH_SYSTEM = """당신은 글로벌 반도체·AI·테크 산업 전문 애널리스트입니다.
기술 투자자와 업계 전문가를 위한 Tech Intelligence Report를 작성합니다.
핵심 원칙:
- 기술적 중요도와 시장 영향력 기준으로 선별
- 중복 보도는 하나로 통합 (가장 상세한 내용 기준)
- HTML 태그: <b>, <a href="..."> 만 사용
- ** 마크다운 기호 절대 사용 금지
- 어조: 간결하고 전문적"""



# ─── 유틸 ────────────────────────────────────────────────────────────────────
def _validate_env() -> bool:
    required = {
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "CHAT_ID": CHAT_ID,


    }
    missing = [k for k, v in required.items() if not (v and str(v).strip())]
    if missing:
        logger.error("필수 환경 변수 없음: %s", ", ".join(missing))
        return False
    return True


def send_telegram(message: str) -> None:
    if not message or len(message.strip()) < MIN_MESSAGE_LEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        with requests.Session() as session:
            res = session.post(url, json=payload, timeout=TELEGRAM_TIMEOUT)
            if res.status_code != 200:
                payload["parse_mode"] = ""
                session.post(url, json=payload, timeout=TELEGRAM_TIMEOUT)
    except requests.RequestException as e:
        logger.warning("텔레그램 전송 실패: %s", e)


def _apply_bold_headers(text: str) -> str:
    """Claude 출력에서 제목 패턴을 감지해 <b> 태그로 볼드 처리."""
    import re
    lines = text.splitlines()
    result = []
    # 볼드 처리할 패턴들
    HEADER_PATTERNS = [
        r'^(📊\s*\[.+?\])',           # 📊 [Summary]
        r'^(🔬\s*\[.+?\])',           # 🔬 [심층 분석]
        r'^(📈\s*\[.+?\])',           # 📈 [특징주]
        r'^(⚠️\s*\[.+?\])',          # ⚠️ [주요 리스크]
        r'^([1-3]️⃣\s*.+)',           # 1️⃣ 2️⃣ 3️⃣ 소제목
        r'^(1️⃣|2️⃣|3️⃣|4️⃣)',        # 숫자 이모지 단독
        r'^(⚠️\s*[1-9️⃣].+)',        # ⚠️ 리스크 번호 제목
    ]
    combined = re.compile("|".join(HEADER_PATTERNS))
    for line in lines:
        stripped = line.strip()
        if stripped and combined.match(stripped):
            # HTML 특수문자 이스케이프 후 볼드
            safe = stripped.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            result.append(f"<b>{safe}</b>")
        else:
            # 일반 라인은 & 만 이스케이프 (< > 는 없어야 함)
            result.append(line.replace("&", "&amp;"))
    return "\n".join(result)



def send_telegram_plain(message: str) -> None:
    """제목 볼드 처리 후 HTML 모드로 전송. 4000자 초과 시 자동 분할."""
    if not message or len(message.strip()) < MIN_MESSAGE_LEN:
        return
    # 제목 볼드 처리
    formatted = _apply_bold_headers(message)
    MAX_LEN = 4000
    chunks = []
    if len(formatted) <= MAX_LEN:
        chunks = [formatted]
    else:
        current = ""
        for line in formatted.splitlines(keepends=True):
            if len(current) + len(line) > MAX_LEN:
                if current:
                    chunks.append(current.rstrip())
                current = line
            else:
                current += line
        if current.strip():
            chunks.append(current.rstrip())

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for i, chunk in enumerate(chunks):
        if not chunk.strip():
            continue
        text = chunk if i == 0 else ("(계속)\n" + chunk)
        payload = {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            with requests.Session() as session:
                res = session.post(url, json=payload, timeout=TELEGRAM_TIMEOUT)
                if res.status_code != 200:
                    # HTML 파싱 실패 시 plain으로 재시도
                    payload["parse_mode"] = ""
                    payload["text"] = message[:(MAX_LEN * (i+1))]
                    session.post(url, json=payload, timeout=TELEGRAM_TIMEOUT)
        except requests.RequestException as e:
            logger.warning("텔레그램 전송 실패 (청크 %d): %s", i, e)


def send_telegram_error(context: str, error: Exception) -> None:
    """에러 발생 시 Telegram으로 알림 전송 (traceback 포함)."""
    import traceback
    tb = traceback.format_exc()
    # traceback 마지막 3줄만 (핵심 위치)
    tb_lines = [l for l in tb.strip().splitlines() if l.strip()]
    tb_short = "\n".join(tb_lines[-4:])[:400]
    msg = (
        f"⚠️ <b>[Daily Market Bot 에러]</b>\n"
        f"<b>파트:</b> {html.escape(context)}\n"
        f"<b>오류:</b> {html.escape(str(error)[:200])}\n"
        f"<code>{html.escape(tb_short)}</code>"
    )
    send_telegram(msg)


def fmt_dt(dt_str: str | None) -> str:
    if not dt_str:
        return ""
    try:
        return datetime.strptime(dt_str, "%Y-%m-%d").strftime("%m/%d")
    except ValueError:
        return dt_str


# ─── Claude API ──────────────────────────────────────────────────────────────
def _claude_call(prompt: str, max_tokens: int = 4096, system: str | None = None) -> str | None:
    """Claude API 호출. 실패 시 None 반환."""
    if not _CLAUDE_CLIENT:
        logger.error("ANTHROPIC_API_KEY 없음 — Claude API 호출 불가")
        return None
    try:
        kwargs: dict[str, Any] = {
            "model": CLAUDE_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        res = _CLAUDE_CLIENT.messages.create(**kwargs)
        if res and res.content:
            return res.content[0].text.strip()
    except anthropic.APIConnectionError as e:
        logger.error("Claude API 연결 오류: %s", e)
    except anthropic.RateLimitError as e:
        logger.error("Claude API 레이트 리밋: %s", e)
    except anthropic.APIStatusError as e:
        logger.error("Claude API 상태 오류 %s: %s", e.status_code, e.message)
    except Exception as e:
        logger.exception("Claude API 예상치 못한 오류: %s", e)
    return None


# ─── FRED ────────────────────────────────────────────────────────────────────
def get_fred_observations(series_id: str, is_weekly: bool = False) -> tuple[dict | None, dict | None]:
    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}&api_key={FRED_API_KEY}"
        "&file_type=json&sort_order=desc&limit=30"
    )
    for attempt in range(2):  # 실패 시 1회 재시도
        try:
            r = requests.get(url, timeout=FRED_TIMEOUT)
            r.raise_for_status()
            obs = r.json().get("observations", [])
            valid = [
                {"val": float(o["value"]), "date": o["date"]}
                for o in obs
                if o.get("value") and o["value"] != "."
            ]
            if len(valid) < 2:
                return (valid[0], None) if valid else (None, None)
            curr, rest = valid[0], valid[1:]
            prev = rest[0]
            if is_weekly:
                for p in rest:
                    if p["val"] != curr["val"]:
                        prev = p
                        break
            return curr, prev
        except Exception as e:
            logger.debug("FRED 오류 %s (시도 %d): %s", series_id, attempt + 1, e)
            if attempt == 0:
                import time; time.sleep(2)  # 재시도 전 2초 대기
    return None, None


# ─── YFinance ────────────────────────────────────────────────────────────────
def _ytd_base_and_current(ticker: yf.Ticker, current_year: int) -> tuple[float | None, float | None, object | None]:
    """YTD 기준가, 현재가, 최근 daily history 반환 (3-tuple).

    [수정 이유]
    - 기존: ticker.history(period="10d") + ticker.history(start=..., end=...) 이중 호출
      → 둘 중 하나라도 실패하면 데이터 누락. KOSPI·NIKKEI 등 아시아 지수에서 빈번히 발생.
    - 변경: start=12/15 단일 호출 → 현재가·직전가·YTD 기준가 모두 한 번에 처리.
    - 연휴 대응: 12/15부터 fetch해 추석(최대 6일)·설날 포함 어떤 연휴에도 2거래일 이상 확보.
    - timezone-aware DatetimeIndex: strftime으로 연도 필터링 (KOSPI 등 로컬 연도 기준).
    """
    start = f"{current_year - 1}-12-15"   # 12/15부터: 연말 연휴 여유 확보
    # end를 UTC 기준 내일로 명시: KST 새벽 실행 시 UTC가 아직 전날이어서
    # yfinance의 exclusive end가 당일 데이터를 잘라내는 문제 방지.
    end = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        daily = ticker.history(start=start, end=end, interval="1d", actions=False)
        if daily.empty or len(daily) < 2:
            return None, None, None

        daily = daily.sort_index()

        # ── 데이터 신선도 검증 ──────────────────────────────────────────────────
        # yfinance가 stale 캐시를 반환하는 경우(특히 아시아 티커) 감지 후 보정.
        # 가장 최근 거래일이 5 캘린더일 초과로 오래됐으면 period="5d" 로 보충.
        try:
            latest_ts = daily.index[-1]
            # timezone-aware Timestamp → naive UTC date 변환
            if hasattr(latest_ts, 'tz_convert'):
                latest_date = latest_ts.tz_convert("UTC").date()
            elif hasattr(latest_ts, 'date'):
                latest_date = latest_ts.date()
            else:
                latest_date = pd.Timestamp(latest_ts).date()

            today_utc = datetime.now(timezone.utc).date()
            days_old = (today_utc - latest_date).days
            if days_old > 5:
                logger.info(
                    "Stale data (%d일 경과) 감지: %s (최신: %s) → period='5d' fallback",
                    days_old, ticker.ticker, latest_date,
                )
                fresh = ticker.history(period="5d", interval="1d", actions=False)
                if not fresh.empty and len(fresh) >= 2:
                    fresh = fresh.sort_index()
                    fresh_date = (
                        fresh.index[-1].tz_convert("UTC").date()
                        if hasattr(fresh.index[-1], 'tz_convert')
                        else fresh.index[-1].date()
                    )
                    if fresh_date > latest_date:
                        # YTD base는 original daily 유지, 최근 데이터만 fresh로 교체
                        combined = pd.concat([daily, fresh])
                        combined = combined[~combined.index.duplicated(keep="last")]
                        daily = combined.sort_index()
                        logger.info("보정 완료: %s 최신 → %s", ticker.ticker, daily.index[-1])
        except Exception as fe:
            logger.debug("Freshness check 오류 (%s): %s", ticker.ticker, fe)
        # ───────────────────────────────────────────────────────────────────────

        curr_close = float(daily["Close"].iloc[-1])
        if not (0 < curr_close < 1_000_000_000):
            return None, None, None

        # YTD base: 작년 마지막 거래일 종가
        prev_year_str = str(current_year - 1)
        try:
            mask = daily.index.strftime("%Y") == prev_year_str
            last_year_data = daily[mask]
        except Exception:
            last_year_data = daily[daily.index.year == (current_year - 1)]

        if not last_year_data.empty:
            base = float(last_year_data["Close"].iloc[-1])
        else:
            # fallback: 올해 첫 거래일 (연초 연휴로 작년 데이터가 없는 경우)
            curr_year_str = str(current_year)
            try:
                mask2 = daily.index.strftime("%Y") == curr_year_str
                this_year_data = daily[mask2]
            except Exception:
                this_year_data = daily[daily.index.year == current_year]
            base = float(this_year_data["Close"].iloc[0]) if not this_year_data.empty else float(daily["Close"].iloc[0])

        if base <= 0:
            return None, curr_close, daily

        ytd_ratio = (curr_close - base) / base
        if not (-0.90 <= ytd_ratio <= 5.0):
            logger.warning("비정상 YTD %.1f%% 감지 — base 무효화 처리", ytd_ratio * 100)
            return None, curr_close, daily

        return base, curr_close, daily
    except Exception as e:
        logger.debug("YF ytd 오류: %s", e)
        return None, None, None


def get_yf_data(tickers: dict[str, str]) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    current_year = datetime.now().year
    for display_name, symbol in tickers.items():
        for attempt in range(2):  # 최대 2회 시도 (간헐적 yfinance 오류 대응)
            try:
                t = yf.Ticker(symbol)
                base, curr, daily = _ytd_base_and_current(t, current_year)
                if curr is None or daily is None:
                    if attempt == 0:
                        logger.info("지수 데이터 없음 (1차): %s (%s) → 재시도", display_name, symbol)
                        continue
                    logger.warning("지수 데이터 없음 (최종): %s (%s)", display_name, symbol)
                    break
                if len(daily) < 2:
                    if attempt == 0:
                        logger.info("거래일 부족 (1차): %s (%s) → 재시도", display_name, symbol)
                        continue
                    logger.warning("거래일 데이터 부족 (<2): %s (%s) — 연휴 연속 가능성", display_name, symbol)
                    break
                prev_close = float(daily["Close"].iloc[-2])
                if prev_close <= 0:
                    logger.warning("직전 종가 비정상 (%.2f): %s", prev_close, display_name)
                    break
                chg = ((curr - prev_close) / prev_close) * 100
                if abs(chg) > 30:
                    if attempt == 0:
                        logger.info("비정상 일간 변동 %.2f%% (%s) → 재시도", chg, display_name)
                        continue
                    logger.warning("비정상 일간 변동 %.2f%% (%s) — 스킵", chg, display_name)
                    break
                ytd = ((curr - base) / base) * 100 if base is not None else 0.0
                results[display_name] = {"curr": curr, "chg": chg, "ytd": ytd}
                break  # 성공
            except Exception as e:
                if attempt == 0:
                    logger.info("get_yf_data 1차 실패 %s (%s): %s → 재시도", display_name, symbol, e)
                else:
                    logger.warning("get_yf_data 최종 실패 %s (%s): %s", display_name, symbol, e)
    return results


def get_tenyear_tnx() -> tuple[float | None, float | None]:
    try:
        hist = yf.Ticker("^TNX").history(period="5d", interval="1d")
        if hist.empty or len(hist) < 2:
            return None, None
        curr = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])
        return curr, curr - prev
    except Exception:
        return None, None


def get_vix() -> float | None:
    """VIX 공포지수."""
    try:
        hist = yf.Ticker("^VIX").history(period="5d", interval="1d")  # 2d→5d: 연휴 대응
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


def get_cnn_fear_greed() -> str:
    """CNN Fear & Greed Index 스크래핑. 실패 시 빈 문자열 반환."""
    try:
        url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        r = requests.get(url, headers=HTTP_HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        score = data.get("fear_and_greed", {}).get("score", None)
        rating = data.get("fear_and_greed", {}).get("rating", "")
        if score is None:
            return ""
        score = round(float(score), 1)
        # 이모지 매핑
        if score <= 25:
            emoji = "😨"
        elif score <= 45:
            emoji = "😟"
        elif score <= 55:
            emoji = "😐"
        elif score <= 75:
            emoji = "😊"
        else:
            emoji = "🤑"
        label_map = {
            "Extreme Fear": "극단적 공포",
            "Fear": "공포",
            "Neutral": "중립",
            "Greed": "탐욕",
            "Extreme Greed": "극단적 탐욕",
        }
        label_kr = label_map.get(rating, rating)
        return f"{emoji} CNN Fear&Greed: {score:.0f} ({label_kr})"
    except Exception as e:
        logger.debug("CNN Fear&Greed 수집 실패: %s", e)
        return ""


def get_oil_data() -> dict[str, Any]:
    """WTI/BRENT 가격, 등락률, YTD, 스프레드 반환."""
    result = {}
    current_year = datetime.now().year
    for name, sym in [("WTI", "CL=F"), ("BRENT", "BZ=F")]:
        try:
            t = yf.Ticker(sym)
            base, curr, daily = _ytd_base_and_current(t, current_year)
            if curr is None or daily is None or len(daily) < 2:
                continue
            prev = float(daily["Close"].iloc[-2])
            if prev <= 0:
                continue
            chg = ((curr - prev) / prev) * 100
            ytd = ((curr - base) / base) * 100 if base is not None else 0.0
            result[name] = {"curr": curr, "chg": chg, "ytd": ytd}
        except Exception as e:
            logger.debug("Oil data %s: %s", name, e)
    return result


def get_sector_etf_performance() -> str:
    """S&P 섹터 ETF 등락률 (전일 대비) + YTD. HTML 색상 포함."""
    sectors = {
        "XLK Tech":       "XLK",   # ~32%
        "XLF Finance":    "XLF",   # ~13%
        "XLV Health":     "XLV",   # ~12%
        "XLY Cons.Disc":  "XLY",   # ~11%
        "XLC Comm":       "XLC",   # ~9%
        "XLI Industr":    "XLI",   # ~8%
        "XLP Cons.Stpl":  "XLP",   # ~6%
        "XLE Energy":     "XLE",   # ~4%
        "XLRE RealEst":   "XLRE",  # ~2%
        "XLB Materials":  "XLB",   # ~2%
        "XLU Utilities":  "XLU",   # ~2%
    }
    current_year = datetime.now().year
    lines = []
    for name, sym in sectors.items():
        try:
            t = yf.Ticker(sym)
            base, curr, hist = _ytd_base_and_current(t, current_year)
            if curr is None or hist is None or len(hist) < 2:
                continue
            prev = float(hist["Close"].iloc[-2])
            if prev <= 0:
                continue
            chg = ((curr - prev) / prev) * 100
            ytd = ((curr - base) / base) * 100 if base is not None else 0.0
            # 전일 등락 / YTD 모두 ▲▼ / 상승 볼드 하락 밑줄
            chg_str = f'▲{chg:.2f}%' if chg >= 0 else f'▼{abs(chg):.2f}%'
            ytd_str = f'▲{ytd:.1f}%' if ytd >= 0 else f'▼{abs(ytd):.1f}%'
            line = f"{name}: {chg_str}  (YTD: {ytd_str})"
            if chg >= 0:
                line = f"<b>{line}</b>"
            else:
                line = f"<u>{line}</u>"
            lines.append(line)
        except Exception:
            continue
    return "\n".join(lines) if lines else ""



def get_notable_movers(news_text: str = "", top_n: int = 10) -> list[dict]:
    """
    특징주 수집: yfinance 등락률 + 거래량 + 뉴스 멘션 빈도 교차 선별.
    S&P500 대형주/시장주도주 풀에서 실제 데이터 기반으로 추출.
    """
    WATCH_LIST = {
        # Mega cap Tech / AI / 반도체
        "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA",
        "GOOGL": "Alphabet", "META": "Meta", "AMZN": "Amazon",
        "TSLA": "Tesla", "AVGO": "Broadcom", "AMD": "AMD",
        "ORCL": "Oracle", "CRM": "Salesforce", "ADBE": "Adobe",
        "QCOM": "Qualcomm", "MU": "Micron", "INTC": "Intel",
        "AMAT": "Applied Materials", "LRCX": "Lam Research",
        # Finance
        "JPM": "JPMorgan", "BAC": "BofA", "GS": "Goldman",
        "MS": "Morgan Stanley", "BLK": "BlackRock",
        "V": "Visa", "MA": "Mastercard",
        # Energy
        "XOM": "Exxon", "CVX": "Chevron", "OXY": "Occidental",
        # Healthcare / Pharma
        "LLY": "Eli Lilly", "JNJ": "Johnson", "UNH": "UnitedHealth",
        "PFE": "Pfizer", "MRK": "Merck", "ABBV": "AbbVie",
        # Consumer / Retail
        "WMT": "Walmart", "COST": "Costco", "NKE": "Nike",
        # Industrial / Defense
        "CAT": "Caterpillar", "BA": "Boeing", "LMT": "Lockheed",
        "RTX": "Raytheon", "GE": "GE Aerospace",
        # Others
        "BRK-B": "Berkshire", "NFLX": "Netflix", "DIS": "Disney",
        "COIN": "Coinbase", "MSTR": "MicroStrategy",
    }

    movers = []
    for ticker, name in WATCH_LIST.items():
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d", interval="1d")
            if hist.empty or len(hist) < 2:
                continue
            curr = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2])
            chg  = ((curr - prev) / prev) * 100
            vol_today = float(hist["Volume"].iloc[-1]) if "Volume" in hist.columns else 0
            vol_avg   = float(hist["Volume"].iloc[:-1].mean()) if "Volume" in hist.columns else 1
            vol_ratio = vol_today / vol_avg if vol_avg > 0 else 1.0
            movers.append({
                "ticker": ticker, "name": name,
                "chg": chg, "curr": curr,
                "vol_ratio": vol_ratio, "mention": 0,
            })
        except Exception:
            continue

    # 뉴스 멘션 카운트
    if news_text:
        for m in movers:
            ticker_cnt = len(re.findall(rf"\b{re.escape(m['ticker'])}\b", news_text))
            name_cnt   = len(re.findall(rf"\b{re.escape(m['name'])}\b", news_text, re.I))
            m["mention"] = ticker_cnt + name_cnt

    # 점수: |등락률|×0.5 + 거래량배수×0.3 + 멘션×0.2
    for m in movers:
        m["score"] = (
            abs(m["chg"]) * 0.5
            + min(m["vol_ratio"], 5.0) * 0.3
            + min(m["mention"], 10) * 0.2
        )

    # ±1% 미만 제거 후 점수 내림차순
    movers = [m for m in movers if abs(m["chg"]) >= 1.0]
    movers.sort(key=lambda x: x["score"], reverse=True)
    return movers[:top_n]


def format_notable_movers_block(movers: list[dict]) -> str:
    """특징주 리스트 → 프롬프트용 텍스트 블록."""
    if not movers:
        return "(특징주 데이터 없음)"
    lines = []
    for m in movers:
        sign  = "+" if m["chg"] >= 0 else ""
        vol_t = f" [거래량 {m['vol_ratio']:.1f}x]" if m["vol_ratio"] >= 2.0 else ""
        men_t = f" [뉴스 {m['mention']}회 언급]" if m["mention"] >= 2 else ""
        lines.append(f"{m['ticker']} ({m['name']}) {sign}{m['chg']:.2f}%{vol_t}{men_t}")
    return "\n".join(lines)


def get_notable_movers_asia(top_n: int = 12) -> list[dict]:
    """한국(KRX) + 일본(TSE) 특징주: 등락률 + 거래량 기반 선별."""
    movers = []
    for ticker, name in ASIA_WATCH_LIST.items():
        try:
            t = yf.Ticker(ticker)
            end = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
            hist = t.history(start=(datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d"),
                             end=end, interval="1d", actions=False)
            if hist.empty or len(hist) < 2:
                continue
            hist = hist.sort_index()
            curr = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2])
            if prev <= 0:
                continue
            chg = ((curr - prev) / prev) * 100
            vol_today = float(hist["Volume"].iloc[-1]) if "Volume" in hist.columns else 0
            vol_avg   = float(hist["Volume"].iloc[:-1].mean()) if "Volume" in hist.columns else 1
            vol_ratio = vol_today / vol_avg if vol_avg > 0 else 1.0
            movers.append({
                "ticker": ticker, "name": name,
                "chg": chg, "curr": curr,
                "vol_ratio": vol_ratio, "mention": 0,
            })
        except Exception:
            continue

    movers = [m for m in movers if abs(m["chg"]) >= 0.5]
    for m in movers:
        m["score"] = abs(m["chg"]) * 0.6 + min(m["vol_ratio"], 5.0) * 0.4
    movers.sort(key=lambda x: x["score"], reverse=True)
    return movers[:top_n]


# ─── ASIA MARKET PART ────────────────────────────────────────────────────────
def _generate_asia_brief(index_text: str, movers_block: str) -> str:
    """아시아 시황 + 특징주 Claude 분석."""
    if not _CLAUDE_CLIENT:
        return "[오류] ANTHROPIC_API_KEY 없음"
    kst = (datetime.now() + timedelta(hours=9)).strftime("%Y년 %m월 %d일 %H:%M")
    prompt = f"""[기준 시점] {kst} KST (아시아 장 마감 직후)

아래 데이터를 바탕으로 아시아 시장 브리프를 작성하라.

━━━━━━ 출력 포맷 (정확히 이 구조만 출력) ━━━━━━

📊 [아시아 시황]

🇰🇷 한국
(KOSPI·KOSDAQ 흐름 + 핵심 드라이버 1~2줄)

🇯🇵 일본
(NIKKEI 흐름 + 핵심 드라이버 1~2줄)

🇨🇳 중국
(상해종합·홍콩 항셍 흐름 + 핵심 드라이버 1~2줄)

🇹🇼 대만
(TWSE 흐름 + 핵심 드라이버 1~2줄)

📈 [특징주 — 한국/일본]
아래 [특징주 데이터] 종목만 사용. 등락률 크거나 거래량 높은 순으로 6~10개.
🟢 종목명(티커) +X.XX% — 원인 한 줄
🔴 종목명(티커) -X.XX% — 원인 한 줄
(종목 간 빈 줄 없이 연속 출력)

━━━━━━ 데이터 ━━━━━━

[아시아 지수]
{index_text}

[특징주 데이터 — 등락률·거래량 기반 선별 (이 종목만 사용)]
{movers_block}

━━━━━━ 제약 ━━━━━━
• 출력 포맷 구조 그대로 유지 (섹션 추가·변경 금지)
• ** 마크다운 기호 절대 사용 금지
• HTML 태그 절대 사용 금지
• 출처명 괄호 표기 금지
• 어조: '~함', '~음' 문어체 간결체"""

    text = _claude_call(prompt, max_tokens=1800, system=_SUMMARY_SYSTEM)
    return text if text else "[오류] Claude 응답 없음"


def run_asia_market_part() -> None:
    """ASIA MARKET PART — 평일 오후 4:10 KST 실행 (중국 장 마감 후)."""
    try:
        now_str = (datetime.now() + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")

        # ── 1. 아시아 지수 + 매크로 ──
        index_text, _ = _build_asia_index_section()
        macro_text = _build_macro_section()
        report = (
            f"🌏 <b>ASIA MARKET PART ({now_str} KST)</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>종합지수</b>\n{index_text}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>주요 매크로 & 원자재</b>\n{macro_text}"
        )
        send_telegram(report)
        logger.info("ASIA MARKET PART 지수+매크로 전송 완료")

        # ── 2. 특징주 수집 ──
        movers = get_notable_movers_asia(top_n=12)
        movers_block = format_notable_movers_block(movers)
        logger.info("아시아 특징주 %d건 수집 완료", len(movers))

        # ── 3. Claude 브리프 ──
        brief = _generate_asia_brief(index_text, movers_block)
        header = f"📝 <b>ASIA SUMMARY ({now_str} KST)</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        send_telegram_plain(header + brief)
        logger.info("ASIA SUMMARY 전송 완료")

    except Exception as e:
        logger.exception("ASIA MARKET PART 오류: %s", e)
        send_telegram_error("ASIA MARKET PART", e)


# ─── MARKET PART ─────────────────────────────────────────────────────────────
def _build_liquidity_section() -> str:
    lines = []
    walcl_c, walcl_p = get_fred_observations("WALCL",     is_weekly=True)
    tga_c,   tga_p   = get_fred_observations("WTREGEN",   is_weekly=True)
    rrp_c,   rrp_p   = get_fred_observations("RRPONTSYD", is_weekly=True)
    sofr_c,  _       = get_fred_observations("SOFR")
    iorb_c,  _       = get_fred_observations("IORB")

    # 순유동성: 3개 모두 있을 때만 계산, 없으면 어느 지표가 실패했는지 표시
    if walcl_c and tga_c and rrp_c:
        net_c = (walcl_c["val"] / 1000) - (tga_c["val"] / 1000) - rrp_c["val"]
        if walcl_p and tga_p and rrp_p:
            net_p = (walcl_p["val"] / 1000) - (tga_p["val"] / 1000) - rrp_p["val"]
            lines.append(f"🔹 순유동성: ${net_c:,.2f}B ({net_c - net_p:+.2f}B) [{fmt_dt(walcl_c['date'])}]")
        else:
            lines.append(f"🔹 순유동성: ${net_c:,.2f}B [{fmt_dt(walcl_c['date'])}]")
    else:
        missing = [m for m, v in [("Fed자산", walcl_c), ("TGA", tga_c), ("RRP", rrp_c)] if not v]
        lines.append(f"🔹 순유동성: N/A ({', '.join(missing)} 수집 실패)")

    # TGA / RRP 개별 표시 (순유동성 계산 실패와 무관하게 독립 출력)
    if tga_c:
        lines.append(f"🔸 TGA 잔액: ${tga_c['val']/1000:,.2f}B [{fmt_dt(tga_c['date'])}]")
    else:
        lines.append("🔸 TGA 잔액: N/A (수집 실패)")

    if rrp_c:
        lines.append(f"🔸 RRP 잔액: ${rrp_c['val']:,.2f}B [{fmt_dt(rrp_c['date'])}]")
    else:
        lines.append("🔸 RRP 잔액: N/A (수집 실패)")

    if sofr_c and iorb_c:
        diff = sofr_c["val"] - iorb_c["val"]
        lines.append(f"📊 SOFR-IORB 스프레드: {diff:.3f}% [{fmt_dt(sofr_c['date'])}]")
    return "\n".join(lines) if lines else "(유동성 데이터 수집 실패)"


def _build_index_section() -> tuple[str, dict[str, dict[str, Any]]]:
    """US + EU 지수만 (오전 6:30 US Market Part 전용)."""
    idx_res = get_yf_data(US_EU_INDEX_TICKERS)
    lines = []
    for label, keys in US_EU_INDEX_GROUPS:
        lines.append(label)
        for k in keys:
            if k in idx_res:
                d = idx_res[k]
                lines.append(f"{k}: {d['curr']:,.2f}  {d['chg']:+.2f}%  (YTD: {d['ytd']:+.1f}%)")
        lines.append("")
    return "\n".join(lines).strip(), idx_res


def _build_asia_index_section() -> tuple[str, dict[str, dict[str, Any]]]:
    """아시아 지수 3파트 (한국/일본/중국) — 오후 4:10 Asia Market Part 전용."""
    idx_res = get_yf_data(ASIA_INDEX_TICKERS)
    lines = []
    for label, keys in _ASIA_INDEX_GROUPS:
        lines.append(label)
        for k in keys:
            if k in idx_res:
                d = idx_res[k]
                lines.append(f"{k}: {d['curr']:,.2f}  {d['chg']:+.2f}%  (YTD: {d['ytd']:+.1f}%)")
        lines.append("")   # 파트 간 공백
    return "\n".join(lines).rstrip(), idx_res


def _build_macro_section() -> str:
    lines = []
    mac_res = get_yf_data(MACRO_TICKERS)

    # ── 1. BTC ──
    if "₿ BTC" in mac_res:
        d = mac_res["₿ BTC"]
        lines.append(f"₿ BTC: {d['curr']:,.2f}  {d['chg']:+.2f}%  (YTD: {d['ytd']:+.1f}%)")

    # ── 2. VIX + CNN Fear&Greed ──
    vix = get_vix()
    if vix is not None:
        lines.append(f"😱 VIX: {vix:.2f}")
    fg = get_cnn_fear_greed()
    if fg:
        lines.append(fg)

    lines.append("")

    # ── 3. DXY + US10Y ──
    if "💵 DXY" in mac_res:
        d = mac_res["💵 DXY"]
        lines.append(f"💵 DXY: {d['curr']:,.2f}  {d['chg']:+.2f}%  (YTD: {d['ytd']:+.1f}%)")
    curr_10y, chg_pp = get_tenyear_tnx()
    if curr_10y is not None:
        pp_str = f"  ({chg_pp:+.2f}%p)" if chg_pp is not None else ""
        lines.append(f"📈 US10Y: {curr_10y:.2f}%{pp_str}")

    lines.append("")

    # ── 4. WTI / BRENT / SPREAD ──
    oil = get_oil_data()
    if "WTI" in oil:
        d = oil["WTI"]
        lines.append(f"🛢️ WTI: {d['curr']:,.2f}  {d['chg']:+.2f}%  (YTD: {d['ytd']:+.1f}%)")
    if "BRENT" in oil:
        d = oil["BRENT"]
        lines.append(f"🛢️ BRENT: {d['curr']:,.2f}  {d['chg']:+.2f}%  (YTD: {d['ytd']:+.1f}%)")
    if "WTI" in oil and "BRENT" in oil:
        spread = oil["BRENT"]["curr"] - oil["WTI"]["curr"]
        lines.append(f"   └ WTI-BRENT Spread: {spread:+.2f}")

    # ── 5. NAT GAS ──
    if "⛽ NAT GAS" in mac_res:
        d = mac_res["⛽ NAT GAS"]
        lines.append(f"⛽ NAT GAS: {d['curr']:,.2f}  {d['chg']:+.2f}%  (YTD: {d['ytd']:+.1f}%)")

    lines.append("")

    # ── 6. 귀금속 / COPPER ──
    for k in ["🏗️ COPPER", "🟡 GOLD", "⚪ SILVER"]:
        if k in mac_res:
            d = mac_res[k]
            lines.append(f"{k}: {d['curr']:,.2f}  {d['chg']:+.2f}%  (YTD: {d['ytd']:+.1f}%)")

    return "\n".join(lines)


def run_us_market_part() -> dict[str, dict[str, Any]]:
    """US MARKET PART — 화~토 오전 7:00 KST, 직전 미국 정규장 기준."""
    try:
        now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
        report = (
            f"📊 <b>US MARKET PART ({now_str} KST)</b>\n"
            f"<i>기준: {_us_session_label()}</i>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
        )

        report += "<b>1. 유동성 데이터</b>\n"
        report += _build_liquidity_section()
        report += "\n━━━━━━━━━━━━━━━━━━\n\n"

        report += "<b>2. 종합지수</b>\n"
        index_text, idx_res = _build_index_section()
        report += index_text
        report += "\n\n━━━━━━━━━━━━━━━━━━\n\n"

        report += "<b>3. 주요 매크로 & 원자재</b>\n"
        report += _build_macro_section()
        report += "\n\n━━━━━━━━━━━━━━━━━━\n\n"

        report += "<b>4. 섹터 ETF 성과 (전일 대비)</b>\n"
        sector_txt = get_sector_etf_performance()
        report += sector_txt if sector_txt else "(섹터 데이터 없음)"

        send_telegram(report)
        logger.info("MARKET PART 전송 완료")
        return idx_res
    except Exception as e:
        logger.exception("MARKET PART 오류: %s", e)
        send_telegram_error("MARKET PART", e)
        return {}


# ─── SUMMARY PART ────────────────────────────────────────────────────────────
def _parse_forex_bonds(text: str) -> list[dict[str, str]]:
    out = []
    for name in FOREX_BONDS_KEYS:
        m = re.search(
            rf"{re.escape(name)}\s+([\d.,]+)\s+(?:[+-]?[\d.]+\s+)?([+-]?[\d.]+)\s*%",
            text,
        )
        if m:
            out.append({"name": name, "last": m.group(1).replace(",", ""), "change_pct": m.group(2)})
    return out


def _fetch_finviz_home() -> dict[str, Any]:
    out: dict[str, Any] = {"index_summary": "", "forex_bonds": []}
    try:
        r = requests.get(FINVIZ_HOME_URL, headers=HTTP_HEADERS, timeout=NEWS_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text()
        for tag in soup.find_all(["div", "p", "span", "a"]):
            t = tag.get_text(strip=True)
            if t and 60 < len(t) < 400 and any(kw in t.lower() for kw in ["close", "stock", "inflation", "rotation"]):
                out["index_summary"] = f"[Finviz] {t[:400]}"
                break
        out["forex_bonds"] = _parse_forex_bonds(text)
        for fb in out["forex_bonds"]:
            out["index_summary"] += f"\n[{fb['name']}] Last {fb['last']} Chg% {fb['change_pct']}%"
    except Exception as e:
        logger.warning("Finviz Home 오류: %s", e)
    return out


def _fetch_finviz_news() -> str:
    try:
        r = requests.get(FINVIZ_NEWS_URL, headers=HTTP_HEADERS, timeout=NEWS_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        items, seen = [], set()
        for a in soup.select("a[href^='http']"):
            href = a.get("href", "")
            if any(x in href for x in ["seekingalpha", "zerohedge", "blog"]):
                continue
            t = a.get_text(strip=True)
            if t and 25 < len(t) < 300 and t not in seen:
                seen.add(t)
                items.append(t)
        return "\n".join(items[:40]) if items else ""
    except Exception as e:
        logger.warning("Finviz News 오류: %s", e)
        return ""


def _fetch_summary_extra_news() -> str:
    """Bloomberg/Reuters RSS — 직전 미국 정규장 내 보충 뉴스."""
    start_utc, end_utc = _latest_completed_us_session()
    start_ts, end_ts = start_utc.timestamp(), end_utc.timestamp()
    lines = []
    for name, url in SUMMARY_EXTRA_FEEDS:
        try:
            feed = feedparser.parse(url, request_headers=HTTP_HEADERS)
            for e in feed.entries[:15]:
                pub = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
                if not pub:
                    continue
                try:
                    pub_ts = timegm(pub)
                    if not start_ts <= pub_ts <= end_ts:
                        continue
                except Exception:
                    continue
                title = (e.get("title") or "").strip()
                if not title:
                    continue
                summary = re.sub(r"<[^>]+>", "", (e.get("summary") or ""))[:200]
                lines.append(f"[{name}] {title}" + (f"\n  {summary}" if summary else ""))
        except Exception as e:
            logger.debug("Summary extra RSS 오류 %s: %s", name, e)
    return "\n\n".join(lines) if lines else ""


def _fetch_ts2_article() -> str:
    """ts2.tech 당일 Stock Market Today 기사."""
    from datetime import date
    base = "https://ts2.tech/en/stock-market-today-{dd:02d}-{mm:02d}-{yyyy}/"
    for days_ago in range(5):
        d = date.today() - timedelta(days=days_ago)
        url = base.format(dd=d.day, mm=d.month, yyyy=d.year)
        try:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=NEWS_TIMEOUT)
            if r.status_code != 200 or len(r.text) < 1000:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            body = (soup.find("article") or soup.find("main")
                    or soup.find(class_=re.compile(r"post|content|entry", re.I)))
            if body:
                parts = [
                    tag.get_text(strip=True)
                    for tag in body.find_all(["h1", "h2", "h3", "p"])
                    if len(tag.get_text(strip=True)) > 10
                ]
                return "\n\n".join(parts)[:15000]
        except Exception:
            continue
    return ""


def _get_market_data_for_summary() -> str:
    """유동성 + 지수 + 매크로 텍스트 블록."""
    parts = []
    liq = _build_liquidity_section()
    if liq:
        # 순유동성 수치는 MARKET PART에서 이미 전송 — Summary에서는 맥락만 유지
        parts.append("[유동성]\n" + liq)
    idx_txt, _ = _build_index_section()
    if idx_txt:
        parts.append("[종합지수]\n" + idx_txt)
    macro = _build_macro_section()
    if macro:
        parts.append("[매크로·원자재·금리]\n" + macro)
    sector = get_sector_etf_performance()
    if sector:
        parts.append("[섹터 ETF 전일 등락]\n" + sector)
    return "\n\n".join(parts)


def _format_forex_bonds(forex_bonds: list[dict[str, str]]) -> str:
    if not forex_bonds:
        return "(Forex/Bonds 없음)"
    return "\n".join(f"{x['name']}: {x['last']}  ({x['change_pct']}%)" for x in forex_bonds)


def _escape_html(s: str) -> str:
    """허용 태그(<b><i>) 유지하며 나머지 이스케이프."""
    ph = [("\x00B1\x00", "<b>"), ("\x00B2\x00", "</b>"),
          ("\x00I1\x00", "<i>"), ("\x00I2\x00", "</i>")]
    for p, tag in ph:
        s = s.replace(tag, p)
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for p, tag in ph:
        s = s.replace(p, tag)
    return s


# ── Summary용 시스템 프롬프트 (20년차 PM/애널리스트) ──────────────────────
_SUMMARY_SYSTEM = """당신은 20년 경력의 글로벌 주식 펀드 매니저 겸 수석 애널리스트입니다.
Goldman Sachs, Morgan Stanley 수준의 기관 리서치 품질로 데일리 브리프를 작성합니다.

핵심 원칙:
- 단순 사실 나열이 아닌 '왜 중요한가(So What)'를 반드시 제시
- Insight는 반드시 포지셔닝 시사점까지 도달해야 함:
  · 어떤 섹터/팩터/자산군이 유리해지는가, 불리해지는가
  · 현재 레짐(Risk-on/off, 성장주/가치주, EM/DM 등) 전환 여부
  · 비중 확대·축소·헤지 방향 중 하나를 명확히 제시
  · 단, 단정이 어려운 경우 조건(~시 ~전략)으로 표현
- 데이터 간 교차 검증: 금리+DXY+크레딧 스프레드, 유동성+섹터 로테이션 등 연계 분석
- 어조: '~함', '~음' 문어체 간결체
- HTML 태그 절대 사용 금지. 이모지와 텍스트만 사용할 것.
- ** 등 마크다운 강조 기호 절대 사용 금지."""


def _generate_summary(
    finviz_home: str,
    forex_bonds: list[dict[str, str]],
    market_data: str,
    finviz_news: str,
    extra_news: str,
    ts2_article: str,
    movers_block: str = "(특징주 데이터 없음)",
) -> str:
    if not _CLAUDE_CLIENT:
        return "[오류] ANTHROPIC_API_KEY 없음"

    kst = (datetime.now() + timedelta(hours=9)).strftime("%Y년 %m월 %d일 %H:%M")
    forex_block = _format_forex_bonds(forex_bonds)

    prompt = f"""[기준 시점] {kst} KST

아래 [시장 데이터]와 [뉴스 데이터]를 종합 분석하여 기관 투자자용 '데일리 마켓 브리프'를 작성하라.

━━━━━━ 출력 포맷 (정확히 이 구조만 출력) ━━━━━━

📊 [Summary]
1. (미 증시 3대 지수 흐름 한 줄 + 수치)
(1) (핵심 드라이버 1 — 1~2줄 이내로 간결하게)
(2) (핵심 드라이버 2 — 1~2줄 이내로 간결하게)

🔬 [심층 분석]
1️⃣ 경제
💡 (Fed 발언·경제지표·통화/재정정책·유동성 중 핵심 이슈 취합 → 현재 레짐(Risk-on/off·성장/가치·EM/DM) 판단 + 포지셔닝 시사점, 150자 이내)

2️⃣ 정치
💡 (지정학 리스크·미국 내 정치 뉴스 중 핵심 이슈 취합 → 직접 영향받는 섹터·자산군 + 포지셔닝 방향, 150자 이내)

3️⃣ 섹터 로테이션
💡 (섹터 ETF 전일 등락·YTD 상승률 기반 섹터 간 로테이션 판별 → 주도 섹터 지속 가능성·소외 섹터 반등 조건 + 구체적 ETF, 150자 이내)

4️⃣ 산업
💡 (AI·우주·양자컴퓨터·원전 등 다양한 섹터 뉴스 중 언급 多·관심도 高 주제 2~3개 취합 → 핵심 시사점, 150자 이내)

📈 [특징주]
아래 [특징주 데이터] 종목만 사용. 등락률 크거나 뉴스 언급 많은 순으로 7~10개.
🟢 TICKER +X.XX% — 원인 한 줄
🔴 TICKER -X.XX% — 원인 한 줄
(종목 간 빈 줄 없이 연속 출력)

━━━━━━ 데이터 ━━━━━━

[특징주 데이터 — 등락률·거래량·뉴스멘션 기반 선별 (이 종목만 사용)]
{movers_block}

[시장 데이터 — 수치는 이 블록만 사용]
{market_data}

[환율·금리 (Finviz)]
{forex_block}

[Finviz 홈 요약]
{finviz_home}

[Finviz News]
{finviz_news[:3000]}

[ts2.tech Stock Market Today]
{ts2_article[:10000]}

[Bloomberg·Reuters 보충]
{extra_news[:6000]}

━━━━━━ 제약 ━━━━━━
• 출력 포맷 구조 그대로 유지 (섹션 추가·변경 금지)
• ** 마크다운 기호 절대 사용 금지
• HTML 태그 절대 사용 금지
• 출처명 괄호 표기 금지
• 순유동성 수치 직접 언급 금지
• 투자 아이디어 언급 금지 (별도 메시지)"""

    text = _claude_call(prompt, max_tokens=2500, system=_SUMMARY_SYSTEM)
    return text if text else "[오류] Claude 응답 없음"


def _generate_ideas(
    market_data: str,
    finviz_news: str,
    extra_news: str,
    ts2_article: str,
) -> str:
    """투자 아이디어 + 주요 리스크 별도 생성."""
    if not _CLAUDE_CLIENT:
        return ""
    kst = (datetime.now() + timedelta(hours=9)).strftime("%Y년 %m월 %d일 %H:%M")
    prompt = f"""[기준 시점] {kst} KST

아래 시장 데이터와 뉴스를 바탕으로 주요 리스크를 작성하라.

━━━━━━ 출력 포맷 (정확히 이 구조만 출력) ━━━━━━

⚠️ [주요 리스크]

1️⃣ (리스크 제목)
📌 Fact: (구체적 수치·이벤트, 100자 이내)
⚡ Risk & Monitor: (영향받는 자산·섹터 + 확인해야 할 지표·이벤트·임계값, 130자 이내)

2️⃣ (리스크 제목)
📌 Fact: (100자 이내)
⚡ Risk & Monitor: (130자 이내)

3️⃣ (리스크 제목)
📌 Fact: (100자 이내)
⚡ Risk & Monitor: (130자 이내)

4️⃣ (리스크 제목, 해당 시만 작성)
📌 Fact: (100자 이내)
⚡ Risk & Monitor: (130자 이내)

━━━━━━ 데이터 ━━━━━━
[시장 데이터]
{market_data}

[뉴스]
{finviz_news[:2000]}
{ts2_article[:5000]}
{extra_news[:3000]}

━━━━━━ 제약 ━━━━━━
• 출력 포맷 구조 그대로 유지
• 중요도 높은 순으로 3~4개만
• ** 마크다운 기호 절대 사용 금지
• HTML 태그 절대 사용 금지
• 출처명 괄호 표기 금지"""

    text = _claude_call(prompt, max_tokens=1200, system=_SUMMARY_SYSTEM)
    return text if text else ""


def run_summary_part() -> None:
    if not _CLAUDE_CLIENT:
        logger.warning("ANTHROPIC_API_KEY 없음 — SUMMARY PART 생략")
        return
    try:
        logger.info("SUMMARY PART 수집 시작")
        home        = _fetch_finviz_home()
        finviz_news = _fetch_finviz_news()
        extra_news  = _fetch_summary_extra_news()
        market_data = _get_market_data_for_summary()
        ts2_article = _fetch_ts2_article()

        # 특징주: 뉴스 텍스트 합산 후 교차 선별
        news_corpus = " ".join([finviz_news, extra_news, ts2_article])
        movers      = get_notable_movers(news_text=news_corpus, top_n=10)
        movers_block = format_notable_movers_block(movers)
        logger.info("특징주 %d건 수집 완료", len(movers))

        summary = _generate_summary(
            finviz_home   = home["index_summary"],
            forex_bonds   = home.get("forex_bonds", []),
            market_data   = market_data,
            finviz_news   = finviz_news,
            extra_news    = extra_news,
            ts2_article   = ts2_article,
            movers_block  = movers_block,
        )
        ideas = _generate_ideas(
            market_data = market_data,
            finviz_news = finviz_news,
            extra_news  = extra_news,
            ts2_article = ts2_article,
        )
        now_str = (datetime.now() + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")

        # 메시지 1: 시황 브리프 (Summary + 심층분석 + 특징주)
        header1 = f"📝 SUMMARY PART ({now_str} KST)\n━━━━━━━━━━━━━━━━━━\n\n"
        send_telegram_plain(header1 + summary)

        # 메시지 2: 투자 아이디어 + 리스크
        if ideas:
            header2 = f"⚠️ RISK MONITOR ({now_str} KST)\n━━━━━━━━━━━━━━━━━━\n\n"
            send_telegram_plain(header2 + ideas)

        logger.info("SUMMARY PART 전송 완료")
    except Exception as e:
        logger.exception("SUMMARY PART 오류: %s", e)
        send_telegram_error("SUMMARY PART", e)




# ─── TECH NEWS BOT 수집 함수들 ───────────────────────────────────────────────
def _tech_cutoff_ts() -> float:
    """AM(10시)/PM(22시) 각 run이 12h 창을 커버하므로 TECH_NEWS_HOURS=12 사용."""
    return mktime((datetime.now(timezone.utc) - timedelta(hours=TECH_NEWS_HOURS)).timetuple())


def _fetch_semiconductor() -> list[dict]:
    """반도체 뉴스 RSS 수집 (스크래핑 대신 RSS/Google News 사용)."""
    return _fetch_tech_rss(SEMI_SOURCES, "semiconductor")


def _fetch_tech_rss(feeds: list[tuple], section: str) -> list[dict]:
    """RSS 피드 수집 (24h 필터)."""
    cutoff = _tech_cutoff_ts()
    out = []
    for name, url in feeds:
        try:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=NEWS_TIMEOUT)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            for e in getattr(feed, "entries", [])[:30]:
                try:
                    title = _safe_str(e.get("title"), 300)
                    link  = _safe_str(e.get("link") or e.get("id"))
                    if not link.startswith("http"):
                        for lk in (e.get("links") or []):
                            h = _safe_str(lk)
                            if h.startswith("http"):
                                link = h
                                break
                    if not title or not link.startswith("http"):
                        continue
                    pub = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
                    pub_ts = None
                    if pub:
                        try: pub_ts = mktime(pub)
                        except Exception: pass
                    if pub_ts and pub_ts < cutoff:
                        continue
                    snippet = re.sub(r"<[^>]+>", "",
                                     _safe_str(e.get("summary") or e.get("description"), 400))[:200]
                    out.append({"section": section, "source": name,
                                "title": title, "url": link.rstrip("/"), "snippet": snippet})
                except Exception:
                    continue
        except Exception as e:
            logger.debug("Tech RSS 오류 %s: %s", name, e)
    return out


def _tech_dedupe(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result = []
    for it in items:
        u = str(it.get("url") or "").rstrip("/")
        if u and u not in seen:
            seen.add(u)
            result.append(it)
    return result


def _build_tech_context() -> dict[str, str]:
    semi = _tech_dedupe(_fetch_semiconductor())
    tech = _tech_dedupe(_fetch_tech_rss(TECH_RSS_FEEDS_BOT2, "tech"))
    logger.info("Tech 수집: 반도체 %d / Tech %d", len(semi), len(tech))

    def _fmt(items: list[dict]) -> str:
        lines = [f"- [{it['source']}] {it['title']}\n  URL: {it['url']}\n  Snippet: {it['snippet'][:150]}"
                 for it in items]
        return "\n".join(lines) if lines else "(수집 없음)"

    return {"semiconductor": _fmt(semi), "tech": _fmt(tech)}


def _generate_tech_report(context: dict[str, str]) -> list[dict]:
    """
    Claude에게 번호만 받아서 코드에서 직접 뉴스 조립.
    각 섹션별 TOP5 인덱스를 JSON 형태로 반환받음.
    반환: [{"section": "semiconductor", "idx": 0, "title_kr": "한글제목"}, ...]
    """
    if not _CLAUDE_CLIENT:
        return []

    kst = (datetime.now() + timedelta(hours=9)).strftime("%Y년 %m월 %d일 %H:%M")

    # 각 섹션 데이터를 번호 붙여서 전달
    def _numbered(items_str: str) -> str:
        lines = []
        for i, line in enumerate(items_str.strip().splitlines()):
            if line.startswith("- ["):
                lines.append(f"{i+1}. {line[2:]}")
        return "\n".join(lines) if lines else "(없음)"

    def _parse_items(raw: str) -> list[dict]:
        """context 문자열에서 뉴스 아이템 파싱 (source, title, url 추출)."""
        result = []
        current = {}
        for line in raw.strip().splitlines():
            if line.startswith("- ["):
                if current:
                    result.append(current)
                m = re.match(r"- \[([^\]]+)\]\s*(.*)", line)
                current = {
                    "source": m.group(1) if m else "",
                    "title":  m.group(2).strip() if m else line[2:],
                    "url":    "",
                }
            elif line.strip().startswith("URL:") and current:
                url_val = line.strip()[4:].strip()
                current["url"] = url_val
        if current:
            result.append(current)
        return result

    semi_items = _parse_items(context["semiconductor"])
    tech_items = _parse_items(context["tech"])

    def _fmt_numbered(items):
        return "\n".join(f"{i+1}. [{it['source']}] {it['title']}" for i, it in enumerate(items)) or "(없음)"

    prompt = f"""[기준 시점] {kst} KST

아래 2개 섹션에서 각각 중요도 순 TOP 15~20개 번호와 한글 번역 제목을 출력하라.
(기사가 풍부하면 20개, 부족하면 15개로 조정. 중복 내용은 가장 상세한 것 1개만 선택)

중요도 판단 기준 (높은 순):
1. 주요 기업 실적·가이던스·M&A·CEO 발언
2. 공급망·생산 능력·수율 변화 (TSMC/삼성/SK하이닉스 등)
3. 규제·수출통제·지정학 리스크 (미-중 반도체 등)
4. 신제품·신기술 발표 (칩 아키텍처, AI 모델 등)
5. 시장 점유율·수요 전망 변화

출력 형식 (이것만, 다른 텍스트 없음):
SEMI: 번호,한글제목|번호,한글제목|...(15~20개)
TECH: 번호,한글제목|번호,한글제목|...(15~20개)

예시:
SEMI: 3,TSMC 2nm 수율 개선|1,삼성 HBM4 공급 확대|5,NVIDIA 칩 수출 제한|2,인텔 파운드리 전략 변경|4,SK하이닉스 HBM 점유율 확대|...

[Semiconductor 목록]
{_fmt_numbered(semi_items[:40])}

[Tech 목록]
{_fmt_numbered(tech_items[:40])}"""

    raw = (_claude_call(prompt, max_tokens=1500) or "").strip()

    # 파싱: "SEMI: 3,제목|1,제목..." 형태
    result = []
    section_map = {
        "SEMI": ("semiconductor", "🔬 <b>[Semiconductor]</b>", semi_items),
        "TECH": ("tech",          "📱 <b>[Tech]</b>",          tech_items),
    }

    for line in raw.splitlines():
        line = line.strip()
        for key, (section, header, items) in section_map.items():
            if line.startswith(f"{key}:"):
                entries = line[len(key)+1:].strip().split("|")
                news_list = []
                for entry in entries[:20]:
                    parts = entry.strip().split(",", 1)
                    if len(parts) == 2 and parts[0].strip().isdigit():
                        idx = int(parts[0].strip()) - 1
                        title_kr = parts[1].strip()
                        if 0 <= idx < len(items):
                            # 원본 dict에서 URL/source 직접 사용
                            item = items[idx]
                            news_list.append({
                                "source":   item.get("source", "Unknown"),
                                "title_kr": title_kr,
                                "url":      item.get("url", ""),
                            })
                result.append({"section": section, "header": header, "news": news_list})
                break

    return result


def _format_tech_section(section_data: dict) -> str:
    """섹션 데이터를 텔레그램 HTML 문자열로 변환."""
    lines = [section_data["header"]]
    for item in section_data["news"]:
        src   = html.escape(item["source"])
        title = html.escape(item["title_kr"])
        url   = item["url"]
        link  = f'<a href="{url}">(Link)</a>' if url else "(Link)"
        lines.append(f"<i>[{src}]</i> <b>{title}</b> {link}")
    return "\n".join(lines)


def run_tech_news_bot() -> None:
    """Tech News Bot — 2메시지 전송: [Semiconductor] / [Tech]."""
    if not _CLAUDE_CLIENT:
        logger.warning("ANTHROPIC_API_KEY 없음 — Tech News Bot 생략")
        return
    try:
        logger.info("Tech News Bot 시작")
        now_str  = (datetime.now() + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")
        context  = _build_tech_context()
        sections = _generate_tech_report(context)  # list[dict]

        if not sections:
            send_telegram(f"📡 <b>DAILY TECH NEWS ({now_str} KST)</b>\n⚠️ 리포트 생성 실패")
            return

        # 섹션별로 텍스트 조립
        section_texts = {s["section"]: _format_tech_section(s) for s in sections}

        # 메시지 1: Semiconductor
        if "semiconductor" in section_texts:
            msg1 = (f"📡 <b>DAILY TECH NEWS 1/2 ({now_str} KST)</b>\n━━━━━━━━━━━━━━━━━━\n\n"
                    + section_texts["semiconductor"])
            send_telegram(msg1)
            logger.info("Tech News Bot 1/2 전송 완료")

        # 메시지 2: Tech
        if "tech" in section_texts:
            msg2 = (f"📡 <b>DAILY TECH NEWS 2/2 ({now_str} KST)</b>\n━━━━━━━━━━━━━━━━━━\n\n"
                    + section_texts["tech"])
            send_telegram(msg2)
            logger.info("Tech News Bot 2/2 전송 완료")

        logger.info("Tech News Bot 전송 완료")
    except Exception as e:
        logger.exception("Tech News Bot 오류: %s", e)
        send_telegram_error("Tech News Bot", e)


def _safe_str(val: Any, max_len: int = 0) -> str:
    """feedparser 필드값을 안전하게 문자열로 변환. 중첩 구조 완전 처리."""
    # 최대 3번 언래핑 (중첩 dict/list 대응)
    for _ in range(3):
        if val is None:
            return ""
        if isinstance(val, str):
            break
        if isinstance(val, dict):
            val = (val.get("href") or val.get("url") or
                   val.get("value") or val.get("text") or
                   next(iter(val.values()), ""))
        elif isinstance(val, list):
            val = val[0] if val else ""
        else:
            break
    result = str(val).strip() if val else ""
    return result[:max_len] if max_len else result


def _extract_link(e: Any) -> str:
    """feedparser entry에서 URL 문자열 추출. 실패 시 빈 문자열."""
    # 1순위: link 필드
    link = _safe_str(e.get("link") or "")
    if link.startswith("http"):
        return link
    # 2순위: id 필드 (일부 Atom 피드)
    link = _safe_str(e.get("id") or "")
    if link.startswith("http"):
        return link
    # 3순위: links 리스트
    for lk in (e.get("links") or []):
        href = _safe_str(lk)
        if href.startswith("http"):
            return href
    return ""


def _normalize_item(item: dict) -> dict:
    """뉴스 아이템의 모든 필드를 str로 정규화. dict/list 필드 완전 제거."""
    return {
        "title":         str(item.get("title") or "").strip(),
        "url":           str(item.get("url") or "").strip(),
        "description":   str(item.get("description") or "").strip(),
        "source":        str(item.get("source") or "").strip(),
        "pub_timestamp": item.get("pub_timestamp"),  # float or None
        "summary":       str(item.get("summary") or "").strip(),
    }


def _fetch_rss(url: str, source_name: str, limit: int = 40) -> list[dict[str, Any]]:
    """단일 RSS 피드 수집. entry별 개별 try/except로 한 건 오류가 전체에 영향 없음."""
    out = []
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=NEWS_TIMEOUT)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:
        logger.debug("RSS 요청 오류 %s: %s", source_name, e)
        return out

    for e in getattr(feed, "entries", [])[:limit]:
        try:
            title = _safe_str(e.get("title"), 200)
            link  = _extract_link(e)
            if not title or not link:
                continue
            raw_desc = e.get("description") or e.get("summary") or ""
            desc = re.sub(r"<[^>]+>", "", _safe_str(raw_desc, 500))[:300].strip()
            pub_ts = None
            pub = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
            if pub:
                try:
                    pub_ts = timegm(pub)
                except Exception:
                    pass
            # 최종 타입 보장: url은 반드시 str
            assert isinstance(title, str)
            assert isinstance(link, str)
            out.append({"title": title, "url": link, "description": desc,
                        "source": source_name, "pub_timestamp": pub_ts})
        except Exception as entry_err:
            logger.debug("RSS entry 파싱 오류 %s: %s", source_name, entry_err)
            continue
    return out


def _collect_all_news() -> list[dict[str, Any]]:
    """전체 RSS 피드 수집 + URL 중복 제거 + 직전 미국 정규장 필터."""
    start_utc, end_utc = _latest_completed_us_session()
    start_ts, end_ts = start_utc.timestamp(), end_utc.timestamp()
    seen_urls: set[str] = set()
    combined: list[dict[str, Any]] = []

    for source_name, rss_url in RSS_FEEDS:
        for item in _fetch_rss(rss_url, source_name):
            # url 완전 str 보장
            raw_url = item.get("url") or ""
            if not isinstance(raw_url, str):
                raw_url = _safe_str(raw_url)
            raw_url = str(raw_url).strip().rstrip("/")
            if not raw_url or not raw_url.startswith("http"):
                continue
            if raw_url in seen_urls:
                continue
            seen_urls.add(raw_url)
            item["url"] = raw_url
            pt = item.get("pub_timestamp")
            if pt is None or not start_ts <= pt <= end_ts:
                continue
            combined.append(_normalize_item(item))

    logger.info(
        "전체 뉴스 수집: %d건 (미국 정규장 %s 필터 후)",
        len(combined),
        _us_session_label(),
    )
    return combined


def _claude_sort_and_summarize(items: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    """
    Claude 1회 호출로 중요도 정렬 + 한글 요약 동시 처리.
    반환: 중요도 순 정렬된 딕셔너리 리스트 (summary 키 포함)
    """
    if not _CLAUDE_CLIENT or not items:
        return [{**it, "summary": it["title"]} for it in items[:top_n]]

    lines = []
    for i, it in enumerate(items[:120]):
        desc = (it.get("description") or "")[:150]
        lines.append(f"{i+1}. [{it['source']}] {it['title']}" + (f"\n   {desc}" if desc else ""))

    prompt = """아래는 주식/경제/정치/산업 뉴스 목록이다.
펀드 매니저 관점에서 시장 중요도 순으로 정렬하고, 각 제목을 한글로 번역 요약하라.

중요도 기준 (높은 순):
1. Fed·금리·인플레이션·중앙은행 관련
2. 지정학 리스크 (전쟁·무역갈등·제재·관세)
3. 대형 기업 실적·M&A·가이던스
4. 반도체·AI·에너지 핵심 뉴스
5. 기타 거시 경제지표·산업 뉴스

출력 규칙:
- 번호,한글요약 형식으로 한 줄씩 출력
- 한글 요약은 50자 이내, 제목 내용만 (분석·의견 추가 금지)
- 다른 텍스트 없이 결과만 출력
- 최대 """ + str(top_n) + """개

출력 예시:
3. Fed, 기준금리 25bp 인상 결정
7. NVIDIA 분기 실적 예상치 대폭 상회
1. 미-중 반도체 추가 제재 협상 난항

뉴스 목록:
""" + "\n".join(lines)

    raw = (_claude_call(prompt, max_tokens=2000) or "").strip()

    ordered: list[dict[str, Any]] = []
    seen_idx: set[int] = set()

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # "번호. 요약" 또는 "번호, 요약" 두 형식 모두 처리
        m = re.match(r"^(\d+)[.,]\s*(.+)", line)
        if m:
            idx = int(m.group(1))
            summary = m.group(2).strip()[:150]
            if 1 <= idx <= len(items) and idx not in seen_idx:
                seen_idx.add(idx)
                base = _normalize_item(items[idx - 1])
                base["summary"] = str(summary).strip()
                ordered.append(base)

    if not ordered:
        result = []
        for it in items[:top_n]:
            n = _normalize_item(it)
            n["summary"] = n["title"]
            result.append(n)
        return result
    return ordered[:top_n]


def _format_news_message(batch: list[dict[str, Any]],
                          msg_num: int, total_msgs: int, now_str: str) -> str:
    start = (msg_num - 1) * NEWS_BATCH + 1
    end   = start + len(batch) - 1
    parts = [
        f"📰 <b>NEWS PART {msg_num}/{total_msgs} — Top {start}~{end}</b>\n"
        f"<b>{now_str} KST</b>\n━━━━━━━━━━━━━━━━━━\n"
    ]
    for it in batch:
        url_str  = str(it.get("url") or "").strip()
        link_tag = f'<a href="{html.escape(url_str)}"><b>(Link)</b></a>'
        src_str  = str(it.get("source") or "").strip()
        src_tag  = f"<i>[{html.escape(src_str)}]</i>"
        summary  = str(it.get("summary") or it.get("title") or "").strip()
        title_b  = f"<b>{html.escape(summary)}</b>"
        parts.append(f"{src_tag} {title_b} {link_tag}")
    return "\n\n".join(parts)


def run_news_part() -> None:
    if not _CLAUDE_CLIENT:
        logger.warning("ANTHROPIC_API_KEY 없음 — NEWS PART 생략")
        return
    try:
        logger.info("NEWS PART 수집 시작")
        now_str   = (datetime.now() + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")
        all_news  = _collect_all_news()

        if not all_news:
            send_telegram(f"📰 <b>NEWS PART ({now_str} KST)</b>\n⚠️ 24시간 내 수집된 뉴스가 없습니다.")
            return

        # 중요도 정렬 + 한글 요약 1회 호출
        top30 = _claude_sort_and_summarize(all_news, NEWS_TOTAL)
        logger.info("TOP%d 정렬+요약 완료", len(top30))

        # 3 배치 × 10건 전송
        total_msgs = (NEWS_TOTAL + NEWS_BATCH - 1) // NEWS_BATCH
        for i in range(0, len(top30), NEWS_BATCH):
            batch   = top30[i : i + NEWS_BATCH]
            msg_num = i // NEWS_BATCH + 1
            msg     = _format_news_message(batch, msg_num, total_msgs, now_str)
            send_telegram(msg)
            logger.info("NEWS PART %d/%d 전송 완료", msg_num, total_msgs)

    except Exception as e:
        logger.exception("NEWS PART 오류: %s", e)
        send_telegram_error("NEWS PART", e)


# ─── 진입점 ──────────────────────────────────────────────────────────────────
def main() -> None:
    if not _validate_env():
        raise RuntimeError("필수 환경 변수가 누락되어 브리핑 생성을 중단합니다.")
    run_us_market_part()
    run_summary_part()
    run_news_part()
    logger.info("US Market Bot 전송 완료")


def lambda_handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    """
    AWS Lambda 진입점 — bot_type으로 분기.

    EventBridge 규칙별 input / cron (UTC 기준):
      {"bot_type": "asia_market"} → cron(10 7 ? * MON-FRI *)   월~금 16:10 KST
                                     중국 장 마감 후: 한국/일본/중국 지수 + 매크로 + 특징주 브리프
      {"bot_type": "us_market"}   → cron(30 21 ? * MON-FRI *)  화~토 06:30 KST
                                     미국·유럽 지수 + Summary + News
      {"bot_type": "tech_news"}   → cron(0 1 ? * MON-FRI *)    월~금 10:00 KST (AM)
                                     cron(0 13 ? * MON-FRI *)   월~금 22:00 KST (PM)
      {"bot_type": "weekly"}      → cron(5 22 ? * SAT *)        토 07:05 KST (예정)

    Tech News Bot은 12h 창으로 AM/PM 자연 분리 → 중복 뉴스 없음.
    필수 환경 변수: TELEGRAM_TOKEN, CHAT_ID, ANTHROPIC_API_KEY, FRED_API_KEY
    """
    load_dotenv()
    bot_type = (event or {}).get("bot_type", "us_market")
    logger.info("Lambda 실행: bot_type=%s", bot_type)

    try:
        if bot_type == "asia_market":
            run_asia_market_part()
            return {"statusCode": 200, "body": "Asia Market Bot 완료"}
        elif bot_type == "tech_news":
            run_tech_news_bot()
            return {"statusCode": 200, "body": "Tech News Bot 완료"}
        else:
            # 기본: us_market (구 daily_market 포함 하위호환)
            main()
            return {"statusCode": 200, "body": "US Market Bot 완료"}
    except Exception as e:
        logger.exception("Lambda 실행 실패 (%s): %s", bot_type, e)
        send_telegram_error(f"Lambda/{bot_type}", e)
        return {"statusCode": 500, "body": str(e)}


if __name__ == "__main__":
    import sys
    # 사용법:
    #   python daily_market_bot.py              → Daily Market Bot (기본)
    #   python daily_market_bot.py tech         → Tech News Bot
    #   python daily_market_bot.py market       → Daily Market Bot
    if len(sys.argv) > 1 and sys.argv[1] == "tech":
        print("=== Tech News Bot 실행 ===")
        run_tech_news_bot()
    else:
        print("=== Daily Market Bot 실행 ===")
        main()
