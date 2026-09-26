"""뉴스·판별·반응 기록을 SQLite 한 파일(data/stocknews.db)에 둔다.

koreainvest 의 bars.db 와 같은 방식: WAL 로 열어 읽는 동안에도 쓸 수 있고, 다른 쪽이 쓰는 중이면 30초까지 기다린다.
스레드마다 연결을 따로 연다 (판별 루프, 웹 페이지, 요약 스레드가 함께 쓴다).

표
  news      RSS 에서 받은 뉴스 (id = 링크 해시). rss_summary 는 RSS 에 딸려 온 설명 (야후만 있다)
  judged    판별 결과 (점수·이유·사건·번역 제목·요약·알림 여부)
  feedback  🔔10·👍·👎·🔕0 반응. 누를 때마다 한 줄씩 쌓고, 같은 뉴스는 마지막 줄이 이긴다
  meta      옛 CSV·jsonl 을 옮겼는지 등
"""
import csv
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "data" / "stocknews.db"

NEWS_COLS = ["id", "created_at", "found_at", "tickers", "feed", "source", "title", "url", "rss_summary"]
JUDGED_COLS = ["id", "title", "url", "source", "tickers", "created_at", "score", "reason", "topic", "say",
               "title_ko", "summary_ko", "summary_by", "summary_state", "alerted", "late", "by", "at",
               "published_real", "stale"]
BOOL_COLS = {"alerted", "late", "stale"}

SCHEMA = """
create table if not exists news (
    id text primary key, created_at text, found_at text, tickers text, feed text,
    source text, title text, url text, rss_summary text);
create index if not exists news_found on news(found_at);
create table if not exists judged (
    id text primary key, title text, url text, source text, tickers text, created_at text,
    score int, reason text, topic text, say text,
    title_ko text,            -- 영어 제목의 번역. null 이면 아직 번역 전 (옛 기록)
    summary_ko text,          -- 한국어 요약
    summary_by text,          -- 요약한 쪽: rss(야후 설명을 판별 때 줄임) / ollama
    summary_state text,       -- null 요약 전, done, skip:<까닭>
    alerted int, late int, by text, at text,
    published_real text,      -- 원문 페이지에 적힌 처음 나온 시각 (구글 기사만, 확인한 것만)
    stale int);               -- 원문 날짜가 오래된 옛 기사 (구글이 새 날짜를 붙여 다시 올린 것)
create index if not exists judged_at on judged(at);
create table if not exists feedback (
    n integer primary key autoincrement, id text, title text, "like" int, strong int, at text);
create table if not exists meta (k text primary key, v text);
create table if not exists mb_ratings (       -- MarketBeat 목표가 변경 (확장이 사용자 탭에서 읽어 보낸 것)
    id text primary key,                       -- MarketBeat 의 변경 번호 (details/<id>)
    first_seen text, last_seen text,           -- 처음·마지막으로 표에서 본 시각 (UTC)
    baseline int,                              -- 1 이면 측정을 시작할 때 이미 있던 줄 (속도 재기에서 뺀다)
    ticker text, company text, action text, brokerage text, analyst text, price text,
    pt_old text, pt_new text, rating_old text, rating_new text, page_refreshed text);
"""

_local = threading.local()
_write = threading.Lock()


