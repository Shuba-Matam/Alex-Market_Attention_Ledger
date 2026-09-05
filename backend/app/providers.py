"""Market-data adapters. Two live sources only — no synthetic data.

Yahoo Finance's keyless chart endpoint is the primary source; Twelve Data
(when an API key is set) is the secondary cross-check. Prices are stored in
their native currency and converted to INR at the API boundary using a cached
FX rate, so NSE quotes are never double-converted.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    volume: Optional[float]
    timestamp: int
    source: str
    currency: str
    previous_close: Optional[float] = None


class HttpError(RuntimeError):
    """Network/HTTP failure while contacting a provider."""


_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_FX_TICKER = "INR=X"
_FX_DEFAULT_REFRESH_SECONDS = 300
_FX_RATE = None  # type: ignore[assignment]
_FX_RATE_FETCHED_AT = 0.0
_FX_LOCK = threading.Lock()


def _http_get_json(url: str, headers: Optional[dict] = None, timeout: int = 8) -> dict:
    merged = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    if headers:
        merged.update(headers)
    req = urllib.request.Request(url, headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        raise HttpError(str(exc)) from exc


class FxRateProvider:
    """Cached USD->INR rate fetched from Yahoo's chart endpoint (ticker INR=X).

    FX does not need 15-second freshness, so the rate is refetched only after
    FX_REFRESH_SECONDS (default 300) and the last good value is reused when
    Yahoo is unreachable.
    """

    def __init__(self, refresh_seconds: Optional[int] = None):
        self.refresh_seconds = (
            refresh_seconds
            if refresh_seconds is not None
            else int(os.getenv("FX_REFRESH_SECONDS", str(_FX_DEFAULT_REFRESH_SECONDS)))
        )

    def get_usd_to_inr(self, force: bool = False) -> Optional[float]:
        global _FX_RATE, _FX_RATE_FETCHED_AT
        now = time.time()
        with _FX_LOCK:
            if not force and _FX_RATE and (now - _FX_RATE_FETCHED_AT) < self.refresh_seconds:
                return _FX_RATE
            try:
                body = _http_get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{_FX_TICKER}")
                result = body["chart"]["result"][0]
                rate = float(result["meta"]["regularMarketPrice"])
                if rate <= 0:
                    return _FX_RATE
                _FX_RATE = rate
                _FX_RATE_FETCHED_AT = now
                return rate
            except (HttpError, KeyError, ValueError, TypeError, IndexError):
                return _FX_RATE


class YahooChartProvider:
    """Primary live source: Yahoo Finance chart endpoint. Keyless.

    Some tickers exist on both NSE and US exchanges (INFY trades as Infosys Ltd
    on NSE and as the Infosys ADR on NYSE). This is an India-first product, so
    the NSE listing (.NS) is tried first for suffix-less symbols and the US
    listing is the fallback. The resolution is cached, so steady-state polling
    costs one request per symbol either way.
    """

    source = "yahoo"

    _SUFFIX_CACHE: dict[str, str] = {}

    def quote(self, symbol: str) -> Optional[Quote]:
        upper = symbol.upper()
        if "." in upper:
            result = self._fetch(upper)
        else:
            cached = YahooChartProvider._SUFFIX_CACHE.get(upper)
            if cached:
                result = self._fetch(cached)
                if result is None:  # cached resolution went stale; re-resolve once
                    YahooChartProvider._SUFFIX_CACHE.pop(upper, None)
                    result = self._resolve(upper)
            else:
                result = self._resolve(upper)
        if result is None:
            return None
        try:
            meta = result["meta"]
            price = float(meta["regularMarketPrice"])
            if price <= 0:
                return None
            previous_close = meta.get("chartPreviousClose")
            if previous_close is not None:
                previous_close = float(previous_close)
            timestamp = int(meta["regularMarketTime"])
            currency = str(meta.get("currency") or "").upper()
            volume = self._latest_volume(result)
            return Quote(
                symbol=symbol,
                price=price,
                volume=volume,
                timestamp=timestamp,
                source=self.source,
                currency=currency,
                previous_close=previous_close,
            )
        except (KeyError, ValueError, TypeError, IndexError):
            return None

    @staticmethod
    def _resolve(upper: str) -> Optional[dict]:
        """Resolve a suffix-less symbol: NSE listing first, US fallback."""
        for candidate in (f"{upper}.NS", upper):
            result = YahooChartProvider._fetch(candidate)
            if result is not None:
                YahooChartProvider._SUFFIX_CACHE[upper] = candidate
                return result
        return None

    @staticmethod
    def _fetch(market_symbol: str) -> Optional[dict]:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(market_symbol)}"
        try:
            body = _http_get_json(url)
            return body["chart"]["result"][0]
        except (HttpError, KeyError, IndexError, TypeError):
            return None

    @staticmethod
    def _latest_volume(result: dict) -> Optional[float]:
        indicators = result.get("indicators") or {}
        quotes = indicators.get("quote") or []
        if not quotes:
            return None
        volumes = quotes[0].get("volume") or []
        for value in reversed(volumes):
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
        return None


class TwelveDataProvider:
    """Secondary live source via /quote. Optional; degrades gracefully without a key.

    The free tier allows ~800 requests/day, so calls are throttled to at most
    one per TWELVE_DATA_MIN_INTERVAL_SECONDS (default 110s ≈ 785/day). When the
    throttle blocks a call the symbol is simply single-sourced (Yahoo) for that
    poll cycle.

    Verified against the documented /quote response: it includes `currency`,
    `exchange`, `previous_close`, `timestamp`, `close` and `volume`. Currency
    inference from the exchange is only a fallback for older responses.
    """

    source = "twelve_data"

    _CALL_LOCK = threading.Lock()
    _LAST_CALL = 0.0

    def __init__(self, api_key: Optional[str] = None, min_interval: Optional[float] = None):
        self.api_key = api_key or os.getenv("TWELVE_DATA_API_KEY")
        self.min_interval = (
            min_interval
            if min_interval is not None
            else float(os.getenv("TWELVE_DATA_MIN_INTERVAL_SECONDS", "110"))
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _throttle(self) -> bool:
        """True when this call is allowed under the free-tier budget."""
        if self.min_interval <= 0:
            return True
        with TwelveDataProvider._CALL_LOCK:
            now = time.monotonic()
            if now - TwelveDataProvider._LAST_CALL >= self.min_interval:
                TwelveDataProvider._LAST_CALL = now
                return True
            return False

    def quote(self, symbol: str) -> Optional[Quote]:
        if not self.enabled or not self._throttle():
            return None
        params = urllib.parse.urlencode({"symbol": symbol, "apikey": self.api_key})
        url = f"https://api.twelvedata.com/quote?{params}"
        try:
            body = _http_get_json(url)
        except HttpError:
            return None
        if not isinstance(body, dict) or body.get("status") == "error":
            return None
        try:
            price = float(body["close"])
            if price <= 0:
                return None
            previous_close = body.get("previous_close")
            if previous_close not in (None, ""):
                previous_close = float(previous_close)
            else:
                previous_close = None
            timestamp_raw = body.get("timestamp") or body.get("datetime")
            timestamp = int(float(timestamp_raw)) if timestamp_raw is not None else int(time.time())
            volume_raw = body.get("volume")
            volume = float(volume_raw) if volume_raw not in (None, "") else None
            currency = self._resolve_currency(body)
            return Quote(
                symbol=symbol,
                price=price,
                volume=volume,
                timestamp=timestamp,
                source=self.source,
                currency=currency,
                previous_close=previous_close,
            )
        except (KeyError, ValueError, TypeError):
            return None

    @staticmethod
    def _resolve_currency(payload: dict) -> str:
        explicit = payload.get("currency")
        if explicit:
            return str(explicit).upper()
        exchange = (payload.get("exchange") or "").upper()
        if exchange in {"NSE", "BSE", "BSE_INDEX", "NSE_INDEX"}:
            return "INR"
        country = (payload.get("country") or "").upper()
        if country in {"INDIA", "IN"}:
            return "INR"
        return "USD"


def choose_freshest(candidates: list[Quote]) -> Quote:
    """Resolve a conflicting multi-source poll: fresher provider timestamp wins,
    with a deterministic priority tie-break."""
    priority = {"yahoo": 3, "twelve_data": 2}
    return max(candidates, key=lambda item: (item.timestamp, priority.get(item.source, 0)))


def convert_to_inr(price: float, currency: str, usd_to_inr: Optional[float]) -> float:
    """Convert `price` to INR. INR prices are returned unchanged (never double-converted)."""
    code = (currency or "").upper()
    if code in {"INR", ""}:
        return price
    if code == "USD" and usd_to_inr:
        return price * usd_to_inr
    # Unknown currencies are returned as-is so the UI can show provenance.
    return price


def conflict_threshold_pct() -> float:
    """Threshold above which two converted quotes are flagged as a real conflict."""
    return float(os.getenv("CONFLICT_THRESHOLD_PCT", "0.5"))


def is_conflict(inr_a: float, inr_b: float) -> bool:
    """True when two INR-converted prices for the same symbol differ beyond the threshold."""
    denominator = max(abs(inr_a), abs(inr_b))
    if denominator == 0:
        return False
    return abs(inr_a - inr_b) / denominator * 100 > conflict_threshold_pct()


MARKET_ASLEEP_AFTER_SECONDS = int(os.getenv("MARKET_ASLEEP_AFTER_SECONDS", "1800"))


def market_is_asleep(quote: Quote) -> bool:
    """True when the provider's last market event is far behind the wall clock.

    regularMarketTime only advances while a market trades, so a timestamp many
    minutes stale means that symbol's market is closed (night, weekend, holiday)
    without needing a per-exchange calendar.
    """
    return quote.timestamp < int(time.time()) - MARKET_ASLEEP_AFTER_SECONDS
