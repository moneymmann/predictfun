# Polymarket 15분 BTC 이진옵션 봇 사용법 (초보자용)

이 문서는 **코딩을 전혀 몰라도** 실행할 수 있도록 최대한 쉽게 적었습니다.

---

## 0) 이 봇이 하는 일

- Binance 실시간 가격(BTCUSDT 호가)을 받아서
- Polymarket 오더북과 비교해
- 수학 모델로 계산한 확률이 더 유리할 때만
- FOK(즉시 전량체결 아니면 취소) 주문을 넣습니다.

기본값은 안전하게 **실주문 OFF(페이퍼 모드)** 입니다.

---

## 1) 준비물

1. Python 3.10+
2. 인터넷 연결
3. Polymarket 계정 (실거래 시)
4. 필요한 패키지 설치

```bash
pip install websockets scipy py-clob-client
```

---

## 2) 가장 쉬운 실행 순서

### 2-1) 파일 복사

먼저 프로젝트 폴더로 이동:

```bash
cd /workspace/predictfun
```

페이퍼 모드(실주문 없음) 환경파일 생성:

```bash
cp .env.paper.example .env
```

실거래 모드 환경파일 생성:

```bash
cp .env.live.example .env
```

> 처음엔 반드시 `.env.paper.example`로 시작하세요.

---

### 2-2) `.env` 파일 열어서 값 입력

아래 3개는 필수입니다.

- `POLYMARKET_TOKEN_ID`: 거래할 YES/NO 토큰 ID
- `MARKET_STRIKE`: 기준 가격 (예: 95000)
- `MARKET_EXPIRY_TS`: 만기 유닉스 초(예: 1767225600)

실거래일 때는 추가로:

- `ENABLE_TRADING=true`
- `POLYMARKET_PRIVATE_KEY=...`
- 필요 시 `POLYMARKET_FUNDER=...`

---

### 2-3) 실행 (복붙)

```bash
bash run_bot.sh
```

정상 실행 시 로그 예시:

- `Engine started | enable_trading=False` → 페이퍼 모드
- `Connected to Binance bookTicker stream` → 바이낸스 연결 성공
- `P_true=...` → 모델 계산 중
- `EDGE FOUND ...` → 기회 감지
- `PAPER MODE: ... order skipped` → 페이퍼 모드라 주문은 안 넣음

---

## 3) 실거래로 바꾸는 방법 (중요)

`.env`에서 아래만 바꾸세요:

```env
ENABLE_TRADING=true
POLYMARKET_PRIVATE_KEY=여기에_개인키
```

그 다음 다시 실행:

```bash
bash run_bot.sh
```

주의:
- 실거래는 실제 돈이 오갑니다.
- 처음엔 아주 작은 금액(`WALLET_USDC`, `MAX_SHARES_PER_TRADE`)으로 테스트하세요.

---

## 4) 자주 나는 오류

### 오류: `ModuleNotFoundError: No module named 'websockets'`

해결:

```bash
pip install websockets scipy py-clob-client
```

### 오류: `POLYMARKET_TOKEN_ID, MARKET_STRIKE, MARKET_EXPIRY_TS ... required`

해결: `.env`에서 3개 값을 채우세요.

### 오류: `ENABLE_TRADING=true requires POLYMARKET_PRIVATE_KEY`

해결: 실거래 모드면 개인키를 넣거나, `ENABLE_TRADING=false`로 바꾸세요.

---

## 5) 절대 지켜야 할 안전 규칙

1. 처음엔 무조건 페이퍼 모드로 테스트.
2. 실거래 전 소액으로만 시작.
3. 개인키를 절대 공유하지 말 것.
4. 서버/PC 시간 동기화 유지.

---

## 6) 한 줄 요약

- 초보자는: `.env.paper.example` → `.env` 복사 후 값 입력 → `bash run_bot.sh`
- 실거래는: `.env`에서 `ENABLE_TRADING=true` + 개인키 입력 후 실행