def con() -> sqlite3.Connection:
    c = getattr(_local, "con", None)
    if c is None:
        DB.parent.mkdir(exist_ok=True)
        c = sqlite3.connect(DB, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("pragma journal_mode=wal")
        c.executescript(SCHEMA)
        # 먼저 만든 DB 에는 없는 칸을 더한다
        have = {r[1] for r in c.execute("pragma table_info(judged)")}
        for col, typ in (("published_real", "text"), ("stale", "int")):
            if col not in have:
                c.execute(f"alter table judged add column {col} {typ}")
        c.commit()
        _local.con = c
    return c


def _commit(sql: str, args=(), many=False) -> int:
    with _write:
        c = con()
        cur = c.executemany(sql, args) if many else c.execute(sql, args)
        c.commit()
        return cur.rowcount


def get_meta(k: str, default: str = "") -> str:
    row = con().execute("select v from meta where k=?", (k,)).fetchone()
    return row[0] if row else default


def set_meta(k: str, v: str):
    _commit("insert or replace into meta values (?, ?)", (k, v))


# ─────────────────────────────────────────────────────────────
# news
# ─────────────────────────────────────────────────────────────

def add_news(rows: list) -> int:
    """새 뉴스를 넣는다. 이미 있는 id 는 건너뛴다. 넣은 수를 돌려준다."""
    before = con().execute("select count(*) from news").fetchone()[0]
    _commit(f"insert or ignore into news values ({','.join('?' * len(NEWS_COLS))})",
            [tuple(r.get(k, "") for k in NEWS_COLS) for r in rows], many=True)
    return con().execute("select count(*) from news").fetchone()[0] - before


def read_news(days: int = 2) -> list:
    """최근 며칠 안에 받은 뉴스. 옛 코드와 맞게 rss_summary 는 summary 로도 넣어 준다."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    out = []
    for r in con().execute("select * from news where found_at >= ? order by found_at", (since,)):
        d = dict(r)
        d["summary"] = d.get("rss_summary") or ""
        out.append(d)
    return out


def known_ids(days: int = 3) -> set:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    return {r[0] for r in con().execute("select id from news where found_at >= ?", (since,))}


def recent_titles(days: int = 2) -> list:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    return [r[0] for r in con().execute("select title from news where found_at >= ?", (since,))]


def rss_summary(nid: str) -> str:
    r = con().execute("select rss_summary from news where id=?", (nid,)).fetchone()
    return (r[0] or "") if r else ""


# ─────────────────────────────────────────────────────────────
# judged
# ─────────────────────────────────────────────────────────────

def _judged_dict(r: sqlite3.Row) -> dict:
    d = {k: r[k] for k in r.keys()}
    for k in BOOL_COLS:
        d[k] = bool(d.get(k))
    # 옛 jsonl 에는 title_ko 칸 자체가 없었다. 번역을 다시 할지 가르려고 null 은 칸을 뺀다
    for k in ("title_ko", "summary_ko", "summary_by", "summary_state", "published_real"):
        if d.get(k) is None:
            d.pop(k, None)
    return d


def load_judged() -> dict:
    return {r["id"]: _judged_dict(r) for r in con().execute("select * from judged")}


def save_judged(rec: dict):
    row = tuple(int(bool(rec.get(k))) if k in BOOL_COLS else rec.get(k) for k in JUDGED_COLS)
    _commit(f"insert or replace into judged values ({','.join('?' * len(JUDGED_COLS))})", row)


def summary_todo(min_score: int, hours: int = 48) -> list:
    """요약할 판별 기록: 목록에 보이는 점수, 아직 요약 전, 새것부터. 뉴스의 feed·rss_summary 도 붙인다."""
    since = (datetime.now(timezone(timedelta(hours=9))) - timedelta(hours=hours)).isoformat(timespec="seconds")
    cur = con().execute(
        "select j.*, n.feed as feed, n.rss_summary as rss_summary from judged j left join news n on n.id = j.id "
        "where j.summary_state is null and j.score > ? and j.at >= ? order by j.created_at desc",
        (min_score, since))
    out = []
    for r in cur:
        d = _judged_dict(r)
        d["feed"], d["rss_summary"] = r["feed"], r["rss_summary"]
        out.append(d)
    return out


# ─────────────────────────────────────────────────────────────
# feedback
# ─────────────────────────────────────────────────────────────

def add_feedback(rec: dict):
    like = rec.get("like")
    _commit('insert into feedback (id, title, "like", strong, at) values (?,?,?,?,?)',
            (rec["id"], rec.get("title", ""), None if like is None else int(bool(like)),
             int(bool(rec.get("strong"))), rec.get("at", "")))


def latest_feedback() -> dict:
    fb = {}
    for r in con().execute('select id, title, "like", strong, at from feedback order by n'):
        fb[r["id"]] = {"id": r["id"], "title": r["title"], "like": None if r["like"] is None else bool(r["like"]),
                       "strong": bool(r["strong"]), "at": r["at"]}
    return fb


# ─────────────────────────────────────────────────────────────
# MarketBeat 목표가 변경 (속도 재기)
# ─────────────────────────────────────────────────────────────

MB_COLS = ["ticker", "company", "action", "brokerage", "analyst", "price",
           "pt_old", "pt_new", "rating_old", "rating_new"]


def add_mb(rows: list, refreshed: str) -> int:
    """표의 줄을 넣는다. 처음 본 줄은 first_seen 을 지금으로, 이미 있던 줄은 last_seen 만 바꾼다.
    표를 처음 받을 때(아직 아무 줄도 없을 때) 들어온 줄은 baseline 으로 표시한다. 새 줄 수를 돌려준다."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    c = con()
    baseline = int(c.execute("select count(*) from mb_ratings").fetchone()[0] == 0)
    have = {r[0] for r in c.execute("select id from mb_ratings")}
    new = [r for r in rows if str(r.get("id")) not in have]
    with _write:
        c.executemany(
            f"insert into mb_ratings (id, first_seen, last_seen, baseline, {', '.join(MB_COLS)}, page_refreshed) "
            f"values (?, ?, ?, ?, {', '.join('?' * len(MB_COLS))}, ?)",
            [(str(r["id"]), now, now, baseline, *(str(r.get(k, ""))[:120] for k in MB_COLS), refreshed[:80])
             for r in new])
        c.executemany("update mb_ratings set last_seen=? where id=?", [(now, str(r["id"])) for r in rows])
        c.commit()
    return len(new)


# ─────────────────────────────────────────────────────────────
# 처음부터 다시: 표를 지우지 않고 이름을 바꿔 남긴다 (되살리려면 이름을 원래대로)
# ─────────────────────────────────────────────────────────────

def reset(what: str) -> list:
    stamp = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d_%H%M%S")
    tables = ["feedback"] + (["judged"] if what == "all" else [])
    moved = []
    with _write:
        c = con()
        for t in tables:
            n = c.execute(f"select count(*) from {t}").fetchone()[0]
            if not n:
                continue
            c.execute(f"alter table {t} rename to {t}_bak_{stamp}")
            moved.append(f"{t}_bak_{stamp} ({n}건)")
        c.executescript(SCHEMA)
        c.commit()
    return moved


# ─────────────────────────────────────────────────────────────
# 옛 기록 옮기기: data/stocknews_*.csv, news_judged.jsonl, news_feedback.jsonl → DB (한 번만, 옛 파일은 그대로 둔다)
# ─────────────────────────────────────────────────────────────

def _jsonl(path: Path) -> list:
    out = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def migrate(log=print):
    c = con()
    if c.execute("select v from meta where k='migrated'").fetchone():
        return
    news = []
    for path in sorted((BASE / "data").glob("stocknews_*.csv")):
        with path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                r["rss_summary"] = r.get("summary", "")
                news.append(r)
    n_news = add_news(news)
    judged = {}
    for rec in _jsonl(BASE / "news_judged.jsonl"):   # 같은 id 는 마지막 줄이 이긴다
        judged[rec["id"]] = rec
    for rec in judged.values():
        if rec.get("summary_ko"):
            rec.setdefault("summary_state", "done")
        save_judged(rec)
    fbs = _jsonl(BASE / "news_feedback.jsonl")
    for rec in fbs:
        add_feedback(rec)
    _commit("insert or replace into meta values ('migrated', ?)", (datetime.now().isoformat(timespec="seconds"),))
    log(f"옛 기록을 DB 로 옮김: 뉴스 {n_news}건, 판별 {len(judged)}건, 반응 {len(fbs)}건 (옛 파일은 그대로 둔다)")
