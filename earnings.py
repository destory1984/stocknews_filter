"""
earnings.py — 종목마다 다음 실적 발표일을 야후에서 받아 둔다.

yfinance 가 있으면 쓰고, 없으면 조용히 꺼진다 (pip install yfinance).
하루에 한 번만 받고 DB(meta 표)에 적어 둔다. 종목 수만큼 요청하는데 한 번에 0.2~1초씩 걸린다.
ETF(SOXL, KORU, DRAM 등)는 실적이 없어 비어 있다.
날짜는 야후가 주는 미국 날짜 그대로다. 장 마감 뒤 발표면 한국에서는 다음 날 아침이다.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import store

KST = timezone(timedelta(hours=9))
KEY = "earnings"


def _fetch(tickers) -> dict:
    try:
        import yfinance as yf
    except ImportError:
        return {}
    out = {}
    for t in tickers:
        try:
            cal = yf.Ticker(t).calendar
        except Exception:
            continue                      # ETF 는 404, 그 밖의 실패도 그 종목만 건너뛴다
        days = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if days:
            out[t] = min(days).isoformat()
    return out


def refresh(tickers, force: bool = False) -> dict:
    """오늘 이미 받았고 종목이 그대로면 적어 둔 것을 쓴다. {티커: 'YYYY-MM-DD'}"""
    tickers = sorted({t for t in tickers if t})
    today = datetime.now(KST).date().isoformat()
    try:
        saved = json.loads(store.get_meta(KEY, "{}") or "{}")
    except ValueError:
        saved = {}
    if not force and saved.get("day") == today and saved.get("tickers") == tickers:
        return saved.get("dates", {})
    dates = _fetch(tickers)
    store.set_meta(KEY, json.dumps({"day": today, "tickers": tickers, "dates": dates}))
    return dates


def cached() -> dict:
    try:
        return json.loads(store.get_meta(KEY, "{}") or "{}").get("dates", {})
    except ValueError:
        return {}


def upcoming(within_days: int = 30) -> list:
    """[(티커, 날짜, D-몇)] 가까운 순. 지난 것은 뺀다."""
    today = datetime.now(KST).date()
    out = []
    for t, d in cached().items():
        try:
            day = date.fromisoformat(d)
        except ValueError:
            continue
        left = (day - today).days
        if 0 <= left <= within_days:
            out.append((t, day, left))
    return sorted(out, key=lambda x: (x[1], x[0]))
