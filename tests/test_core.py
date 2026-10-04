"""모델도 네트워크도 안 쓰는 함수들의 시험. `python -m pytest tests -q` 로 돌린다.

여기 든 예는 모두 실제로 틀렸던 일에서 왔다 (괄호 안 날짜). 규칙을 고칠 때 옛 고장이 되살아나지 않는지 본다.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stock_alert as a   # noqa: E402
import stocknews          # noqa: E402
import targets            # noqa: E402
import targets_db         # noqa: E402

NAMES = ["삼성전자", "samsung electronics", "sk하이닉스", "하이닉스", "hynix", "nvidia", "nvda", "tesla", "tsla"]


# ── 같은 사건 (알림을 12시간에 한 번만) ──────────────────────────
def test_same_event_joins_renamed_topics():
    # 모델은 같은 일에 이름을 조금씩 달리 붙인다 (09-27)
    assert a.same_event("마이크론 실적 발표", "Micron", "마이크론 4분기 실적", "Micron")
    assert a.same_event("마이크론 실적 발표", "Micron", "마이크론 실적 발표", "Micron")


def test_same_event_needs_a_shared_stock():
    assert not a.same_event("마이크론 실적 발표", "Micron", "인텔 실적 발표", "Intel")
    assert not a.same_event("", "Micron", "마이크론 실적", "Micron")


# ── 여러 곳 보도 (곳 수 세기는 same_event 보다 좁다) ────────────────
def test_buzz_ignores_broad_words():
    # "삼성전자 AI 메모리" 50곳 가운데 48곳이 "삼성전자 AI 구독" 이었다 (09-28)
    assert a.same_event("삼성전자 AI 메모리", "삼성전자", "삼성전자 AI 구독", "삼성전자")
    assert not a.buzz_event("삼성전자 AI 메모리", "삼성전자", "삼성전자 AI 구독", "삼성전자", NAMES)
    assert a.buzz_event("삼성전자 AI 메모리", "삼성전자", "삼성전자 메모리 투자", "삼성전자", NAMES)


def test_buzz_skips_topics_made_of_broad_words_only():
    # "엔비디아 주가", "삼성 하이닉스 시세" 는 서로 다른 기사를 한데 묶는다 (09-29)
    assert not a.buzz_event("엔비디아 주가", "NVIDIA", "엔비디아 주가", "NVIDIA", NAMES)
    assert not a.buzz_event("삼성 하이닉스 시세", "삼성전자", "하이닉스 시세", "삼성전자", NAMES)


def test_buzz_keeps_a_real_event():
    assert a.buzz_event("스페이스X 발사", "SpaceX", "스페이스X 위성 발사", "SpaceX", NAMES)


# ── 알림 종류 (/react) ──────────────────────────────────────
def test_alert_kind():
    assert a.alert_kind({"title": "Baird Raises Micron Price Target to $1,520", "reason": ""}) == "목표가·의견"
    assert a.alert_kind({"title": "x", "title_ko": "마이크론 4분기 매출 급증", "reason": ""}) == "실적"
    assert a.alert_kind({"title": "x", "reason": "9곳 보도 · 스타십 발사"}) == "여러 곳 보도"
    assert a.alert_kind({"title": "Something else entirely", "reason": ""}) == "그 밖"


# ── 이미 알린 소식 (판별 프롬프트에 넣는 줄) ────────────────────
def test_known_lines_picks_recent_alerts_of_the_same_stock():
    # 09-28 "엔비디아 자사주 1,500억 달러 승인" 을 10-01 에 "주가 2배 전망" 글로 또 9점에 알렸다 (10-02)
    kst = timezone(timedelta(hours=9))
    now = datetime(2026, 10, 1, 21, 0, tzinfo=kst)

    def rec(i, hours_ago, stock, title, alerted=True):
        return {"id": i, "at": (now - timedelta(hours=hours_ago)).isoformat(timespec="seconds"),
                "tickers": stock, "title": title, "title_ko": "", "alerted": alerted}
    recs = [rec("a", 70, "NVIDIA", "엔비디아, 자사주 매입 1500억 달러 승인"), rec("b", 5, "NVIDIA, Intel", "새 소식"),
            rec("c", 100, "NVIDIA", "나흘 전 알림"), rec("d", 3, "NVIDIA", "알리지 않은 뉴스", alerted=False),
            rec("e", 2, "Micron", "다른 종목"), rec("f", -1, "NVIDIA", "판별 시각 뒤의 알림")]
    batch = [{"id": "x", "title": "예측: 엔비디아 주가 2배", "tickers": "NVIDIA"}]
    lines = a.known_lines(recs, batch, now, 3)
    assert lines == ["10-01 NVIDIA, Intel · 새 소식", "09-28 NVIDIA · 엔비디아, 자사주 매입 1500억 달러 승인"]
    assert a.known_lines(recs, batch, now, 0) == []                              # 0 이면 끈다
    assert a.known_lines(recs, [dict(batch[0], id="a")], now, 3) == lines[:1]   # 다시 판별하는 뉴스 자신은 뺀다


# ── 언론사 이름 갈래 ────────────────────────────────────────
def test_source_key_folds_name_variants():
    assert stocknews.source_key("MarketBeat") == stocknews.source_key("marketbeat.com")
    assert stocknews.source_key("Investing.com India") == stocknews.source_key("Investing.com")
    assert stocknews.source_key("") == ""


def test_source_muted():
    # 가린 Kalkine Media 기사가 8점을 받아 알림이 나갔다. 눌러도 Cloudflare 에 막혀 못 읽는 곳이다 (10-02)
    cfg = {"hide_sources": ["Kalkine Media"]}
    assert a.source_muted(cfg, "Kalkine Media")
    assert not a.source_muted(cfg, "Reuters")
    assert not a.source_muted(cfg, "")
    assert not a.source_muted({}, "Kalkine Media")


# ── 제목에 적힌 옛 날짜 ──────────────────────────────────────
def test_old_by_title():
    ref = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    assert stocknews.old_by_title("Why Is Micron Stock Up Pre-Market Today, Sept. 17?", ref) == "제목 날짜 09-17"
    assert stocknews.old_by_title("Micron Reports Sept. 26", ref) == ""      # 사흘 안
    assert stocknews.old_by_title("No date here", ref) == ""


# ── 목표가: 증권사 이름 ─────────────────────────────────────
def test_broker_key_merges_names():
    same = [("JPMorgan", "J.P. Morgan"), ("JPMorgan", "JPMorgan Chase & Co."), ("Citi", "Citigroup"),
            ("RBC Capital", "Royal Bank Of Canada"), ("Bernstein", "Sanford C. Bernstein"), ("Bernstein", "번스타인"),
            ("BNP Paribas", "BNP파리바스"), ("Mizuho", "Mizuho Securities"), ("DS투자증권", "DS투자證"),
            ("Bank of America", "BofA"), ("Deutsche Bank", "도이치방크")]
    for x, y in same:
        assert targets.broker_key(x) == targets.broker_key(y), (x, y)
    assert targets.broker_key("Baird") != targets.broker_key("Bernstein")


# ── 목표가: 모델 답 읽기 ─────────────────────────────────────
def test_targets_parse_keeps_only_watchlist_and_known_actions():
    recs = [{"id": "a", "title": "t1"}, {"id": "b", "title": "t2"}, {"id": "c", "title": "t3"}]
    text = """{"results": [
      {"i": 1, "items": [{"stock": "micron", "broker": "Baird", "action": "상향", "rating": "매수", "old": "1,280", "new": 1520, "cur": "usd"}]},
      {"i": 2, "items": [{"stock": "Apple", "broker": "Baird", "action": "상향"}, {"stock": "Micron", "broker": "", "action": "상향"},
                         {"stock": "Micron", "broker": "UBS", "action": "올림"}]},
      {"i": 9, "items": []}]}"""
    got = targets.parse(text, recs, ["Micron", "Tesla"])
    assert got["a"] == [{"stock": "Micron", "broker": "Baird", "action": "상향", "rating": "매수",
                         "pt_old": 1280.0, "pt_new": 1520.0, "currency": "USD"}]
    assert got["b"] == []          # 관찰 종목 아님, 증권사 없음, 모르는 구분은 버린다
    assert "c" not in got          # 모델이 빠뜨린 뉴스는 결과에 없다 (다시 묻는다)
    assert targets.parse("이건 JSON 이 아니다", recs, ["Micron"]) == {}


def test_targets_candidate():
    assert targets.is_candidate({"title": "Baird Raises Micron Price Target to $1,520"})
    assert targets.is_candidate({"title": "x", "title_ko": "하나증권, 삼성전자 목표가 48만원"})
    assert not targets.is_candidate({"title": "Micron Price Target", "by": "rule"})   # 규칙으로 0점 매긴 것
    assert not targets.is_candidate({"title": "Micron opens a new fab"})


# ── 목표가: 한 줄로 합치기 ────────────────────────────────────
def _row(broker, action, pt, hours_ago, stock="Micron"):
    at = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc) - timedelta(hours=hours_ago)
    return {"stock": stock, "broker": broker, "action": action, "rating": "", "pt_old": None, "pt_new": pt,
            "currency": "USD" if pt else "", "at": at}


def test_group_merges_same_action_and_votes():
    # "Baird Sets $1,520 Target" 이 "신규"·"제시" 로 붙어도 다른 기사들이 "상향" 이면 상향 (09-29, 09-30)
    rows = [_row("Baird", "제시", 1520, 0), _row("Baird", "상향", None, 1), _row("Baird", "상향", 1520, 2),
            _row("Baird", "신규", 1520, 3)]
    g = targets.group(rows)
    assert len(g) == 1 and len(g[0]["news"]) == 4
    assert g[0]["action"] == "상향" and g[0]["pt_new"] == 1520


def test_group_keeps_apart_different_targets_brokers_and_old_news():
    rows = [_row("Baird", "상향", 1520, 0), _row("Baird", "상향", 1400, 1), _row("J.P. Morgan", "유지", 1540, 2),
            _row("JPMorgan", "유지", None, 3), _row("Baird", "상향", 1520, 24 * 5)]
    g = targets.group(rows)
    assert [(x["broker"], x["pt_new"], len(x["news"])) for x in g] == [
        ("Baird", 1520, 1), ("Baird", 1400, 1), ("J.P. Morgan", 1540, 2), ("Baird", 1520, 1)]


def test_group_only_unknown_direction_stays_unknown():
    assert targets.group([_row("유진증권", "제시", 560000, 0, "삼성전자")])[0]["action"] == "제시"


def test_latest_per_broker_keeps_only_the_newest_line():
    # 10-04: 종목별 표의 Micron 에 Goldman Sachs 가 둘 ($1,250 과 하루 앞선 $1,100), JPMorgan 은 이름 갈래까지 달랐다
    rows = [_row("Goldman Sachs", "유지", 1250, 0), _row("JP Morgan", "유지", None, 5), _row("Goldman Sachs", "유지", 1100, 20),
            _row("JPMorgan", "상향", 1540, 24 * 3)]
    g = targets.latest_per_broker(targets.group(rows))
    # 새 줄에 목표가가 없으면 앞선 줄의 것을 적는다
    assert [(x["broker"], x["action"], x["pt_new"]) for x in g] == [("Goldman Sachs", "유지", 1250), ("JP Morgan", "유지", 1540)]
    # 새 줄이 상향·하향이면 앞선 목표가는 바뀌기 전 값이라 적지 않는다
    g = targets.latest_per_broker(targets.group([_row("Baird", "상향", None, 0), _row("Baird", "유지", 1280, 24 * 3)]))
    assert [(x["action"], x["pt_new"]) for x in g] == [("상향", None)]
    assert targets.broker_key("Goldman") == targets.broker_key("Goldman Sachs")
    assert targets.broker_key("Melius Research") == targets.broker_key("Melius")


def test_consensus_counts_each_broker_once():
    # Goldman Sachs 는 새 목표가($1,250)만 센다. 목표가 없는 줄은 빠진다
    rows = [_row("Goldman Sachs", "상향", 1250, 0), _row("Baird", "유지", 1520, 5), _row("Goldman Sachs", "유지", 1100, 60),
            _row("Melius", "유지", None, 70)]
    c = targets.consensus(targets.group(rows))
    assert (c["n"], c["avg"], c["low"], c["high"], c["currency"]) == (2, 1385, 1250, 1520, "USD")
    assert targets.consensus(targets.group([_row("Melius", "유지", None, 0)])) is None
    assert round(targets.gap_pct(1385, 1069.15), 1) == 29.5 and targets.gap_pct(1385, None) is None


# ── 목표가 공용 DB ─────────────────────────────────────────
def test_ticker_key():
    assert targets_db.ticker_key("005930.KS") == "005930"
    assert targets_db.ticker_key("$mu") == "MU"
    assert targets_db.ticker_key(None) == ""


def test_shared_db_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(targets_db, "PATH", tmp_path / "t.db")
    monkeypatch.setattr(targets_db, "_local", type(targets_db._local)())   # 새 연결
    rec = {"id": "n1", "title": "Baird Raises Micron Price Target", "title_ko": "베어드, 마이크론 목표가 상향",
           "url": "http://example.com/1", "source": "Example", "created_at": "2026-09-30T01:00:00+00:00", "alerted": True}
    item = {"stock": "Micron", "ticker": "MU", "broker": "Baird", "action": "상향", "rating": "매수",
            "pt_old": 1280.0, "pt_new": 1520.0, "currency": "USD"}
    targets_db.save("stocknews", rec, [item])
    targets_db.save("saveticker", dict(rec, id="n2"), [])          # 목표가 뉴스 아님
    targets_db.save("stocknews", rec, [item, dict(item, broker="UBS")])   # 다시 뽑으면 갈아 끼운다
    assert targets_db.checked("stocknews") == {"n1"} and targets_db.checked("saveticker") == {"n2"}
    rows = targets_db.read("2026-09-01")
    assert sorted((r["origin"], r["broker"], r["ticker"], r["alerted"]) for r in rows) == [
        ("stocknews", "Baird", "MU", 1), ("stocknews", "UBS", "MU", 1)]
    assert targets_db.read("2026-10-01") == []
    targets_db.con().close()


# ── 수집 실패 (10-04: 정각마다 구글이 거절했는데 번호가 없어 까닭을 몰랐다) ──────
def test_collect_logs_http_status(monkeypatch):
    import urllib.error

    def refuse(stock, days, when=None):
        raise urllib.error.HTTPError("https://example.invalid/rss", 429, "Too Many Requests", None, None)

    def offline(stock, days, when=None):
        raise urllib.error.URLError("no network")
    said = []
    monkeypatch.setattr(stocknews, "google_news", refuse)
    stocknews.collect([{"name": "Micron"}], 1, ["google"], on_error=said.append)
    monkeypatch.setattr(stocknews, "google_news", offline)
    stocknews.collect([{"name": "Micron"}], 1, ["google"], on_error=said.append)
    assert said == ["[Micron] google failed: HTTPError 429", "[Micron] google failed: URLError"]


def _fetch_with(monkeypatch, errors):
    """collect 가 errors 를 알리고 빈손으로 돌아올 때 fetch_news 를 한 번 돌린 뒤, 다음 차례가 넓게 묻는지."""
    asked = []

    def collect(stocks, days, sources, on_error=None, google_when=None):
        asked.append(google_when)
        for m in errors:
            on_error(m)
        return []
    monkeypatch.setattr(stocknews, "load_watchlist", lambda: [{"name": "Micron"}])
    monkeypatch.setattr(stocknews, "collect", collect)
    monkeypatch.setattr(a.store, "known_ids", lambda: set())
    monkeypatch.setattr(a.store, "recent_titles", lambda: [])
    monkeypatch.setattr(a, "log", lambda *x: None)
    monkeypatch.setattr(a, "_last_wide", 0.0)
    cfg = {"wide_every_min": 30, "lookback_days": 1, "fresh_hours": 1}
    a.fetch_news(cfg)
    a.fetch_news(cfg)
    return asked


def test_wide_fetch_is_retried_when_the_network_was_down(monkeypatch):
    # 잠에서 깬 직후 하루치 수집이 통째로 실패하고도 "했다"로 쳐서 30분 뒤에야 다시 물었다 (10-04)
    assert _fetch_with(monkeypatch, ["[Micron] google failed: URLError"]) == [None, None]
    assert _fetch_with(monkeypatch, ["[Micron] google failed: TimeoutError"]) == [None, None]


def test_wide_fetch_is_not_retried_when_google_refused_or_all_went_well(monkeypatch):
    # 거절당한 직후 또 넓게 물으면 더 막힌다. 야후만 실패한 것도 넓은 수집과 상관없다
    assert _fetch_with(monkeypatch, ["[Micron] google failed: HTTPError 429"]) == [None, "1h"]
    assert _fetch_with(monkeypatch, ["[Micron] yahoo failed: URLError"]) == [None, "1h"]
    assert _fetch_with(monkeypatch, []) == [None, "1h"]
