"""
weekly.py — 지난 7일을 종목별로 묶은 주간 리포트.

종목마다: 뉴스 수, 알림 수, 한 주 등락(야후 일봉 종가), 점수 높은 뉴스 셋.
전체: 판별한 뉴스 수, 알림 수, 👍/👎 수, 가장 많이 나온 사건.
판별 기록은 판별 목록과 같은 것(Watcher.judged)을 쓰고, 값만 야후에서 받는다.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def week_change(tickers) -> dict:
    """{티커: (7일 전 종가, 마지막 종가, 등락률%)}. yfinance 가 없거나 못 받으면 빈 것."""
    tickers = [t for t in tickers if t]
    if not tickers:
        return {}
    try:
        import yfinance as yf
        df = yf.download(tickers, period="10d", interval="1d", progress=False,
                         auto_adjust=False, group_by="ticker", threads=True)
    except Exception:
        return {}
    since = datetime.now(KST).date() - timedelta(days=7)
    out = {}
    for t in tickers:
        try:
            close = df[t]["Close"].dropna()
        except (KeyError, TypeError):
            continue
        before = close[[d.date() <= since for d in close.index]]
        if before.empty or close.empty:
            continue
        a, b = float(before.iloc[-1]), float(close.iloc[-1])
        if a:
            out[t] = (a, b, (b - a) / a * 100)
    return out


def build(recs: list, stocks: list, feedback: dict, threshold: int) -> dict:
    """recs: 지난 7일 판별 기록. stocks: 종목 목록. feedback: 뉴스 id → 마지막 반응."""
    by_stock = []
    prices = week_change([s.get("yahoo", "") for s in stocks])
    for s in stocks:
        mine = [r for r in recs if s["name"] in (r.get("tickers") or "") and r.get("by") != "rule"]
        top = sorted(mine, key=lambda r: (r["score"], r.get("created_at", "")), reverse=True)[:3]
        by_stock.append({"name": s["name"], "ticker": s.get("yahoo", ""), "news": len(mine),
                         "alerts": sum(1 for r in mine if r.get("alerted")),
                         "price": prices.get(s.get("yahoo", "")), "top": top})
    fb = [feedback[r["id"]] for r in recs if r["id"] in feedback and feedback[r["id"]]["like"] is not None]
    topics = Counter(r["topic"] for r in recs if r.get("topic") and r["score"] >= threshold)
    return {"news": len(recs), "alerts": sum(1 for r in recs if r.get("alerted")),
            "up": sum(1 for f in fb if f["like"]), "down": sum(1 for f in fb if not f["like"]),
            "topics": topics.most_common(5), "stocks": by_stock}


def telegram_text(rep: dict) -> str:
    """텔레그램용 짧은 요약 (HTML)."""
    lines = [f"<b>주간 리포트</b> · 뉴스 {rep['news']}건, 알림 {rep['alerts']}건"]
    movers = sorted((s for s in rep["stocks"] if s["price"]), key=lambda s: -abs(s["price"][2]))[:5]
    if movers:
        lines.append("등락: " + " · ".join(f"{s['ticker']} {s['price'][2]:+.1f}%" for s in movers))
    if rep["topics"]:
        lines.append("많이 나온 사건: " + ", ".join(f"{t} {n}" for t, n in rep["topics"][:3]))
    return "\n".join(lines)
