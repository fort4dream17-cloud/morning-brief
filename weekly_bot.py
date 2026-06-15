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
