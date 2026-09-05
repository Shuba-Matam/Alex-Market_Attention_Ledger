"""Market-data adapters. All adapters return normalized Quote candidates."""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

import yfinance as yf


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    volume: Optional[float]
    timestamp: int
    source: str


class SyntheticProvider:
    """Repeatable, no-network feed. State is derived from persisted history length."""
    source = "synthetic"

    def quote(self, symbol: str, sequence: int, scenario: str = "normal") -> Quote:
        key = int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16)
        base = 50 + (key % 45_000) / 100
        wave = math.sin(sequence * 0.71 + (key % 17)) * base * 0.006
        drift = sequence * base * 0.00025
        price = base + wave + drift
        volume = 90_000 + (key % 250_000) + abs(math.sin(sequence * 0.43)) * 45_000
        if scenario == "price_jump":
            price *= 1.085
        elif scenario == "volume_spike":
            volume *= 3.5
        elif scenario != "normal":
            raise ValueError("scenario must be normal, price_jump, or volume_spike")
        return Quote(symbol=symbol, price=round(price, 2), volume=round(volume), timestamp=int(time.time()), source=self.source)


class TwelveDataProvider:
    """Optional OHLCV adapter. A network failure is represented by None, never fake freshness."""
    source = "twelve_data"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("TWELVE_DATA_API_KEY")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def quote(self, symbol: str) -> Optional[Quote]:
        if not self.enabled:
            return None
        query = urllib.parse.urlencode({
            "symbol": symbol, "interval": "1min", "outputsize": 1, "apikey": self.api_key,
        })
        request = urllib.request.Request(f"https://api.twelvedata.com/time_series?{query}")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
            values = body.get("values") or []
            if not values:
                return None
            candle = values[0]
            # Twelve timestamps are exchange-local strings. We record fetch time to avoid pretending
            # a parsed local timestamp has a known timezone.
            return Quote(symbol, float(candle["close"]), float(candle["volume"]) if candle.get("volume") else None,
                         int(time.time()), self.source)
        except (OSError, ValueError, KeyError, urllib.error.URLError):
            return None


class YFinanceProvider:
    """Optional NSE OHLCV adapter. It never lets a provider error break ingestion."""

    source = "yfinance"

    def quote(self, symbol: str) -> Optional[Quote]:
        # Watchlists keep clean symbols (for example RELIANCE); only the provider
        # receives yfinance's NSE suffix.
        market_symbol = symbol if symbol.upper().endswith(".NS") else f"{symbol}.NS"
        try:
            history = yf.Ticker(market_symbol).history(
                period="5d", interval="1m", auto_adjust=False, raise_errors=False
            )
            if history.empty:
                return None
            latest = history.iloc[-1]
            close = float(latest["Close"])
            if not math.isfinite(close) or close <= 0:
                return None
            volume_value = latest.get("Volume")
            volume = float(volume_value) if volume_value is not None else None
            if volume is not None and not math.isfinite(volume):
                volume = None
            timestamp = int(history.index[-1].timestamp())
            return Quote(symbol, close, volume, timestamp, self.source)
        except Exception:
            # yfinance can raise for network, symbol, pandas parsing, and upstream
            # response errors. The service will retain prior data and mark it stale.
            return None


def choose_freshest(candidates: list[Quote]) -> Quote:
    """Newest provider timestamp wins; live provider wins a timestamp tie deterministically."""
    priority = {"yfinance": 3, "twelve_data": 2, "synthetic": 1}
    return max(candidates, key=lambda item: (item.timestamp, priority.get(item.source, 0)))
