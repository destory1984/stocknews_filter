"""
moves.py — 종목이 15분 사이에 크게 움직였는지 본다 (야후 5분봉, yfinance).

기준은 3%, 3배 ETF(SOXL, KORU)는 5%. 2026-09-21~26 닷새로 재 보니 이 기준에서
같은 종목을 1시간에 한 번만 세면 하루 3번쯤 걸렸다 (모두 3% 로 하면 5번, 2% 면 12번).
야후에는 오버나이트(한국 낮) 봉이 없어 그 시간에는 걸리지 않는다. 한국 종목은 보지 않는다.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

WINDOW_BARS = 3          # 5분봉 3개 = 15분
STALE_MIN = 20           # 마지막 봉이 이보다 오래됐으면 장이 닫힌 것으로 보고 넘어간다


def check(tickers, pct_for) -> list:
    """[(티커, 변동률, 지금 값, 15분 전 값)] — 기준을 넘은 것만. pct_for(티커) 는 기준(%)."""
    tickers = [t for t in tickers if t and "." not in t]
    if not tickers:
        return []
    try:
        import yfinance as yf
    except ImportError:
        return []
    df = yf.download(tickers, period="1d", interval="5m", prepost=True, progress=False,
                     auto_adjust=False, group_by="ticker", threads=True)
    now = datetime.now(timezone.utc)
    out = []
    for t in tickers:
        try:
            close = df[t]["Close"].dropna()
        except (KeyError, TypeError):
            continue
        if len(close) <= WINDOW_BARS:
            continue
        last_at = close.index[-1].to_pydatetime()
        if (now - last_at).total_seconds() > STALE_MIN * 60:
            continue
        cur, before = float(close.iloc[-1]), float(close.iloc[-1 - WINDOW_BARS])
        if not before:
            continue
        change = (cur - before) / before * 100
        if abs(change) >= pct_for(t):
            out.append((t, change, cur, before))
    return out


PRICE_KEEP_SEC = 300     # 목표가 표가 쓰는 현재가는 이만큼 묵혀 쓴다 (표를 열 때마다 야후에 묻지 않게)
_prices = {"key": None, "at": 0.0, "data": {}}


def last_prices(tickers) -> dict:
    """{티커: (마지막 값, 그 시각)} 야후 5분봉의 마지막 봉 (장 전·장 뒤 포함, 주말이면 금요일 장 뒤 값).
    한국 종목(005930.KS)도 받는다. 받지 못한 티커는 빠진다. 야후가 안 되면 묵은 값이나 빈 것을 준다."""
    tickers = sorted({t for t in tickers if t})
    if not tickers:
        return {}
    if _prices["key"] == tickers and time.time() - _prices["at"] < PRICE_KEEP_SEC:
        return _prices["data"]
    try:
        import yfinance as yf
        df = yf.download(tickers, period="5d", interval="5m", prepost=True, progress=False,
                         auto_adjust=False, group_by="ticker", threads=True)
    except Exception:
        return _prices["data"] if _prices["key"] == tickers else {}
    out = {}
    for t in tickers:
        try:
            close = df[t]["Close"].dropna()
            if len(close):
                out[t] = (float(close.iloc[-1]), close.index[-1].to_pydatetime())
        except (KeyError, TypeError, ValueError):
            continue
    if out or _prices["key"] != tickers:
        _prices.update(key=tickers, at=time.time(), data=out)
    return _prices["data"]


REACT_MIN = 60           # 알림 뒤 이만큼 지난 값과 견준다
NEAR_MIN = 20            # 알림 때·1시간 뒤에 이 안의 봉이 없으면 장이 닫혀 있던 것으로 본다


def reaction(ticker: str, at: datetime):
    """(알릴 때 값, 1시간 뒤 값, 변동률%). 장이 닫혀 있었으면 None, 야후에서 받지 못했으면 False (다음에 다시).
    야후 5분봉 (장 전·장 뒤 포함). 한국 종목은 정규장 봉만 있다."""
    try:
        import yfinance as yf
        df = yf.download(ticker, period="5d", interval="5m", prepost=True, progress=False,
                         auto_adjust=False, threads=False)
        close = df["Close"].dropna()
        if hasattr(close, "columns"):          # 새 yfinance 는 티커 하나여도 열이 여러 겹이다
            close = close.iloc[:, 0]
    except Exception:
        return False
    if close.empty:
        return False
    at = at.astimezone(timezone.utc)
    later = at + timedelta(minutes=REACT_MIN)

    def price_at(t):
        before = close[close.index <= t]
        if before.empty or (t - before.index[-1].to_pydatetime()).total_seconds() > NEAR_MIN * 60:
            return None
        return float(before.iloc[-1])

    p0, p1 = price_at(at), price_at(later)
    if not p0 or not p1:
        return None
    return p0, p1, (p1 - p0) / p0 * 100
