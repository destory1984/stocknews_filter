"""원문 페이지에서 처음 나온 시각 읽기. 예는 날짜를 못 읽어 옛 기사인지 확인하지 못한 채 알림이 나간 곳들이다 (10-03)."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import article   # noqa: E402


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_json_ld_and_meta_as_before():
    assert article.published('{"@type":"NewsArticle","datePublished":"2026-10-02T21:48:10Z"}') == utc(2026, 10, 2, 21, 48, 10)
    assert article.published('<meta property="article:published_time" content="2026-10-03T19:01:10+0900">') == utc(2026, 10, 3, 10, 1, 10)
    assert article.published('<meta content="2026-10-03T10:13:52+00:00" property="article:published_time">') == utc(2026, 10, 3, 10, 13, 52)
    assert article.published("<p>날짜 없는 글</p>") is None


def test_daum_compact_regdate_is_kst():
    # 다음 뉴스는 og:regDate 에 숫자 14자리(한국 시각)로 적는다. 알림 5번 모두 날짜를 못 읽었다
    assert article.published('<meta property="og:regDate" content="20261003190110">') == utc(2026, 10, 3, 10, 1, 10)


def test_single_quotes_and_other_meta_names():
    assert article.published("<meta name='parsely-pub-date' content='2026-10-01T12:00:00Z'>") == utc(2026, 10, 1, 12)
    assert article.published('<meta name="date" content="2026-10-01 21:00:00">') == utc(2026, 10, 1, 12)     # 시간대가 없으면 KST
    assert article.published('<meta name="pubdate" content="Thu, 01 Oct 2026 12:00:00 GMT">') == utc(2026, 10, 1, 12)
    assert article.published('<meta name="DC.date.issued" content="2026-10-01">') == utc(2026, 9, 30, 15)


def test_json_ld_fallbacks():
    assert article.published('{"@type":"VideoObject","uploadDate":"2026-10-01T12:00:00Z"}') == utc(2026, 10, 1, 12)
    assert article.published('{"dateCreated":"2026-10-01T12:00:00Z","dateModified":"2026-10-03T12:00:00Z"}') == utc(2026, 10, 1, 12)
    # 고친 시각만 있으면 처음 나온 때를 모르는 것이다. 옛 기사를 새 기사로 볼 수 있어 쓰지 않는다
    assert article.published('{"dateModified":"2026-10-03T12:00:00Z"}') is None
    assert article.published('<meta property="article:modified_time" content="2026-10-03T12:00:00Z">') is None


def test_bad_values_are_skipped():
    assert article.published('<meta name="date" content="어제"><meta property="og:regDate" content="20261003190110">') == utc(2026, 10, 3, 10, 1, 10)
    assert article.published('<meta name="date" content="20269999999999">') is None
