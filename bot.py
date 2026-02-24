import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import websockets
from scipy.stats import norm

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY, OrderType


# --- HFT 파라미터 영역: 실행 중 O(1) 조회가 필요하므로 딕셔너리로 즉시 접근한다. ---
PRESET_VOLATILITY_PARAMS = {
    "BTC": {"sigma": 0.45, "skew": -0.1, "kurtosis": 3.5},
}

MIN_EDGE_THRESHOLD = 0.02
KELLY_MULTIPLIER = 0.25

# 10초 3500건 burst 제한을 만족시키기 위한 토큰 버킷 파라미터
ORDER_RATE_LIMIT_CAPACITY = 3500
ORDER_RATE_LIMIT_REFILL_WINDOW_SEC = 10.0

# 전역 실시간 기초자산 가격(바이낸스 mid-price)
S: float = 0.0


@dataclass
class MarketConfig:
    token_id: str
    strike: float
    expiry_ts: float
    symbol: str = "BTC"


class AsyncTokenBucket:
    """간단한 asyncio 기반 토큰 버킷.

    - capacity: 버킷 최대치
    - refill_rate: 초당 충전량
    """

    def __init__(self, capacity: int, refill_window_sec: float):
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.refill_rate = float(capacity) / refill_window_sec
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, need: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.last_refill = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)

                if self.tokens >= need:
                    self.tokens -= need
                    return

                deficit = need - self.tokens
                sleep_s = max(deficit / self.refill_rate, 0.001)

            await asyncio.sleep(sleep_s)


