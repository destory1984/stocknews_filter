"""목표가 표 공용 DB. stocknews_filter 와 saveticker_filter 가 같은 파일을 함께 쓴다 (두 저장소에 같은 파일이 있다).

두 알리미가 저마다 뉴스에서 뽑은 목표가를 origin("stocknews" / "saveticker") 을 붙여 한 표에 적는다.
기사 제목·링크·언론사·시각도 함께 적어서, 한쪽 알리미가 다른 쪽 DB 를 몰라도 표를 그릴 수 있다.
파일 위치는 환경변수 STOCK_TARGETS_DB, 없으면 사용자 폴더의 .stock_targets/targets.db.
"""
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

PATH = Path(os.environ.get("STOCK_TARGETS_DB") or Path.home() / ".stock_targets" / "targets.db")
KST = timezone(timedelta(hours=9))

SCHEMA = """
create table if not exists targets (
    origin text, id text, n int,               -- 뽑은 알리미, 뉴스 id, 그 뉴스 안의 순번. n = -1 은 "물어봤더니 목표가 뉴스 아님"
    stock text, ticker text, broker text, action text, rating text, pt_old real, pt_new real, currency text,
    title text, title_ko text, url text, source text, created_at text, alerted int, checked_at text,
    primary key (origin, id, n));
create index if not exists targets_created on targets (created_at);
"""
COLS = ("origin", "id", "n", "stock", "ticker", "broker", "action", "rating", "pt_old", "pt_new", "currency",
        "title", "title_ko", "url", "source", "created_at", "alerted", "checked_at")

_lock = threading.Lock()
_local = threading.local()


def con() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(PATH), timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("pragma journal_mode=wal")   # 두 알리미가 함께 읽고 쓴다
        c.executescript(SCHEMA)
        _local.c = c
    return c


def ticker_key(t: str) -> str:
    """두 알리미의 티커 모양을 맞춘다: "005930.KS" → "005930", "$mu" → "MU"."""
    t = (t or "").strip().lstrip("$").upper()
    for suf in (".KS", ".KQ"):
        if t.endswith(suf):
            t = t[:-len(suf)]
    return t


def _rows(origin: str, rec: dict, items: list, at: str) -> list:
    news = (rec.get("title") or "", rec.get("title_ko") or "", rec.get("url") or "", rec.get("source") or "",
            rec.get("created_at") or "", 1 if rec.get("alerted") else 0)
    if not items:
        return [(origin, rec["id"], -1) + (None,) * 8 + news + (at,)]
    return [(origin, rec["id"], n, x["stock"], ticker_key(x.get("ticker")), x["broker"], x["action"], x.get("rating") or "",
             x.get("pt_old"), x.get("pt_new"), x.get("currency") or "") + news + (at,)
            for n, x in enumerate(items)]


def save(origin: str, rec: dict, items: list, at: str = ""):
    """한 뉴스에서 뽑은 목표가들. rec 은 판별 기록(id, title, title_ko, url, source, created_at, alerted).
    빈 목록이면 '목표가 뉴스 아님' 한 줄(n=-1)을 적어 다시 묻지 않는다."""
    at = at or datetime.now(KST).isoformat(timespec="seconds")
    with _lock:
        c = con()
        c.execute("delete from targets where origin=? and id=?", (origin, rec["id"]))
        c.executemany(f"insert into targets values ({','.join('?' * len(COLS))})", _rows(origin, rec, items, at))
        c.commit()


def checked(origin: str) -> set:
    """이 알리미가 목표가를 뽑으려고 이미 물어본 뉴스 id."""
    return {r[0] for r in con().execute("select distinct id from targets where origin=?", (origin,))}


def read(since: str) -> list:
    """since(ISO) 뒤에 나온 뉴스에서 두 알리미가 뽑은 목표가, 새것부터."""
    return [dict(r) for r in con().execute(
        "select * from targets where n >= 0 and created_at >= ? order by created_at desc", (since,))]


def count(origin: str) -> int:
    return con().execute("select count(*) from targets where origin=?", (origin,)).fetchone()[0]
