#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f .env ]]; then
  echo "[오류] .env 파일이 없습니다."
  echo "먼저 아래 중 하나를 실행하세요:"
  echo "  cp .env.paper.example .env"
  echo "  cp .env.live.example .env"
  exit 1
fi

set -a
# shellcheck disable=SC1091
source ./.env
set +a

echo "[정보] ENABLE_TRADING=${ENABLE_TRADING:-false}"
echo "[정보] bot.py 실행 시작"
python bot.py
