"""
moves.py — 종목이 15분 사이에 크게 움직였는지 본다 (야후 5분봉, yfinance).

기준은 3%, 3배 ETF(SOXL, KORU)는 5%. 2026-09-21~26 닷새로 재 보니 이 기준에서
같은 종목을 1시간에 한 번만 세면 하루 3번쯤 걸렸다 (모두 3% 로 하면 5번, 2% 면 12번).
야후에는 오버나이트(한국 낮) 봉이 없어 그 시간에는 걸리지 않는다. 한국 종목은 보지 않는다.
"""

from __future__ import annotations

from datetime import datetime, timezone

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