def calculate_true_probability(S_: float, K: float, T: float, sigma: float, skew: float, kurtosis: float) -> float:
    """Edgeworth로 튜닝한 Cash-or-Nothing Call의 진성확률 계산."""
    if S_ <= 0 or K <= 0:
        return 0.0
    if T <= 0:
        return 1.0 if S_ > K else 0.0
    if sigma <= 0:
        return 1.0 if S_ > K else 0.0

    d2 = (math.log(S_ / K) - 0.5 * (sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2_adj = d2 - (skew / 6) * (d2**2 - 1) + (kurtosis / 24) * (d2**3 - 3 * d2)
    return float(norm.cdf(d2_adj))


def calculate_taker_fee(price: float, shares: float) -> float:
    """Polymarket 비선형 taker fee."""
    p = min(max(price, 0.0), 1.0)
    return shares * 0.25 * ((p * (1 - p)) ** 2)


class PolymarketHFTEngine:
    """Polymarket CLOB + Binance 초저지연 피드 결합 실행 엔진."""

    def __init__(self, market_cfg: MarketConfig):
        self.market_cfg = market_cfg

        self.host = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
        self.chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", str(POLYGON)))
        self.private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
        self.funder = os.getenv("POLYMARKET_FUNDER")
        self.enable_trading = os.getenv("ENABLE_TRADING", "false").lower() == "true"

        if self.enable_trading and not self.private_key:
            raise ValueError("ENABLE_TRADING=true requires POLYMARKET_PRIVATE_KEY")

        self.wallet_usdc = float(os.getenv("WALLET_USDC", "500"))
        self.max_shares_per_trade = float(os.getenv("MAX_SHARES_PER_TRADE", "100"))
        self.min_shares = float(os.getenv("MIN_SHARES", "5"))

        self._mid_price_lock = asyncio.Lock()
        self.latest_mid = 0.0

        self.bucket = AsyncTokenBucket(
            capacity=ORDER_RATE_LIMIT_CAPACITY,
            refill_window_sec=ORDER_RATE_LIMIT_REFILL_WINDOW_SEC,
        )

        self.client: Optional[ClobClient] = None
        if self.private_key:
            # --- 핵심 최적화: EIP-712 병목 제거를 위해 시작 시점 단 한 번 L2 creds 파생 ---
            # 이후 모든 주문은 L2 creds 기반 HMAC 서명 경로를 사용하도록 client를 재구성한다.
            init_client = ClobClient(
                host=self.host,
                key=self.private_key,
                chain_id=self.chain_id,
                signature_type=2,
                funder=self.funder,
            )
            self.api_creds = init_client.create_or_derive_api_creds()

            self.client = ClobClient(
                host=self.host,
                key=self.private_key,
                chain_id=self.chain_id,
                signature_type=2,
                funder=self.funder,
                creds=self.api_creds,
            )

        self.logger = logging.getLogger("hft-bot")

    async def update_mid_price(self, mid: float) -> None:
        global S
        async with self._mid_price_lock:
            self.latest_mid = mid
            S = mid

    async def read_mid_price(self) -> float:
        async with self._mid_price_lock:
            return self.latest_mid

    # --- 비동기 ingest 구조 채택 이유 ---
    # HFT에서는 가격 갱신과 전략 판단/주문이 서로 블로킹되면 즉시 엣지를 잃는다.
    # 따라서 Binance 수신은 독립 task로 분리하고, 내부에서는 전역 mid만 원자적으로 갱신한다.
    async def binance_bookticker_consumer(self) -> None:
        url = "wss://fstream.binance.com/ws/btcusdt@bookTicker"
        while True:
            try:
                async with websockets.connect(url, ping_interval=10, ping_timeout=10) as ws:
                    self.logger.info("Connected to Binance bookTicker stream")
                    async for raw in ws:
                        msg = json.loads(raw)
                        bid = float(msg.get("b", 0.0))
                        ask = float(msg.get("a", 0.0))
                        if bid > 0 and ask > 0 and ask >= bid:
                            mid = (bid + ask) * 0.5
                            await self.update_mid_price(mid)
            except Exception as exc:
                self.logger.exception("Binance websocket error: %s", exc)
                await asyncio.sleep(0.5)

    async def get_best_ask(self) -> Optional[tuple[float, float]]:
        if self.client is None:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY is required to fetch order book with current client config")
        # py-clob-client 동기 함수를 event loop에서 직접 호출하면 지연이 누적되므로 to_thread 사용
        book: Any = await asyncio.to_thread(self.client.get_order_book, self.market_cfg.token_id)

        asks = None
        if isinstance(book, dict):
            asks = book.get("asks")
        else:
            asks = getattr(book, "asks", None)

        if not asks:
            return None

        first = asks[0]
        if isinstance(first, dict):
            px = float(first.get("price"))
            sz = float(first.get("size"))
        else:
            px = float(getattr(first, "price"))
            sz = float(getattr(first, "size"))
        return px, sz

    async def place_fok_buy(self, price: float, shares: float) -> Any:
        if self.client is None:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY is required to place orders")
        await self.bucket.acquire(1.0)

        order_args = OrderArgs(
            token_id=self.market_cfg.token_id,
            price=price,
            size=shares,
            side=BUY,
        )
        signed_order = await asyncio.to_thread(self.client.create_order, order_args)
        return await asyncio.to_thread(self.client.post_order, signed_order, OrderType.FOK)

    # --- 전략 루프 상세 ---
    # 1) Binance 실시간 mid(S) 기반으로 TTM을 업데이트
    # 2) Edgeworth 보정 확률(P_true) 계산
    # 3) 오더북 최우선 매도(P_ask)와 비교해 기대수익 및 최소 엣지 조건 확인
    # 4) Kelly * multiplier로 보수적 sizing 후 FOK taker 주문 실행
    async def strategy_loop(self) -> None:
        params = PRESET_VOLATILITY_PARAMS[self.market_cfg.symbol]
        sigma = params["sigma"]
        skew = params["skew"]
        kurtosis = params["kurtosis"]

        while True:
            try:
                mid = await self.read_mid_price()
                if mid <= 0:
                    await asyncio.sleep(0.05)
                    continue

                now = time.time()
                T = max((self.market_cfg.expiry_ts - now) / (365.0 * 24 * 3600), 1e-9)

                p_true = calculate_true_probability(
                    S_=mid,
                    K=self.market_cfg.strike,
                    T=T,
                    sigma=sigma,
                    skew=skew,
                    kurtosis=kurtosis,
                )

                self.logger.info("P_true=%.4f | S=%.2f | T=%.8f", p_true, mid, T)

                best_ask = await self.get_best_ask()
                if best_ask is None:
                    await asyncio.sleep(0.05)
                    continue

                p_ask, ask_size = best_ask
                edge = p_true - p_ask
                if edge <= MIN_EDGE_THRESHOLD:
                    await asyncio.sleep(0.02)
                    continue

                f_kelly = max(0.0, min(edge / max(1 - p_ask, 1e-9), 1.0))
                frac = f_kelly * KELLY_MULTIPLIER
                shares = min(ask_size, self.max_shares_per_trade, (self.wallet_usdc * frac) / max(p_ask, 1e-9))

                if shares < self.min_shares:
                    await asyncio.sleep(0.02)
                    continue

                fee = calculate_taker_fee(p_ask, shares)
                expected_profit = (p_true - p_ask) * shares - fee

                if expected_profit <= 0:
                    await asyncio.sleep(0.02)
                    continue

                self.logger.info(
                    "EDGE FOUND | p_true=%.4f ask=%.4f edge=%.4f shares=%.2f exp_pnl=%.6f fee=%.6f",
                    p_true,
                    p_ask,
                    edge,
                    shares,
                    expected_profit,
                    fee,
                )

                if not self.enable_trading:
                    self.logger.info("PAPER MODE: ENABLE_TRADING=false, order skipped")
                    await asyncio.sleep(0.01)
                    continue

                result = await self.place_fok_buy(price=p_ask, shares=shares)
                self.logger.info("ORDER RESULT: %s", result)

                await asyncio.sleep(0.01)

            except Exception as exc:
                self.logger.exception("Strategy loop error: %s", exc)
                await asyncio.sleep(0.1)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    token_id = os.getenv("POLYMARKET_TOKEN_ID", "")
    strike = float(os.getenv("MARKET_STRIKE", "0"))
    expiry_ts = float(os.getenv("MARKET_EXPIRY_TS", "0"))

    if not token_id or strike <= 0 or expiry_ts <= 0:
        raise ValueError(
            "POLYMARKET_TOKEN_ID, MARKET_STRIKE, MARKET_EXPIRY_TS environment variables are required"
        )

    market_cfg = MarketConfig(
        token_id=token_id,
        strike=strike,
        expiry_ts=expiry_ts,
        symbol="BTC",
    )

    engine = PolymarketHFTEngine(market_cfg)
    logging.getLogger("hft-bot").info("Engine started | enable_trading=%s", engine.enable_trading)

    tasks = [
        asyncio.create_task(engine.binance_bookticker_consumer(), name="binance-ws"),
        asyncio.create_task(engine.strategy_loop(), name="strategy"),
    ]

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    for t in pending:
        t.cancel()
    for t in done:
        if exc := t.exception():
            raise exc


if __name__ == "__main__":
    asyncio.run(main())
