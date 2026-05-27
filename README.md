# Global Morning Brief to Notion

GitHub Actions에서 매일 07:00 KST 전후로 미국장 마감 브리프를 만들고 Notion의 `글로벌 모닝 시황 DB`에 날짜별 페이지로 쌓는 구성입니다.

## What It Does

- Yahoo Finance 공개 차트 API로 주요 지수, 금리, FX, 원자재, VIX, 핵심 종목의 종가와 변동률을 수집합니다.
- 공개 RSS에서 시장 헤드라인을 모읍니다.
- `OPENAI_API_KEY`가 있으면 포트폴리오 매니저 관점의 한국어 해석을 생성합니다.
- OpenAI 키가 없으면 숫자 기반의 규칙형 브리프를 생성합니다.
- Notion DB에 같은 날짜 페이지가 있으면 새로 만들지 않고 업데이트합니다.

## Required GitHub Secrets

GitHub repository에서 `Settings > Secrets and variables > Actions > New repository secret`에 아래 값을 넣습니다.

- `NOTION_TOKEN`: Notion integration token
- `NOTION_DATABASE_ID`: 글로벌 모닝 시황 DB ID. 현재 DB는 `b9307fc89d3e438986c7c0341cc1984d`
- `OPENAI_API_KEY`: 선택. 없으면 규칙형 브리프로 동작합니다.
- `TELEGRAM_BOT_TOKEN`: 선택. 텔레그램 전송을 원할 때만 넣습니다.
- `TELEGRAM_CHAT_ID`: 선택. 텔레그램 전송을 원할 때만 넣습니다.

Notion integration을 만든 뒤, `글로벌 모닝 시황 DB` 페이지에서 integration을 초대해야 합니다.

## Schedule

`.github/workflows/morning-brief.yml`은 UTC 기준 `22:00`에 실행됩니다. 한국시간으로는 매일 `07:00 KST`입니다.

GitHub 예약 실행은 서버 부하에 따라 몇 분 늦어질 수 있습니다. 수동 테스트는 GitHub Actions 화면에서 `Run workflow`로 실행할 수 있습니다.

## Local Test

```powershell
cd github_morning_brief
$env:NOTION_TOKEN="secret_xxx"
$env:NOTION_DATABASE_ID="b9307fc89d3e438986c7c0341cc1984d"
python morning_brief.py --dry-run
```

실제 Notion 업로드:

```powershell
python morning_brief.py
```

텔레그램 전송까지 테스트하려면 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 환경변수도 함께 설정합니다. 두 값이 없으면 텔레그램 전송은 자동으로 건너뜁니다.
