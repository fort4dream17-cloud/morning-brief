# Global Morning Brief to Telegram

GitHub Actions에서 미국장 마감 30분 후 글로벌 시황 브리핑을 만들고 텔레그램으로 전송하는 구성입니다.

## What It Does

- Yahoo Finance 공개 차트 API로 주요 지수, 금리, FX, 원자재, VIX, 핵심 종목의 종가와 변동률을 수집합니다.
- 공개 RSS에서 시장 헤드라인을 모읍니다.
- OPENAI_API_KEY로 포트폴리오 매니저 관점의 한국어 해석과 뉴스 요약을 생성합니다.
- 텔레그램 전송 실패 시 GitHub Actions가 실패로 표시되도록 점검합니다.

## Required GitHub Secrets

GitHub repository에서 Settings > Secrets and variables > Actions > New repository secret에 아래 값을 넣습니다.

- OPENAI_API_KEY: OpenAI API key
- TELEGRAM_BOT_TOKEN: Telegram bot token
- TELEGRAM_CHAT_ID: Telegram chat ID
- FRED_API_KEY: FRED data API key

## Schedule

실행 파일은 .github/workflows/main.yml입니다. GitHub cron은 UTC 기준이므로, 한국시간 기준 발송 시간은 아래처럼 해석합니다.

- 미국 서머타임: 20:30 UTC = 한국시간 화~토 05:30 KST
- 미국 겨울시간: 21:30 UTC = 한국시간 화~토 06:30 KST

두 cron을 모두 등록해 두고, 실행 시점의 뉴욕 시간 기준으로 맞는 스케줄만 통과시키는 방식입니다. 즉 목표는 미국 정규장 마감 후 30분 뒤 전송입니다.

GitHub 예약 실행은 서버 부하에 따라 몇 분 늦어질 수 있습니다. 수동 테스트는 GitHub Actions 화면에서 Run workflow로 실행할 수 있습니다.
