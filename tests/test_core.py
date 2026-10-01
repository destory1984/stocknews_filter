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
