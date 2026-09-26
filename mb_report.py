"""MarketBeat 목표가 변경이 얼마나 빨리 올라오는지 본다.

확장(extension/)이 5분마다 사용자 탭의 표를 읽어 보내면, 새 줄이 처음 보인 시각이 data/stocknews.db 의
mb_ratings 에 쌓인다. 이 스크립트는
  1. 시간대별로 새 줄이 몇 건 올라왔는지 (미국 동부 시각)
  2. 관심 종목의 변경이 뉴스(구글·야후)보다 먼저 보였는지, 늦게 보였는지
를 보여 준다. 측정을 시작할 때 이미 표에 있던 줄(baseline)은 빼고 센다.

  python mb_report.py
"""
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import stocknews
import store

ET = timezone(timedelta(hours=-4))   # 미국 동부 서머타임 (11월 첫 일요일까지)
KST = timezone(timedelta(hours=9))


def t(s):
    return datetime.fromisoformat(s) if s else None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    c = store.con()
    rows = [dict(r) for r in c.execute("select * from mb_ratings where baseline = 0 order by first_seen")]
    base = c.execute("select count(*), min(first_seen) from mb_ratings where baseline = 1").fetchone()
    if base[1]:
        print(f"측정 시작 {t(base[1]).astimezone(KST):%m-%d %H:%M} KST (그때 이미 있던 줄 {base[0]}건은 뺀다)")
    print(f"그 뒤 새로 보인 줄 {len(rows)}건\n")
    if not rows:
        return

    print("미국 동부 시각별 새 줄 (MarketBeat 에 처음 보인 시각)")
    by_hour = Counter(t(r["first_seen"]).astimezone(ET).strftime("%m-%d %H시") for r in rows)
    for h, n in sorted(by_hour.items()):
        print(f"  {h}  {'■' * min(n, 60)} {n}")

    # 관심 종목과 겹치는 변경: 같은 증권사가 나오는 뉴스가 있으면 뉴스 시각과 견준다
    try:
        stocks = stocknews.load_watchlist()
    except SystemExit:
        stocks = []
    tickers = {s.get("yahoo", "").split(".")[0].lstrip("^").upper(): s["name"] for s in stocks if s.get("yahoo")}
    mine = [r for r in rows if r["ticker"].upper() in tickers]
    print(f"\n관심 종목 변경 {len(mine)}건")
    news = [dict(r) for r in c.execute("select title, source, feed, created_at, found_at, tickers from news")]
    for r in mine:
        seen = t(r["first_seen"])
        firm = re.split(r"[ ,.&]", r["brokerage"])[0]
        name = tickers[r["ticker"].upper()]
        hits = [n for n in news if name in (n["tickers"] or "") and firm.lower() in n["title"].lower()]
        first = min(hits, key=lambda n: n["created_at"]) if hits else None
        line = (f"  {r['ticker']:5} {r['action']:22} {r['brokerage'][:22]:22} {r['pt_old'] or '-':>8} → {r['pt_new'] or '-':<8}"
                f" MarketBeat {seen.astimezone(KST):%m-%d %H:%M}")
        if first:
            gap = (seen - t(first["created_at"])).total_seconds() / 60
            line += f" | 뉴스 {t(first['created_at']).astimezone(KST):%H:%M} ({first['source'][:14]}) → MarketBeat 가 {abs(gap):.0f}분 {'늦음' if gap > 0 else '빠름'}"
        else:
            line += " | 같은 증권사 뉴스 없음"
        print(line)


if __name__ == "__main__":
    main()
