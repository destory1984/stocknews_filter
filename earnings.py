"""
earnings.py — 종목마다 다음 실적 발표 시각을 받아 둔다.

- 시각: 야후 (yfinance 의 get_earnings_dates). 미국 동부 시각으로 오는 것을 한국 시각으로 바꿔 둔다.
  정각(16:00 등)은 대개 어림값이다. 실제로는 16:05 일 수도 16:30 일 수도 있다.
  (yfinance 의 calendar 는 이 시각을 PC 시간대로 바꾼 뒤 날짜만 남긴다. 그래서 쓰지 않는다.)
- 확정 / 추정: 나스닥(api.nasdaq.com/api/analyst/<티커>/earnings-date)이 날짜마다 알려 준다.
  "expected*" 는 회사가 공지한 날짜, "estimated" 는 지난 발표일로 어림잡은 날짜다.
  추정끼리는 야후와 나스닥이 일주일씩 어긋나기도 한다 (09-26 TSLA: 야후 10-21, 나스닥 10-28).
  확정인데 야후와 날이 다르면 나스닥 날짜를 쓴다.
- 장 전 / 장 뒤: 확정이면 나스닥 문구("after market close" 등), 아니면 야후 시각으로 가른다.
yfinance 가 없으면 조용히 꺼진다. 하루에 한 번만 받고 DB(meta 표)에 적어 둔다.
ETF(SOXL, KORU, DRAM 등)는 실적이 없어 비어 있다.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import store

KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")
KEY = "earnings"
FORMAT = 3                                  # 저장 모양이 바뀌면 올린다 (옛 것은 새로 받는다)
NASDAQ = "https://api.nasdaq.com/api/analyst/{}/earnings-date"


def _yahoo(t: str):
    """다음 발표 시각 (미국 동부, tz 있음). 없으면 None."""
    import yfinance as yf
    try:
        df = yf.Ticker(t).get_earnings_dates(limit=8)
    except Exception:
        return None                         # ETF 는 404, 그 밖의 실패도 그 종목만 건너뛴다
    if df is None or df.empty:
        return None
    now = datetime.now(timezone.utc) - timedelta(hours=12)   # 발표 당일에도 남겨 둔다
    future = sorted(ts.to_pydatetime() for ts in df.index if ts.to_pydatetime() >= now)
    if not future:
        return None
    ny = future[0].astimezone(NY)
    # 야후는 '장 마감 뒤' 를 서머타임과 상관없이 20:00 UTC 로 적는다. 11월 서머타임이 끝나면
    # 이것이 15:00(장 중)이 되어 버리므로, 뉴욕 시각 16:00 으로 되돌린다 (한국 06:00)
    if future[0].astimezone(timezone.utc).hour == 20 and future[0].minute == 0:
        ny = ny.replace(hour=16)
    return ny


def _nasdaq(t: str) -> dict:
    """나스닥이 보는 다음 발표: {confirmed, day(미국 날짜), session}. 못 받거나 날짜가 없으면 {}."""
    try:
        r = requests.get(NASDAQ.format(t), timeout=15,
                         headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        text = ((r.json().get("data") or {}).get("reportText")) or ""
    except (requests.RequestException, ValueError, AttributeError):
        return {}
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", text)
    if not m:
        return {}                           # "hasn't provided us with the upcoming earnings report date"
    low = text.lower()
    return {"confirmed": "expected*" in low and "estimated" not in low,
            "day": f"{m.group(3)}-{m.group(1)}-{m.group(2)}",
            "session": "장 뒤" if "after market close" in low else "장 전" if "before market open" in low else ""}


def _session(ny: datetime) -> str:
    minutes = ny.hour * 60 + ny.minute
    return "장 전" if minutes < 9 * 60 + 30 else "장 뒤" if minutes >= 16 * 60 else "장 중"


def _fetch(tickers) -> dict:
    try:
        import yfinance  # noqa: F401
    except ImportError:
        return {}
    out = {}
    for t in tickers:
        ny = _yahoo(t)
        if not ny:
            continue
        us = "." not in t                   # 005930.KS 같은 한국 종목은 나스닥에 없고 장 전/뒤도 가르지 않는다
        nq = _nasdaq(t) if us else {}
        session = _session(ny) if us else ""
        if nq.get("confirmed"):
            session = nq["session"] or session
            if nq["day"] != ny.date().isoformat():   # 회사가 공지한 날이 야후와 다르면 공지를 따른다
                y, mo, d = map(int, nq["day"].split("-"))
                ny = datetime(y, mo, d, 8 if session == "장 전" else 16, tzinfo=NY)
        out[t] = {"at": ny.astimezone(KST).isoformat(), "approx": ny.minute == 0, "session": session,
                  "confirmed": nq.get("confirmed") if nq else None,   # None: 나스닥에 없음 (한국 종목 등)
                  "nasdaq_day": nq.get("day", "")}
    return out


def refresh(tickers, force: bool = False) -> dict:
    """오늘 이미 받았고 종목이 그대로면 적어 둔 것을 쓴다. {티커: {at, approx, session, checked}}"""
    tickers = sorted({t for t in tickers if t})
    today = datetime.now(KST).date().isoformat()
    try:
        saved = json.loads(store.get_meta(KEY, "{}") or "{}")
    except ValueError:
        saved = {}
    if (not force and saved.get("v") == FORMAT and saved.get("day") == today
            and saved.get("tickers") == tickers):
        return saved.get("dates", {})
    dates = _fetch(tickers)
    store.set_meta(KEY, json.dumps({"v": FORMAT, "day": today, "tickers": tickers, "dates": dates}))
    return dates


def cached() -> dict:
    try:
        saved = json.loads(store.get_meta(KEY, "{}") or "{}")
    except ValueError:
        return {}
    return saved.get("dates", {}) if saved.get("v") == FORMAT else {}


def upcoming(within_days: int = 60) -> list:
    """[(티커, 한국 시각, D-몇, 항목)] 가까운 순. 지난 것은 뺀다 (발표 뒤 12시간까지는 남긴다)."""
    now = datetime.now(KST)
    out = []
    for t, item in cached().items():
        try:
            at = datetime.fromisoformat(item["at"]).astimezone(KST)
        except (KeyError, TypeError, ValueError):
            continue
        left = (at.date() - now.date()).days
        if at >= now - timedelta(hours=12) and left <= within_days:
            out.append((t, at, max(left, 0), item))
    return sorted(out, key=lambda x: (x[1], x[0]))
