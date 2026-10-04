"""
종목 뉴스 필터 (saveticker 필터링의 news_alert.py 를 가져와 뉴스 출처만 바꾼 것)

  fetch_min 분마다 watchlist.json 의 종목 뉴스를 구글 뉴스·야후 파이낸스 RSS 에서 받아
  data/stocknews_날짜.csv 에 쌓는다 (stocknews.py 의 키워드 거름망을 먼저 거친다).
  새 뉴스는 LLM(ollama, 안 되면 claude CLI)이 interests.md 와 과거 👍/👎 반응을 읽고
  0~10점으로 판별한다. 기준 점수 이상이면 윈도우 토스트와 음성으로 알린다.

  토스트의 [👍 관심] [👎 별로] 버튼은 http://127.0.0.1:18766 로 반응을 기록하고,
  기록은 다음 판별의 예시로 들어간다. 같은 주소에서 최근 판별 목록도 볼 수 있다.

실행:
  python stock_alert.py            # 감시 시작
  python stock_alert.py --test 15  # 최근 15건만 판별해 점수를 출력 (알림 없음)
  python stock_alert.py --say "삼성전자 목표가 상향"   # 음성 알림 시험

필요:
  pip install requests winotify edge-tts pywin32
  claude CLI 로그인 (터미널에서 claude 실행 후 /login 한 번)
  stock_alert_config.json 의 backend: "auto" 는 ollama 가 켜져 있으면 먼저 쓰고,
  꺼져 있거나 오류·엉뚱한 답이면 claude 로 넘긴다
"""
import argparse
import csv
import difflib
import hashlib
import html
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import requests

import article
import earnings
import moves
import settings
import stocknews
import store
import targets
import targets_db
import weekly

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
CONFIG = BASE / "stock_alert_config.json"
INTERESTS = BASE / "interests.md"
# 뉴스·판별·반응 기록은 data/stocknews.db (store.py). 옛 news_judged.jsonl·CSV 는 처음 켤 때 옮겨 온다

KST = timezone(timedelta(hours=9))

DEFAULTS = {
    "backend": "auto",             # "auto" (ollama 먼저, 안 되면 claude), "claude", "ollama"
    "claude_model": "sonnet",
    "ollama_url": "http://localhost:11434/api/generate",
    "model": "qwen3.8:27b",        # rsi_vision 이 이미 올려 둔 모델을 같이 쓴다
    "keep_alive": -1,
    "timeout_sec": 180,
    "ollama_timeout_sec": 90,      # auto 에서 ollama 를 이만큼 기다려 보고 안 되면 claude 로
    "threshold": 7,                # 이 점수 이상이면 알린다
    "fetch_min": 5,                # 구글 뉴스·야후 RSS 를 이 간격(분)으로 받는다
    "lookback_days": 1,            # RSS 에서 며칠 전 뉴스까지 받을지
    # 구글은 관련도 순으로 수십 건만 주어서 하루치로 물으면 방금 나온 기사가 빠진다 (09-28: 1시간 안 25건 중 17건).
    # 그래서 평소에는 fresh_hours 시간치만 묻고, 하루치(lookback_days)는 wide_every_min 분마다 한 번 묻는다
    "fresh_hours": 1,
    "wide_every_min": 30,
    # 이보다 오래된 뉴스는 알리지 않는다 (판별은 한다).
    # 구글 뉴스는 기사가 나온 뒤 늦게 잡히기도 해서 saveticker(60분)보다 길게 둔다.
    "max_age_min": 120,
    # 같은 사건은 이 시간 안에 한 번만 알린다. 같은 종목이고 사건 이름의 낱말(회사 이름 뒤)이 겹치면 같은 사건으로 본다.
    # 1시간·같은 이름일 때 09-26 하루 35번 (마이크론 실적 예고만 10번 가까이) → 12시간·낱말 겹침으로 되돌려 보니 21번
    "topic_hours": 12,
    # 판별할 때 지난 known_days 일 동안 알린 뉴스(같은 종목 것)를 함께 보여 준다. 며칠 전 일을 새 사실 없이 다시 쓴 글을
    # 모델이 낮게 매기게 하려는 것이다 (10-02: 09-28 엔비디아 1,500억 달러 자사주 승인을 10-01 에 9점으로 또 알렸다). 0 이면 끔.
    "known_days": 3,
    # 기준 점수에 못 미쳐도 6시간 안에 언론사 buzz_sources 곳 넘게 쓴 사건이면 알린다 (buzz_min_score 점 이상만, 0 이면 끔).
    # 09-27 까지 기록으로 되돌려 보면 하루 한 번쯤 (높은 점수 사건은 이미 알림이 나가서)
    "buzz_sources": 4,
    "buzz_min_score": 6,
    "catchup_hours": 12,           # 절전·재시작으로 밀린 뉴스는 이만큼까지 거슬러 판별해 목록에만 올린다
    "batch": 10,                   # 한 번에 묻는 뉴스 수
    "poll_sec": 10,
    "max_wait_sec": 90,            # 뉴스를 모아서 한 번에 묻는다. batch 가 차거나 가장 오래 기다린 뉴스가 이만큼 되면 묻는다
    "port": 18766,                 # 18765 는 saveticker 필터링
    "examples": 15,                # 프롬프트에 넣을 👍, 👎 각각의 최대 개수
    "dup_ratio": 0.6,              # 최근 알린 제목과 이만큼 비슷하면 알리지 않는다
    # 급등락: 15분 사이 이만큼(%) 움직이면 알린다. 3배 ETF 는 따로. 같은 종목은 move_cooldown_min 에 한 번
    "earnings_alert": True,        # 회사가 확정한 실적 발표를 earnings_lead_hours 전에 한 번 알린다 (추정 날짜는 안 알림)
    "earnings_lead_hours": 8,      # 새벽 05:00 발표면 전날 21:00, 저녁 21:00 발표면 그날 13:00
    "weekly_notice": "Sun 09:00",   # 이 요일·시각(한국)에 주간 리포트가 나왔다고 한 번 알린다. 비우면 안 알림
    "move_alerts": True,
    "move_pct": 3.0,
    "move_pct_3x": 5.0,
    "move_3x": ["SOXL", "KORU"],
    "move_cooldown_min": 60,
    "hide_sources": [],            # 판별 목록에서 가리고 알림도 내지 않을 언론사 (언론사 성적표에서 고른다). 판별은 그대로 한다
    "hide_max_score": 3,           # 판별 목록에서 이 점수 이하는 기본으로 숨긴다 (👍·🔔10 준 것은 보인다)
    "tts": True,                   # 알림을 말로도 읽는다: 말머리 소리 → "언론사, 제목" (영어 제목은 번역한 것)
    "tts_voice": "ko-KR-SunHiNeural",   # Edge 읽어주기 음성. 안 되면 윈도우 기본 음성(SAPI)
    "tts_voice_en": "",            # 영어 언론사 이름("The Motley Fool, …")을 읽을 영어 음성 (예: en-US-JennyNeural). 비우면 한국어 음성이 다 읽는다
    "tts_rate": "+0%",
    "tts_volume": 100,             # 목소리 크기(%). 말머리 소리는 그대로다
    "tts_chime": r"C:\Windows\Media\Windows Notify Email.wav",   # saveticker(Messaging)·RSI 와 다른 소리
    "quiet_on": False,             # 조용한 시각을 쓸지
    "tts_quiet": "23:00-07:00",    # 조용한 시각: 말하지 않는다 (토스트·텔레그램은 소리 없이 그대로)
    "toast": True,                 # 윈도우 토스트
    "telegram": False,             # 텔레그램으로도 보낸다 (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수)
    # 구글 기사 요약: 원문을 받아 Ollama 로만 요약한다 (Claude 는 쓰지 않는다). Ollama 가 꺼져 있으면
    # 판별 목록이 갱신될 때마다(15초) 켜졌는지 보고, 켜지면 밀린 것을 요약한다
    "summarize": True,
    "summary_hours": 48,           # 이 시간 안에 판별한 뉴스만 요약한다
    # 구글 링크 풀기 사이 간격. 자주 부르면 구글이 429 로 막는다. 30초 간격으로는 막힌 뒤 풀려도
    # 네 건 만에 다시 막혔다 (2026-09-26 19:09~19:14). 요약은 급하지 않으니 길게 띄운다
    "google_gap_sec": 180,
    "google_alert_gap_sec": 30,    # 알리기 전 원문 날짜 보기는 알림이 늦으면 안 되니 이만큼만 띄운다
    "google_block_min": 30,        # 막히면 이만큼 쉰다. 쉬고 나서도 막혀 있으면 두 배씩 (최대 네 배)
    # 구글 뉴스 RSS 는 옛 기사에 새 날짜를 붙여 다시 올리기도 한다 (6월 기사가 9월 날짜로 옴).
    # 알릴 만한 구글 기사는 알리기 전에 원문 날짜를 보고, 이보다 오래됐으면 알리지 않고 목록에서 숨긴다
    "stale_days": 3,
}

PROMPT = """너는 한 개인 투자자의 뉴스 비서다.
아래 [관심사]와 [과거 반응]을 보고, [새 뉴스] 각각이 이 사람에게 지금 알려줄 가치가 얼마나 되는지 0~10점으로 매겨라.

점수 기준:
- 9~10: 보유·관찰 종목이나 시장 전체를 당장 움직일 만한 소식
- 7~8: 관심 분야와 직접 관련 있고 알면 도움이 되는 소식
- 4~6: 간접적으로만 관련
- 0~3: 관련 없음, 이미 나온 내용의 반복, 사소한 소식
[과거 반응]에서 👍 받은 뉴스와 비슷하면 점수를 올리고, 👎 받은 뉴스와 비슷하면 내려라.
reason 과 con 은 점수와 상관없이 둘 다 쓴다. 점수가 높으면 reason 이 앞서고 낮으면 con 이 앞선다. 정말 없으면 빈 문자열.
[과거 반응]의 🔔 는 "이런 뉴스는 반드시 알려라", 🔕 는 "이런 뉴스는 절대 알리지 마라"는 강한 표시다.
🔔 와 같은 종류의 뉴스는 9~10점, 🔕 와 같은 종류는 0~1점을 줘라. 이것이 👍/👎 와 관심사보다 우선한다.

[관찰 종목] 이 사람이 고른 종목이다. 모두 관심 종목이고 모두 상장돼 거래된다 (괄호는 야후 티커).
"관심 종목 아님", "비상장사" 를 까닭으로 점수를 깎지 마라.
{stocks}

[관심사]
{interests}

[과거 반응]
{examples}

같은 사건 묶기:
- 뉴스마다 그 뉴스가 다루는 사건의 이름(topic)을 10자 안팎 한국어로 붙여라. 예: "미중 정상회담", "H&M 3분기 실적", "미 5년물 국채 입찰".
- 한 사건에서 나온 여러 발언, 후속 보도([2보] 등), 다른 매체의 같은 보도는 모두 같은 이름을 쓴다.
- [최근 사건 이름]에 같은 사건이 있으면 그 이름을 글자 그대로 다시 써라.

[최근 사건 이름]
{topics}

이미 알린 소식:
- [이미 알린 소식]은 지난 며칠 동안 이 사람에게 이미 알림을 보낸 뉴스다 (알린 날, 종목, 제목).
- [새 뉴스]가 그 가운데 하나와 같은 일을 다시 쓴 글이고 새 사실이 없으면 3점 이하를 주고 con 에 "이미 알린 소식" 이라고 써라.
  며칠 전 발표·결정을 돌아보거나 풀이·전망만 덧붙인 글, 지난 분기 실적을 다시 정리한 글이 여기에 든다.
- 새 사실이 있으면 깎지 마라. 새 숫자, 확정·무산, 새 당사자, 그 일로 오늘 주가가 크게 움직였다는 소식이 새 사실이다.
  다른 증권사가 낸 목표가·투자의견은 같은 종목이어도 새 소식이다. 같은 종목의 다른 일도 깎지 마라.

[이미 알린 소식]
{known}

[새 뉴스]
{news}

JSON 만 출력하라. 다른 말은 쓰지 마라.
{{"results": [{{"i": 번호, "score": 0~10 정수, "reason": "이 사람이 이 뉴스를 볼 까닭 12자 이내 한국어", "con": "이 사람이 이 뉴스를 안 볼 까닭 12자 이내 한국어", "topic": "사건 이름", "say": "제목을 소리내 읽기 좋게 12자 안팎으로 줄인 말. 예: 이란 휴전안 거부", "ko": "제목이 영어면 자연스러운 한국어 제목으로 번역, 한국어 제목이면 빈 문자열", "sum": "[설명] 이 있으면 그 내용을 한국어 1~2문장으로 요약, 없으면 빈 문자열"}}]}}
"""


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    else:
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return cfg


LOG = BASE / "stock_alert.log"


def log(msg: str):
    line = f"{datetime.now():%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────
# 뉴스 읽기
# ─────────────────────────────────────────────────────────────

def parse_ts(s: str):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


_last_wide = 0.0   # 구글에 lookback_days 치를 마지막으로 물은 시각. 켜자마자 한 번은 하루치로 묻는다


def fetch_news(cfg: dict) -> int:
    """구글 뉴스·야후 RSS 에서 종목 뉴스를 받아 DB 에 넣는다. 새로 넣은 건수를 돌려준다.
    id 는 링크의 해시. 두 종목에 함께 걸린 기사는 한 줄로 두고 tickers 에 둘 다 적는다.
    구글과 야후가 같은 기사를 다른 링크로 주는 일이 있어, 최근 이틀 안에 같은 제목이 있으면 넣지 않는다.
    구글은 평소 fresh_hours 시간치만, wide_every_min 분마다 한 번 lookback_days 치를 묻는다."""
    global _last_wide
    try:
        stocks = stocknews.load_watchlist()
    except SystemExit as e:
        log(str(e))
        return 0
    wide = time.time() - _last_wide >= cfg["wide_every_min"] * 60
    failed = []
    rows = stocknews.collect(stocks, cfg["lookback_days"], ["google", "yahoo"],
                             on_error=lambda m: (failed.append(m), log(f"수집 실패 {m}")),
                             google_when=None if wide else f"{cfg['fresh_hours']}h")
    # 잠에서 깬 직후처럼 네트워크가 끊겨 구글을 못 물은 때는 넓은 수집을 한 것으로 치지 않고 다음 차례에 다시 묻는다.
    # 구글이 거절한(HTTPError) 때는 곧바로 또 넓게 물으면 더 막히니 그대로 넘어간다
    if wide and not any("google failed" in m and "HTTPError" not in m for m in failed):
        _last_wide = time.time()
    known = store.known_ids()
    titles = {stocknews.norm_title(t) for t in store.recent_titles()}
    now = datetime.now(timezone.utc)
    new = {}
    for r in rows:
        rid = hashlib.md5(r["link"].encode()).hexdigest()[:16]
        if rid in known:
            continue
        if rid in new:
            if r["stock"] not in new[rid]["tickers"]:
                new[rid]["tickers"] += f", {r['stock']}"
            continue
        key = stocknews.norm_title(r["title"])
        if key in titles:
            continue
        titles.add(key)
        new[rid] = {"id": rid, "created_at": (r["published"] or now).isoformat(timespec="seconds"),
                    "found_at": now.isoformat(timespec="seconds"), "tickers": r["stock"],
                    "feed": r["feed"], "source": r["source"], "title": r["title"],
                    "url": r["link"], "rss_summary": r["summary"][:600]}
    return store.add_news(sorted(new.values(), key=lambda r: r["created_at"])) if new else 0


def read_news(days: int = 2) -> list:
    """최근 며칠 안에 받은 뉴스."""
    rows = store.read_news(days)
    for r in rows:
        r["ts"] = parse_ts(r.get("created_at", ""))
    return [r for r in rows if r.get("id") and r["ts"]]


# ─────────────────────────────────────────────────────────────
# 판별
# ─────────────────────────────────────────────────────────────

def same_event(topic_a: str, tickers_a: str, topic_b: str, tickers_b: str) -> bool:
    """판별 모델이 붙인 사건 이름 둘이 같은 사건인가. 모델은 같은 일에 "마이크론 실적 발표",
    "마이크론 4분기 실적", "마이크론 실적 전망" 처럼 이름을 조금씩 달리 붙인다.
    종목이 하나라도 같고, 이름이 같거나 첫 낱말(보통 회사 이름) 뒤 낱말이 하나라도 겹치면 같은 사건으로 본다."""
    if not topic_a or not topic_b:
        return False
    ta = {x.strip() for x in tickers_a.split(",") if x.strip()}
    tb = {x.strip() for x in tickers_b.split(",") if x.strip()}
    if ta and tb and not ta & tb:
        return False
    if topic_a == topic_b:
        return True
    words = lambda t: set(t.split()[1:]) or set(t.split())
    return bool(words(topic_a) & words(topic_b))


# 사건 이름에 흔히 붙는 넓은 말. 여러 곳 보도를 셀 때는 이 말만 겹쳐서는 같은 사건으로 치지 않는다.
# 09-28 "삼성전자 AI 메모리" 50곳 가운데 48곳이 "삼성전자 AI 구독" 이었다 ("AI" 가 겹침)
BROAD_WORDS = {"AI", "주가", "시세", "시황", "동향", "전망", "분석", "논쟁", "소식", "이슈", "뉴스"}


def company_word(w: str, names: list) -> bool:
    """사건 이름 낱말이 관찰 종목 이름·키워드인가 ("삼성 하이닉스 시세" 의 "하이닉스")."""
    w = w.lower()
    return len(w) >= 2 and any(w in k or k in w for k in names)


def buzz_event(topic_a: str, tickers_a: str, topic_b: str, tickers_b: str, names: list = ()) -> bool:
    """여러 곳 보도를 셀 때의 같은 사건. same_event 보다 좁다: 종목이 겹치고, 이름이 같거나
    넓은 말(BROAD_WORDS)이 아닌 낱말이 겹쳐야 한다. "엔비디아 주가", "삼성 하이닉스 시세" 처럼
    넓은 말뿐인 이름은 서로 다른 기사를 한데 묶으므로 아예 세지 않는다. names(소문자 종목 이름·키워드)에 든 낱말도 넓은 말로 친다."""
    if not topic_a or not topic_b or not same_event(topic_a, tickers_a, topic_b, tickers_b):
        return False
    words = lambda t: {w for w in t.split()[1:] if w not in BROAD_WORDS and not company_word(w, names)}
    wa = words(topic_a)
    return bool(wa) and (topic_a == topic_b or bool(wa & words(topic_b)))


def news_line(r: dict) -> str:
    t = r["title"]
    if r.get("title_en") and r["title_en"] != t:
        t += f" / {r['title_en']}"
    extra = " ".join(x for x in (r.get("tickers"), r.get("labels")) if x)
    line = f"{t}" + (f" ({extra})" if extra else "")
    if r.get("summary"):   # 야후는 RSS 에 기사 설명이 딸려 온다
        line += f"\n   [설명] {r['summary'][:400]}"
    return line


def is_english(title: str) -> bool:
    """한글이 한 자도 없으면 영어 제목으로 본다."""
    return bool(title) and not re.search("[가-힣]", title)


# 반응 버튼: v 값 → (like, strong). 10점·0점은 👍/👎 보다 강한 반응이다.
FB_VALUES = {"10": (True, True), "1": (True, False), "0": (False, False), "00": (False, True)}


def fb_key(rec) -> str:
    """반응 기록 → "10" / "1" / "0" / "00", 반응이 없거나 취소했으면 None."""
    if not rec or rec.get("like") is None:
        return None
    return {(True, True): "10", (True, False): "1", (False, False): "0", (False, True): "00"}[
        (rec["like"], bool(rec.get("strong")))]


def latest_feedback() -> dict:
    return store.latest_feedback()   # 같은 뉴스에 여러 번 누르면 마지막 것


# 👎·🔕0 을 누른 뒤 고르는 까닭. 판별 프롬프트의 [과거 반응] 에 "(까닭: …)" 으로 붙는다
# 👎·🔕0 까닭. 09-28 까지 까닭 38건 중 20건이 "주가에 영향이 적은 뉴스" 하나에 몰려 있었고, 그 안에 설·수상·전망 기사가
# 섞여 있어서 종류별로 나눴다. 옛 이름(같은 내용 반복, 관심 없는 종류, 광고·시세 페이지, 종목과 무관,
# 종목과 관련 적은 일반 시황, 주가에 영향이 적은 뉴스)은 DB 에 그대로 남고 프롬프트에도 그대로 들어간다
WHY_CHOICES = ("옛 기사", "이미 본 내용", "확정 안 된 설", "숫자 없는 전망", "개인 의견·투자 조언", "수상·협약·홍보",
               "단순 시세·수급", "시장 전체 시황", "다른 회사가 주인공", "그 밖에 주가 영향 적음")


def examples_text(n: int) -> str:
    recs = sorted(latest_feedback().values(), key=lambda x: x.get("at", ""), reverse=True)
    groups = {k: [r["title"] + (f" (까닭: {r['why']})" if r.get("why") else "") for r in recs if fb_key(r) == k]
              for k in FB_VALUES}
    # 10점·0점은 드물고 중요하니 더 많이 남긴다
    lines = ([f"🔔 {t}" for t in groups["10"][:n * 2]] + [f"👍 {t}" for t in groups["1"][:n]]
             + [f"👎 {t}" for t in groups["0"][:n]] + [f"🔕 {t}" for t in groups["00"][:n * 2]])
    return "\n".join(lines) or "(아직 없음)"


def ask_claude(cfg: dict, prompt: str) -> str:
    # 프롬프트는 stdin 으로 넘긴다. 뉴스 제목이 많으면 명령줄 길이 제한에 걸린다.
    # 도구·MCP·설정·메모리를 모두 빼고 시스템 프롬프트를 한 줄로 바꾼다.
    # 그냥 부르면 Claude Code 전체가 딸려 와서 입력이 10배 넘게 는다 (10건에 4만 → 3천 토큰).
    p = subprocess.run(
        ["claude", "-p", "--model", cfg["claude_model"], "--output-format", "json",
         "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
         "--setting-sources", "", "--no-session-persistence",
         "--system-prompt", "너는 뉴스 판별기다. 요청한 JSON 만 출력한다."],
        input=prompt, capture_output=True, text=True, encoding="utf-8",
        timeout=cfg["timeout_sec"], cwd=str(DATA), shell=(sys.platform == "win32"),
    )
    try:
        out = json.loads(p.stdout)
    except ValueError:
        raise RuntimeError(f"claude 응답을 읽지 못함: {(p.stdout or p.stderr)[:200]}")
    if out.get("is_error"):
        raise RuntimeError(f"claude: {out.get('result', '')[:200]}")
    return out.get("result", "")


def ask_ollama(cfg: dict, prompt: str, timeout: float) -> str:
    r = requests.post(
        cfg["ollama_url"],
        json={
            "model": cfg["model"],
            "prompt": prompt,
            "stream": False,
            "think": False,
            "format": "json",
            "keep_alive": cfg["keep_alive"],
            "options": {"temperature": 0.0},
        },
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code}: {r.text[:200]}")
    return r.json()["response"]


def ask_targets(cfg: dict, prompt: str) -> tuple:
    """목표가 뽑기 물음: Ollama 먼저, 안 되면 Claude (10-01 전하 분부: Ollama 를 끈 동안에도 목표가 표가 채워지게).
    (답, 답한 쪽). backend 가 "ollama" 면 Claude 로 넘기지 않는다."""
    if cfg["backend"] in ("auto", "ollama"):
        try:
            return ask_ollama(cfg, prompt, cfg["ollama_timeout_sec"] if cfg["backend"] == "auto" else cfg["timeout_sec"]), "ollama"
        except Exception:
            if cfg["backend"] == "ollama":
                raise
    return ask_claude(cfg, prompt), "claude"


def parse_results(text: str, batch: list) -> dict:
    """{id: (score, reason, topic, say, ko, sum, con)}. reason 은 볼 까닭, con 은 안 볼 까닭. 모델이 빠뜨린 뉴스는 결과에 없다.
    ko 는 영어 제목의 번역, sum 은 RSS 설명(야후)의 한국어 요약."""
    try:
        items = json.loads(text).get("results", [])
    except (ValueError, AttributeError):
        m = re.search(r"\{.*\}", text, re.S)
        try:
            items = json.loads(m.group(0)).get("results", []) if m else []
        except (ValueError, AttributeError):
            items = []
    out = {}
    for it in items if isinstance(items, list) else []:
        try:
            i, score = int(it["i"]), int(round(float(it["score"])))
        except (KeyError, TypeError, ValueError):
            continue
        if 1 <= i <= len(batch):
            out[batch[i - 1]["id"]] = (max(0, min(10, score)), str(it.get("reason", "")).strip(),
                                       str(it.get("topic", "")).strip(), str(it.get("say", "")).strip(),
                                       str(it.get("ko", "") or "").strip() if is_english(batch[i - 1]["title"]) else "",
                                       str(it.get("sum", "") or "").strip() if batch[i - 1].get("summary") else "",
                                       str(it.get("con", "") or "").strip())
    return out


def stock_set(tickers: str) -> set:
    return {t.strip() for t in (tickers or "").split(",") if t.strip()}


def known_lines(recs: list, batch: list, now: datetime, days: float, cap: int = 60) -> list:
    """판별 프롬프트의 [이미 알린 소식]: 지난 days 일 동안 알린 뉴스 가운데 batch 와 종목이 겹치는 것, 새것부터 cap 줄.
    'MM-DD 종목 · 제목'. batch 에 든 뉴스 자신은 뺀다 (다시 판별할 때)."""
    if not days:
        return []
    want = set().union(*(stock_set(r.get("tickers")) for r in batch)) if batch else set()
    ids = {r["id"] for r in batch}
    lo, hi = (now - timedelta(days=days)).isoformat(timespec="seconds"), now.isoformat(timespec="seconds")
    hits = [r for r in recs if r.get("alerted") and r["id"] not in ids and lo <= r.get("at", "") < hi
            and stock_set(r.get("tickers")) & want]
    hits.sort(key=lambda r: r["at"], reverse=True)
    return [f"{r['at'][5:10]} {r.get('tickers', '')} · {r.get('title_ko') or r['title']}" for r in hits[:cap]]


def judge(cfg: dict, batch: list, topics: list = (), known: list = ()) -> tuple:
    """({id: (score, reason, topic, say, ko, sum, con)}, 판별한 쪽 이름). topics 는 최근에 붙인 사건 이름,
    known 은 이미 알린 뉴스 줄 (known_lines)."""
    prompt = PROMPT.format(
        interests=INTERESTS.read_text(encoding="utf-8") if INTERESTS.exists() else "(없음)",
        stocks=", ".join(f"{x['name']} ({x['yahoo']})" if x.get("yahoo") else x["name"] for x in load_stocks()) or "(없음)",
        examples=examples_text(cfg["examples"]),
        topics="\n".join(topics) or "(없음)",
        known="\n".join(known) or "(없음)",
        news="\n".join(f"{i}. {news_line(r)}" for i, r in enumerate(batch, 1)),
    )
    backend = cfg["backend"]
    if backend in ("auto", "ollama"):
        timeout = cfg["ollama_timeout_sec"] if backend == "auto" else cfg["timeout_sec"]
        try:
            out = parse_results(ask_ollama(cfg, prompt, timeout), batch)
            # 절반도 못 매겼으면 형식을 어긴 답으로 보고 claude 에게 다시 묻는다
            if len(out) * 2 < len(batch):
                raise RuntimeError(f"결과 {len(out)}/{len(batch)}건만 읽힘")
            return out, "ollama"
        except Exception as e:
            if backend == "ollama":
                raise
            log(f"ollama 실패 → claude: {str(e)[:120]}")
    return parse_results(ask_claude(cfg, prompt), batch), "claude"


TRANSLATE_PROMPT = """아래 영어 뉴스 제목을 자연스러운 한국어 뉴스 제목으로 번역하라.
회사 이름은 한국에서 흔히 쓰는 표기로 쓴다 (엔비디아, 마이크론, 브로드컴). 티커는 그대로 둔다.
JSON 만 출력하라: {{"results": [{{"i": 번호, "ko": "번역한 제목"}}]}}

{news}
"""


def translate_titles(cfg: dict, titles: list) -> list:
    """영어 제목 목록 → 한국어 제목 목록 (못 한 것은 ""). 판별과 같은 LLM 을 쓴다."""
    prompt = TRANSLATE_PROMPT.format(news="\n".join(f"{i}. {t}" for i, t in enumerate(titles, 1)))
    text = ""
    if cfg["backend"] in ("auto", "ollama"):
        try:
            text = ask_ollama(cfg, prompt, cfg["ollama_timeout_sec"])
        except Exception as e:
            if cfg["backend"] == "ollama":
                raise
            log(f"번역: ollama 실패 → claude: {str(e)[:80]}")
    if not text:
        text = ask_claude(cfg, prompt)
    m = re.search(r"\{.*\}", text, re.S)
    items = json.loads(m.group(0)).get("results", []) if m else []
    out = [""] * len(titles)
    for it in items if isinstance(items, list) else []:
        try:
            i = int(it["i"])
        except (KeyError, TypeError, ValueError):
            continue
        if 1 <= i <= len(titles):
            out[i - 1] = str(it.get("ko", "")).strip()
    return out


# ─────────────────────────────────────────────────────────────
# 알림
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# 음성
# ─────────────────────────────────────────────────────────────

def _mci(cmd: str):
    import ctypes
    err = ctypes.windll.winmm.mciSendStringW(cmd, None, 0, None)
    if err:
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.winmm.mciGetErrorStringW(err, buf, 256)
        raise OSError(f"MCI {err}: {buf.value}")


def play_file(path: str):
    """소리 파일을 끝까지 틀고 돌아온다."""
    kind = "waveaudio" if path.lower().endswith(".wav") else "mpegvideo"
    _mci(f'open "{path}" type {kind} alias newsalert')
    try:
        _mci("play newsalert wait")
    finally:
        _mci("close newsalert")


# "언론사, 제목" 에서 언론사가 영어면 그 부분만 영어 음성으로 읽는다. 한국어 음성은 영어 이름을 어색하게 읽는다
_LATIN_SOURCE = re.compile(r"(?=.*[A-Za-z])[A-Za-z0-9][A-Za-z0-9 .&'!:+\-]*")


def voice_parts(cfg: dict, text: str) -> list:
    """[(글, 음성)]. 영어 언론사 이름이 앞에 있으면 둘로 나눈다."""
    ko, en = cfg["tts_voice"], cfg.get("tts_voice_en")
    head, sep, rest = text.partition(", ")
    if en and sep and rest and _LATIN_SOURCE.fullmatch(head.strip()):
        return [(head.strip(), en), (rest, ko)]
    return [(text, ko)]


def tts_volume(cfg: dict) -> int:
    """목소리 크기 10 → 100 (%). 엉뚱한 값이면 100."""
    try:
        return max(10, min(100, int(cfg.get("tts_volume", 100))))
    except (TypeError, ValueError):
        return 100


def _speak_edge(cfg: dict, text: str):
    import asyncio
    import edge_tts
    parts = voice_parts(cfg, text)
    volume = f"{tts_volume(cfg) - 100:+d}%"
    files = []
    for _ in parts:
        fd, tmp = tempfile.mkstemp(prefix="news_tts_", suffix=".mp3")
        os.close(fd)
        files.append(tmp)

    async def make():   # 조각을 한꺼번에 받아 두고 이어서 튼다 (사이가 벌어지지 않게)
        await asyncio.gather(*(edge_tts.Communicate(t, v, rate=cfg["tts_rate"], volume=volume).save(f)
                               for (t, v), f in zip(parts, files)))
    try:
        asyncio.run(asyncio.wait_for(make(), 15))
        for f in files:
            play_file(f)
    finally:
        for f in files:
            try:
                os.remove(f)
            except OSError:
                pass


def _speak_sapi(text: str, volume: int = 100):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    try:
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        voice.Volume = volume
        voice.Speak(text)
    finally:
        pythoncom.CoUninitialize()


def speak(cfg: dict, text: str) -> str:
    """말머리 소리를 내고 text 를 읽는다. 실제로 읽은 길('edge'·'sapi'·'')을 돌려준다."""
    chime = cfg.get("tts_chime")
    if chime and os.path.exists(chime):
        try:
            play_file(chime)
        except OSError:
            pass
    try:
        _speak_edge(cfg, text)
        return "edge"
    except Exception as e:
        log(f"edge 음성 실패 → SAPI: {type(e).__name__}: {str(e)[:100]}")
    try:
        _speak_sapi(text, tts_volume(cfg))
        return "sapi"
    except Exception as e:
        log(f"SAPI 도 실패: {type(e).__name__}: {e}")
        return ""


def quiet_now(cfg: dict) -> bool:
    """조용한 시각("23:00-07:00") 안인가. 옛 형식 "23-07" 도 읽는다."""
    if not cfg.get("quiet_on"):
        return False
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d\d))?\s*-\s*(\d{1,2})(?::(\d\d))?\s*", cfg.get("tts_quiet") or "")
    if not m:
        return False
    a = int(m[1]) * 60 + int(m[2] or 0)
    b = int(m[3]) * 60 + int(m[4] or 0)
    now = datetime.now()
    t = now.hour * 60 + now.minute
    return a <= t < b if a <= b else t >= a or t < b


_speech = queue.Queue()

# 목록 판 번호. 판별 기록이 늘 때마다 1 씩 오르고(Watcher.ver), 페이지가 그 판을 받아 가면 _shown 에 적힌다.
# 음성은 화면에 먼저 뜬 뒤 읽는다 — 페이지가 그 판을 받아 갈 때까지, 길어야 SPEAK_WAIT_SEC 기다린다
# (목록 창을 안 띄웠거나 뒤로 숨어 타이머가 느려졌어도 음성은 나가야 한다).
SPEAK_WAIT_SEC = 6
_shown = {"ver": 0}
_shown_cv = threading.Condition()


def mark_shown(ver: int):
    with _shown_cv:
        if ver > _shown["ver"]:
            _shown["ver"] = ver
            _shown_cv.notify_all()


def _speech_worker(cfg: dict):
    # 한 판별 묶음에서 알림이 여럿 나와도 겹치지 않게 차례로 읽는다. 판별은 기다리지 않는다.
    while True:
        text, ver = _speech.get()
        with _shown_cv:
            _shown_cv.wait_for(lambda: _shown["ver"] >= ver, timeout=SPEAK_WAIT_SEC)
        try:
            speak(cfg, text)
        except Exception as e:
            log(f"음성 오류: {type(e).__name__}: {e}")


def spoken(r: dict, ko: str = "") -> str:
    """음성으로 읽을 말: "언론사, 제목". 영어 제목은 번역한 제목을 읽는다."""
    title = (ko or r.get("title_ko") or r.get("title") or "").strip()
    src = SOURCE_NAMES.get(r.get("source", ""), r.get("source", "")).strip()
    return f"{src}, {title}" if src and title else title


def say_alert(cfg: dict, text: str, ver: int = 0):
    """ver: 이 알림이 들어간 목록 판. 화면이 그 판을 보인 뒤에 읽는다 (0 이면 바로)."""
    if not cfg["tts"] or quiet_now(cfg) or not text:
        return
    _speech.put((text, ver))


def tg_ready() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def tg_send(text: str, silent: bool = False) -> str:
    """텔레그램으로 보낸다. 실패하면 까닭을, 성공하면 "" 를 돌려준다. 토큰은 어디에도 찍지 않는다."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 없다"
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=15, data={
            "chat_id": chat, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": "true", "disable_notification": "true" if silent else "false"})
    except requests.RequestException as e:
        return f"텔레그램에 닿지 못함 ({type(e).__name__})"
    return "" if r.ok else f"텔레그램 오류 HTTP {r.status_code}"


def telegram_alert(cfg: dict, r: dict, score: int, reason: str):
    if not cfg.get("telegram"):
        return
    text = (f"<b>[{score}점] {html.escape(r.get('tickers', ''))}</b> · {html.escape(reason)}\n"
            f"<a href=\"{html.escape(r['url'])}\">{html.escape(r.get('title_ko') or r['title'])}</a>")

    def run():
        err = tg_send(text, silent=quiet_now(cfg))
        if err:
            log(f"텔레그램 실패: {err}")
    threading.Thread(target=run, daemon=True).start()


def toast(cfg: dict, r: dict, score: int, reason: str):
    if not cfg.get("toast", True):
        return
    try:
        from winotify import Notification, audio
    except ImportError:
        log("winotify 가 없어 토스트를 띄우지 못했다. pip install winotify")
        return
    fb = f"http://127.0.0.1:{cfg['port']}/fb?id={r['id']}"
    n = Notification(app_id="종목 뉴스 필터", title=f"[{score}점] {r.get('tickers', '')} · {reason}",
                     msg=(r.get("title_ko") or r["title"])[:200], launch=r["url"])
    # 말로 읽을 때는 토스트 소리를 끈다. 말머리 소리와 겹치면 뉴스 알림인지 헷갈린다
    speaking = cfg["tts"] and not quiet_now(cfg)
    n.set_audio(audio.Silent if speaking else audio.Default, loop=False)
    n.add_actions(label="👍 관심", launch=fb + "&v=1")
    n.add_actions(label="👎 별로", launch=fb + "&v=0")
    n.show()


SUMMARY_PROMPT = """아래는 뉴스 기사 본문이다. 투자자가 알아야 할 핵심을 한국어 2~3문장(150자 안팎)으로 요약하라.
제목을 되풀이하지 말고, 숫자(금액·비율·날짜)는 살려라. 본문이 기사가 아니면(구독·로그인 안내, 오류 페이지) sum 을 빈 문자열로 둬라.
JSON 만 출력하라: {{"sum": "요약"}}

[제목] {title}
[본문]
{body}
"""


class Summarizer:
    """목록에 보이는 뉴스(숨김 점수 초과)를 한국어로 요약한다. Ollama 로만 한다.
    - 야후: RSS 설명을 줄인다 (판별 때 못 한 옛 기록만. 새 것은 판별 때 함께 한다)
    - 구글: 구글 링크를 풀어 원문을 받고 본문을 요약한다. 본문은 저장하지 않는다.
    Ollama 가 꺼져 있으면 아무것도 하지 않는다. poke() 는 판별 목록이 갱신될 때마다 불린다."""

    def __init__(self, watcher: "Watcher"):
        self.w = watcher
        self.running = False
        self.alive = None          # 마지막으로 본 Ollama 상태
        self.checked = 0.0
        # 구글이 429 로 막으면 이때까지 구글 기사는 쉰다. 잇달아 막힌 횟수는 한 번 통하면 0 으로.
        # 둘 다 DB 에 적어 둔다 — 안 그러면 다시 켤 때마다 곧바로 구글에 묻고 또 막힌다 (09-26 20:23)
        self.busy_until = float(store.get_meta("google_busy_until", "0") or 0)
        self.blocks = int(store.get_meta("google_blocks", "0") or 0)
        self.last_google = 0.0
        self.done = 0
        self.gate = threading.Lock()   # 구글 링크 풀기는 판별 루프와 요약 스레드가 함께 쓴다
        self.cache = {}                # id → 본문. 알리기 전에 받은 본문을 요약에 다시 쓴다 (메모리에만)
        self.dated = set()             # 원문 날짜를 본 id (Ollama 가 꺼져 있어도 옛 기사는 가린다)

    def fetch_article(self, rec: dict, urgent: bool = False, why: str = ""):
        """원문 (본문, 처음 나온 시각). 구글이 막는 중이면 article.Busy.
        구글에 묻는 사이를 google_gap_sec 만큼 띄운다. urgent(알리기 전 날짜 보기)는 google_alert_gap_sec 만.
        기다리는 동안 gate 를 쥐고 있지 않는다 — 요약이 3분 기다리는 사이 알림이 막히면 안 된다.
        article.NO_FETCH 언론사(무무)는 구글 링크도 풀지 않고 곧바로 Skip."""
        if article.no_fetch(source=rec.get("source", "")):
            raise article.Skip("원문 사이트가 자동 접속을 막음")
        gap = self.cfg["google_alert_gap_sec" if urgent else "google_gap_sec"]
        while True:
            with self.gate:
                if time.time() < self.busy_until:
                    raise article.Busy("구글이 잠시 막는 중")
                left = gap - (time.time() - self.last_google)
                if left <= 0:
                    self.last_google = time.time()
                    break
            time.sleep(min(left, 5))
        # 언제 물었는지 초 단위로 남긴다. 구글이 몇 번째에, 어떤 간격에서 막는지 보려면 있어야 한다
        log(f"구글 링크 풀기 ({why or ('알림 전' if urgent else '요약')}) {rec.get('title', '')[:50]}")
        try:
            _, body, pub = article.fetch_body(rec["url"])
        except article.Busy:
            with self.gate:
                rest = self.cfg["google_block_min"] * 2 ** min(self.blocks, 2)
                self.blocks += 1
                self.busy_until = time.time() + rest * 60
                self.save_block()
            log(f"구글이 요청이 많다며 막음 → {rest}분 쉬고 다시")
            raise
        if self.blocks:
            self.blocks = 0
            self.save_block()
        self.cache[rec["id"]] = (body, pub)
        return body, pub

    def save_block(self):
        try:
            store.set_meta("google_busy_until", f"{self.busy_until:.0f}")
            store.set_meta("google_blocks", str(self.blocks))
        except Exception as e:   # 못 적어도 이번 실행 동안은 메모리 값으로 쉰다
            log(f"구글 막힘 기록 실패: {type(e).__name__}")

    def is_stale(self, pub) -> bool:
        return bool(pub) and pub < datetime.now(timezone.utc) - timedelta(days=self.cfg["stale_days"])

    def mark_date(self, rec: dict, pub):
        """원문 날짜를 기록하고, 오래된 기사면 stale 로 표시한다."""
        if pub:
            rec["published_real"] = pub.isoformat(timespec="seconds")
            if self.is_stale(pub):
                rec["stale"] = True
                rec["alerted"] = False

    @property
    def cfg(self):
        return self.w.cfg

    def ollama_alive(self) -> bool:
        base = self.cfg["ollama_url"].split("/api/")[0]
        try:
            self.alive = requests.get(base + "/api/tags", timeout=2).ok
        except requests.RequestException:
            self.alive = False
        self.checked = time.time()
        return self.alive

    def pending(self) -> list:
        """뒤에서 저절로 요약할 것. 야후는 목록에 보이는 뉴스 모두 (RSS 설명을 줄이니 구글을 거치지 않는다).
        구글은 알리기 전 날짜 확인 때 이미 받아 둔 본문이 있는 것만. 나머지 구글 기사는 목록에서
        '요약 받기' 를 누를 때 받는다 — 구글에 묻는 것을 줄이려고 (2026-09-26, 3분 간격에도 13번째에 막힘)."""
        return [r for r in store.summary_todo(self.cfg["hide_max_score"], self.cfg["summary_hours"])
                if r.get("feed") != "google" or r["id"] in self.cache]

    def status(self) -> str:
        if not self.cfg.get("summarize"):
            return "요약 꺼짐"
        n = len(self.pending())
        if self.alive is False:
            return f"요약: Ollama 꺼짐 · 켜지면 {n}건 요약" if n else "요약: Ollama 꺼짐"
        if time.time() < self.busy_until:
            return (f"요약: 구글이 잠시 막아 {datetime.fromtimestamp(self.busy_until):%H:%M} 까지 '요약 받기' 가 안 된다"
                    + (f" · 남은 {n}건" if n else ""))
        return f"요약 중 · 남은 {n}건" if self.running else (f"요약 대기 {n}건" if n else "요약 다 됨")

    def poke(self):
        if self.running or not self.cfg.get("summarize"):
            return
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        """뒤에서는 구글에 묻지 않는다. 야후 설명과, 이미 받아 둔 구글 본문만 요약한다 (Ollama 가 켜져 있을 때)."""
        try:
            todo = self.pending()
            if not todo or not self.ollama_alive():
                return
            for rec in todo[:20]:
                if not self.cfg.get("summarize"):
                    break
                if not self.one(rec):
                    break
        except Exception as e:
            log(f"요약 오류: {type(e).__name__}: {str(e)[:120]}")
        finally:
            self.running = False

    def summarize_now(self, nid: str) -> dict:
        """목록에서 '요약 받기' 를 누른 구글 기사 하나. 알리기 전 날짜 확인과 같은 간격(30초)만 지킨다."""
        live = self.w.judged.get(nid)
        if not live:
            return {"ok": False, "msg": "그 뉴스를 찾지 못했다"}
        if live.get("summary_ko"):
            return {"ok": True}
        if not self.cfg.get("summarize"):
            return {"ok": False, "msg": "설정에서 구글 기사 요약이 꺼져 있다"}
        if not self.ollama_alive():
            return {"ok": False, "msg": "Ollama 가 꺼져 있다"}
        if time.time() < self.busy_until:
            return {"ok": False, "msg": f"구글이 막는 중 ({datetime.fromtimestamp(self.busy_until):%H:%M} 까지)"}
        rec = dict(live, feed="google")
        if nid not in self.cache:
            try:
                body, pub = self.fetch_article(rec, urgent=True, why="누름")
            except article.Busy:
                return {"ok": False, "msg": f"구글이 막았다 ({datetime.fromtimestamp(self.busy_until):%H:%M} 까지)"}
            except article.Skip as e:
                self.save(rec, "", f"skip:{e}")
                return {"ok": False, "msg": str(e)}
            except requests.RequestException as e:
                return {"ok": False, "msg": f"원문을 받지 못했다 ({type(e).__name__})"}
            self.dated.add(nid)
            self.mark_date(live, pub)
            if live.get("stale"):
                self.cache.pop(nid, None)
                self.save(rec, "", "skip:옛 기사")
                return {"ok": False, "msg": f"옛 기사라 숨겼다 (원문 {pub:%Y-%m-%d})"}
            if len(body) < 80:
                self.cache.pop(nid, None)
                self.save(rec, "", "skip:본문을 찾지 못함")
                return {"ok": False, "msg": "본문을 찾지 못했다"}
        if not self.one(rec):
            return {"ok": False, "msg": "Ollama 가 답하지 않았다"}
        got = self.w.judged.get(nid, {})
        return {"ok": bool(got.get("summary_ko")), "msg": "" if got.get("summary_ko") else "요약할 내용이 없다"}

    def check_date(self, rec: dict) -> bool:
        """원문을 받아 날짜를 기록한다 (본문은 요약에 쓰려고 메모리에 둔다). 구글이 막으면 False."""
        try:
            body, pub = self.fetch_article(rec)
        except article.Busy:
            return False
        except article.Skip as e:
            self.save(rec, "", f"skip:{e}")
            return True
        except requests.RequestException as e:
            self.save(rec, "", f"skip:{type(e).__name__}")
            return True
        self.dated.add(rec["id"])
        live = self.w.judged.get(rec["id"], rec)
        self.mark_date(live, pub)
        if live.get("stale"):
            self.cache.pop(rec["id"], None)
            self.save(rec, "", "skip:옛 기사")
            log(f"옛 기사라 목록에서 숨김 (원문 {pub:%Y-%m-%d}) {live.get('title_ko') or live['title']}"[:100])
        elif len(body) < 80:
            self.cache.pop(rec["id"], None)
            self.save(rec, "", "skip:본문을 찾지 못함")
        else:
            store.save_judged({k: v for k, v in live.items() if k not in ("feed", "rss_summary")})
        return True

    def one(self, rec: dict) -> bool:
        """한 건 요약. 계속해도 되면 True, 멈춰야 하면(Ollama 꺼짐·구글 429) False."""
        if rec.get("feed") == "google":
            if rec["id"] not in self.cache:   # 다시 켠 뒤라 본문이 메모리에 없다
                if not self.check_date(rec):
                    return False
                if rec["id"] not in self.cache:   # 옛 기사·본문 없음으로 끝났다
                    return True
            body, _ = self.cache.pop(rec["id"])
        else:
            body = rec.get("rss_summary") or ""
            if not body:
                self.save(rec, "", "skip:설명 없음")
                return True
        prompt = SUMMARY_PROMPT.format(title=rec["title"], body=body)
        try:
            text = ask_ollama(self.cfg, prompt, self.cfg["ollama_timeout_sec"])
        except Exception as e:   # Ollama 가 꺼졌다. 이 기사는 요약 전으로 남겨 다음에 다시
            self.alive = False
            log(f"요약: Ollama 실패 → 다음에 다시 ({type(e).__name__})")
            return False
        m = re.search(r"\{.*\}", text, re.S)
        try:
            summ = str(json.loads(m.group(0)).get("sum", "")).strip() if m else ""
        except ValueError:
            summ = ""
        self.save(rec, summ, "done" if summ else "skip:요약할 내용 없음")
        return True

    def save(self, rec: dict, summ: str, state: str):
        live = self.w.judged.get(rec["id"], rec)
        live.update(summary_ko=summ or None, summary_by="ollama" if summ else None, summary_state=state)
        live.pop("feed", None)
        live.pop("rss_summary", None)
        store.save_judged(live)
        if summ:
            self.done += 1
            log(f"요약 {live.get('title_ko') or live['title']}"[:80] + f" — {summ[:60]}")


class Watcher:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.judged = store.load_judged()
        self.recent_alerts = []   # (시각, 제목) — 비슷한 후속 보도를 거르려고
        self.first_seen = {}      # id → 처음 본 시각
        self.last_fetch = 0.0     # 마지막으로 RSS 를 받은 시각 (time.time)
        self.summarizer = Summarizer(self)
        self.ver = 1              # 목록 판 번호. 알림이 나갈 때 오른다 (mark_shown 참고)
        self.last_earn = 0.0      # 실적일을 마지막으로 살핀 시각. 받는 것은 하루 한 번 (earnings.refresh)
        self.last_moves = 0.0     # 급등락을 마지막으로 살핀 시각 (5분마다)
        self.move_seen = {}       # 티커 → 마지막으로 알린 시각 (같은 종목은 move_cooldown_min 에 한 번)
        self.last_react = 0.0     # 알림 뒤 주가 반응을 마지막으로 살핀 시각 (10분마다)
        self.moves = store.read_moves()   # 최근 급등락 [{at, ticker, name, change, price, news}] 새것부터, 10개까지
        self.fetch_note = "아직 받지 않음"

    def fetch(self):
        if time.time() - self.last_fetch < self.cfg["fetch_min"] * 60:
            return
        self.last_fetch = time.time()
        t0 = time.time()
        n = fetch_news(self.cfg)
        self.fetch_note = f"{datetime.now(KST):%H:%M} 새 뉴스 {n}건"
        if n:
            log(f"RSS 새 뉴스 {n}건 ({time.time() - t0:.1f}초)")

    def is_dup(self, title: str) -> bool:
        cutoff = time.time() - 3600
        self.recent_alerts = [(t, s) for t, s in self.recent_alerts if t > cutoff]
        return any(difflib.SequenceMatcher(None, title, s).ratio() >= self.cfg["dup_ratio"]
                   for _, s in self.recent_alerts)

    def recent(self, hours: float) -> list:
        """최근 hours 시간 안에 판별한 기록, 새것부터."""
        cutoff = (datetime.now(KST) - timedelta(hours=hours)).isoformat(timespec="seconds")
        recs = [r for r in self.judged.values() if r.get("at", "") >= cutoff]
        return sorted(recs, key=lambda r: r.get("at", ""), reverse=True)

    def recent_topics(self) -> list:
        """프롬프트에 넣을 최근 사건 이름. 같은 사건에 같은 이름을 다시 쓰게 한다."""
        seen = []
        for r in self.recent(3):
            t = r.get("topic")
            if t and t not in seen:
                seen.append(t)
        return seen[:40]

    def outlets(self, topic: str, tickers: str, source: str = "") -> int:
        """6시간 안에 같은 사건(same_event)을 쓴 언론사 수 (이름 갈래는 합쳐 센다). source 는 지금 판별하는 뉴스의 것."""
        names = [k.lower() for s in load_stocks() for k in [s["name"], *s.get("keywords", [])]]
        keys = {stocknews.source_key(r.get("source", "")) for r in self.recent(6)
                if buzz_event(topic, tickers, r.get("topic", ""), r.get("tickers", ""), names)}
        if not keys:
            return 0
        return len((keys | {stocknews.source_key(source)}) - {""})

    def topic_alerted(self, topic: str, tickers: str) -> bool:
        """topic_hours 안에 같은 사건으로 알림을 보냈는가 (same_event)."""
        return bool(topic) and any(r.get("alerted") and same_event(topic, tickers, r.get("topic", ""), r.get("tickers", ""))
                                   for r in self.recent(self.cfg["topic_hours"]))

    def pending(self) -> list:
        """아직 판별하지 않은 뉴스. 알릴 만큼 새것을 먼저, 밀린 것은 그 뒤에."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.cfg["catchup_hours"])
        seen, out = set(), []
        for r in read_news():
            done = self.judged.get(r["id"])
            if done:   # 목록 표시용으로만 최신 값을 반영한다
                done["title"] = r["title"]   # 원문 제목이 나중에 한글로 바뀐 경우
                done.setdefault("source", r.get("source", ""))
            if done or r["id"] in seen or r["ts"] < cutoff or found_late(r, self.cfg):
                continue
            seen.add(r["id"])
            out.append(r)
        return sorted(out, key=lambda r: (self.is_late(r), r["ts"]))

    def drop_junk(self, todo: list) -> list:
        """옵션 시세 페이지·소송 모집 광고, 제목 날짜가 오래된 옛 기사는 모델에 묻지 않고 0점으로 적는다 (stocknews.JUNK, old_by_title).
        목록에서는 점수가 낮아 숨고, '모두 보기' 에서 📏 로 보인다."""
        rest = []
        for r in todo:
            why = stocknews.junk_reason(r["title"])
            if not why:   # 제목에 적힌 날짜가 구글이 붙인 날짜보다 사흘 넘게 앞서면 옛 기사
                old = stocknews.old_by_title(r["title"], r.get("ts"), self.cfg["stale_days"])
                why = f"옛 기사 ({old})" if old else ""
            if not why:
                rest.append(r)
                continue
            rec = {"id": r["id"], "title": r["title"], "url": r["url"], "source": r.get("source", ""),
                   "tickers": r.get("tickers", ""), "created_at": r["created_at"], "score": 0,
                   "reason": f"규칙: {why}", "topic": "", "say": "", "title_ko": "", "alerted": False,
                   "late": False, "by": "rule", "at": datetime.now(KST).isoformat(timespec="seconds")}
            self.judged[r["id"]] = rec
            store.save_judged(rec)
            log(f"   0 [규칙: {why}] {r['title'][:70]}")
        return rest

    def is_late(self, r: dict) -> bool:
        """알리기엔 늦은 뉴스인가. PC 가 잠든 사이 나온 뉴스를 깨어나서 한꺼번에 울리지 않게."""
        return r["ts"] < datetime.now(timezone.utc) - timedelta(minutes=self.cfg["max_age_min"])

    def check_earnings(self):
        """10분마다 실적일이 오늘 받은 것인지 본다. 날이 바뀌었거나 종목이 바뀌었으면 뒤에서 새로 받는다."""
        if time.time() - self.last_earn < 600:
            return
        self.last_earn = time.time()

        def job():
            try:
                before = earnings.cached()
                got = earnings.refresh([s.get("yahoo", "") for s in load_stocks()])
                if got != before:
                    log(f"실적 발표 {len(got)}종목: " + ", ".join(
                        f"{t} {v['at'][5:16].replace('T', ' ')} {v.get('session', '')}".rstrip()
                        for t, v in sorted(got.items(), key=lambda x: x[1]["at"])))
            except Exception as e:
                log(f"실적일 받기 실패: {type(e).__name__}: {str(e)[:100]}")
            try:
                self.earnings_notice()
            except Exception as e:
                log(f"실적 발표 알림 실패: {type(e).__name__}: {str(e)[:100]}")
        threading.Thread(target=job, daemon=True).start()

    def earnings_notice(self):
        """확정된 실적 발표가 earnings_lead_hours 안으로 들어오면 한 번 알린다. 알린 것은 DB 에 적는다."""
        if not self.cfg.get("earnings_alert"):
            return
        now = datetime.now(KST)
        lead = timedelta(hours=float(self.cfg.get("earnings_lead_hours") or 8))
        try:
            sent = json.loads(store.get_meta("earnings_notified", "{}") or "{}")
        except ValueError:
            sent = {}
        names = {s.get("yahoo"): s["name"] for s in load_stocks()}
        for t, at, _, item in earnings.upcoming(3):
            if item.get("confirmed") is not True or sent.get(t) == item["at"]:
                continue
            if not (at - lead <= now < at):
                continue
            sent[t] = item["at"]
            store.set_meta("earnings_notified", json.dumps(sent))
            name = names.get(t, t)
            when = spoken_when(at, now) + ("께" if item.get("approx") else "")
            session = {"장 뒤": "장 마감 뒤", "장 전": "장 열기 전"}.get(item.get("session", ""), "")
            head = f"{name} ({t}) 실적 발표 {at:%m-%d}({'월화수목금토일'[at.weekday()]}) {at:%H:%M}{'께' if item.get('approx') else ''}"
            log(f"📅 {head} {item.get('session', '')}")
            if self.cfg.get("toast", True):
                try:
                    from winotify import Notification, audio
                    n = Notification(app_id="종목 뉴스 필터", title="실적 발표 알림", msg=f"{head} {item.get('session', '')}",
                                     launch=f"http://127.0.0.1:{self.cfg['port']}/")
                    n.set_audio(audio.Silent if self.cfg["tts"] and not quiet_now(self.cfg) else audio.Default, loop=False)
                    n.show()
                except Exception as e:
                    log(f"실적 발표 토스트 실패: {type(e).__name__}")
            if self.cfg.get("telegram"):
                err = tg_send(f"<b>실적 발표</b> · {html.escape(head)} {html.escape(item.get('session', ''))}",
                              silent=quiet_now(self.cfg))
                if err:
                    log(f"실적 발표 텔레그램 실패: {err}")
            say_alert(self.cfg, f"{name}, 실적 발표. {when}" + (f", {session}." if session else "."))

    def check_reactions(self):
        """10분마다: 알린 지 65분 넘은 뉴스의 1시간 주가 반응을 적는다 (한 번만). 종목이 여럿이면 첫 종목."""
        if time.time() - self.last_react < 600:
            return
        self.last_react = time.time()
        now = datetime.now(KST)
        done = set(store.reactions())
        todo = [r for r in self.judged.values() if r.get("alerted") and r["id"] not in done
                and now - timedelta(hours=48) <= datetime.fromisoformat(r["at"]) <= now - timedelta(minutes=65)]
        if not todo:
            return
        yahoo = {s["name"]: s.get("yahoo", "") for s in load_stocks()}

        def job():
            for r in todo:
                t = next((yahoo[n] for n in tickers_of(r) if yahoo.get(n)), "")
                if not t:
                    store.add_reaction(r["id"], "", r["at"], None, "티커 없음")
                    continue
                res = moves.reaction(t, datetime.fromisoformat(r["at"]))
                if res is False:   # 야후에서 못 받음: 적지 않고 다음 차례에 다시
                    continue
                store.add_reaction(r["id"], t, r["at"], res, "" if res else "장 닫힘")
                if res:
                    log(f"📊 알림 1시간 뒤 {t} {res[2]:+.1f}% · {(r.get('title_ko') or r['title'])[:60]}")
        threading.Thread(target=job, daemon=True).start()

    def check_moves(self):
        """5분마다 뒤에서 15분 변동을 본다. 기준을 넘으면 화면·토스트·음성(·텔레그램)."""
        if not self.cfg.get("move_alerts") or time.time() - self.last_moves < 300:
            return
        self.last_moves = time.time()
        stocks = [s for s in load_stocks() if s.get("yahoo")]
        names = {s["yahoo"]: s["name"] for s in stocks}
        big = {t.upper() for t in self.cfg.get("move_3x") or []}
        pct_for = lambda t: float(self.cfg["move_pct_3x"] if t.upper() in big else self.cfg["move_pct"])

        def job():
            try:
                hits = moves.check(list(names), pct_for)
            except Exception as e:
                log(f"급등락 확인 실패: {type(e).__name__}: {str(e)[:100]}")
                return
            for t, change, cur, before in hits:
                if time.time() - self.move_seen.get(t, 0) < self.cfg["move_cooldown_min"] * 60:
                    continue
                self.move_seen[t] = time.time()
                self.move_alert(t, names.get(t, t), change, cur)
        threading.Thread(target=job, daemon=True).start()

    def related_news(self, name: str, hours: float = 2, n: int = 3) -> list:
        """그 종목의 최근 뉴스 가운데 점수 높은 것 몇 개 (원인일 만한 것)."""
        recs = [r for r in self.recent(hours) if name in (r.get("tickers") or "") and r.get("by") != "rule"]
        return sorted(recs, key=lambda r: (r["score"], r.get("created_at", "")), reverse=True)[:n]

    def move_alert(self, ticker: str, name: str, change: float, price: float):
        word = "상승" if change > 0 else "하락"
        news = self.related_news(name)
        item = {"at": datetime.now(KST).isoformat(timespec="seconds"), "ticker": ticker, "name": name,
                "change": change, "price": price,
                "news": [{"title": r.get("title_ko") or r["title"], "url": r["url"], "score": r["score"]}
                         for r in news]}
        self.moves = ([item] + self.moves)[:10]
        store.add_move(item)
        log(f"📈 급등락 {ticker} 15분 {change:+.1f}% ({price:.2f}) · 뉴스 {len(news)}건"
            + "".join(f"\n     {x['score']:>2} {x['title'][:70]}" for x in item["news"]))
        self.ver += 1   # 목록이 곧바로 다시 그려지고, 음성은 그 뒤에
        head = f"{name} ({ticker}) 15분 {change:+.1f}%"
        body = item["news"][0]["title"] if news else "최근 2시간 안에 이 종목 뉴스가 없다"
        if self.cfg.get("toast", True):
            try:
                from winotify import Notification, audio
                n = Notification(app_id="종목 뉴스 필터", title=f"급등락 · {head}", msg=body[:200],
                                 launch=item["news"][0]["url"] if news else f"http://127.0.0.1:{self.cfg['port']}/")
                n.set_audio(audio.Silent if self.cfg["tts"] and not quiet_now(self.cfg) else audio.Default, loop=False)
                n.show()
            except Exception as e:
                log(f"급등락 토스트 실패: {type(e).__name__}")
        if self.cfg.get("telegram"):
            text = (f"<b>급등락 · {html.escape(head)}</b>\n" + "\n".join(
                f"[{x['score']}점] <a href=\"{html.escape(x['url'])}\">{html.escape(x['title'])}</a>"
                for x in item["news"]))
            threading.Thread(target=lambda: tg_send(text, silent=quiet_now(self.cfg)), daemon=True).start()
        say_alert(self.cfg, f"{name}, 15분 새 {abs(change):.1f}% {word}", self.ver)

    def weekly_report(self) -> dict:
        recs = self.recent(24 * 7)
        fb = {k: v for k, v in latest_feedback().items()}
        return weekly.build(recs, load_stocks(), fb, self.cfg["threshold"])

    def check_weekly(self):
        """weekly_notice 요일·시각이 지나면 그 주에 한 번 토스트(·텔레그램)로 알린다. 보낸 주는 DB 에 적는다."""
        spec = (self.cfg.get("weekly_notice") or "").split()
        if len(spec) != 2:
            return
        now = datetime.now(KST)
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        if spec[0] not in days or now.weekday() != days.index(spec[0]) or now.strftime("%H:%M") < spec[1]:
            return
        week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
        if store.get_meta("weekly_sent", "") == week:
            return
        store.set_meta("weekly_sent", week)

        def job():
            try:
                r = self.weekly_report()
            except Exception as e:
                log(f"주간 리포트 실패: {type(e).__name__}: {str(e)[:100]}")
                return
            url = f"http://127.0.0.1:{self.cfg['port']}/week"
            log(f"주간 리포트: 뉴스 {r['news']}건, 알림 {r['alerts']}건 → {url}")
            if self.cfg.get("toast", True):
                try:
                    from winotify import Notification
                    Notification(app_id="종목 뉴스 필터", title="주간 리포트가 나왔습니다",
                                 msg=f"지난 7일 뉴스 {r['news']}건, 알림 {r['alerts']}건. 눌러서 보기", launch=url).show()
                except Exception as e:
                    log(f"주간 리포트 토스트 실패: {type(e).__name__}")
            if self.cfg.get("telegram"):
                err = tg_send(weekly.telegram_text(r), silent=quiet_now(self.cfg))
                if err:
                    log(f"주간 리포트 텔레그램 실패: {err}")
        threading.Thread(target=job, daemon=True).start()

    def step(self):
        self.check_weekly()
        self.check_moves()
        self.check_reactions()
        self.check_earnings()
        self.fetch()
        self.summarizer.poke()
        todo = self.pending()
        todo = self.drop_junk(todo)
        if not todo:
            return
        # 한 건씩 바로 물으면 호출마다 관심사·예시를 다시 보내야 한다. 조금 모았다가 묻는다.
        waited = time.time() - self.first_seen.setdefault(todo[0]["id"], time.time())
        if len(todo) < self.cfg["batch"] and waited < self.cfg["max_wait_sec"]:
            return
        for k in range(0, len(todo), self.cfg["batch"]):
            batch = todo[k:k + self.cfg["batch"]]
            # 밀린 뉴스는 한 차례에 한 묶음만. 그사이 새로 들어온 뉴스가 뒤로 밀리지 않게.
            if k and self.is_late(batch[0]):
                return
            t0 = time.time()
            try:
                known = known_lines(list(self.judged.values()), batch, datetime.now(KST), self.cfg.get("known_days", 0))
                result, by = judge(self.cfg, batch, self.recent_topics(), known)
            except Exception as e:   # 모델이 바쁘거나 꺼져 있으면 다음 차례에 다시
                log(f"판별 실패: {e}")
                return
            log(f"{len(batch)}건 판별 {time.time() - t0:.1f}초 ({by})")
            self.fill_missing_ko(batch, result)
            for r in batch:
                if r["id"] not in result:
                    continue
                score, reason, topic, say, ko, summ, con = result[r["id"]]
                if ko:
                    r["title_ko"] = ko   # 토스트·텔레그램이 번역 제목을 쓴다
                # 같은 사건(시진핑 발언 문장마다 뜨는 속보, 실적 예고 기사 여럿 등)은 topic_hours 에 한 번만 알린다
                late = self.is_late(r)
                # 가린 언론사는 알리지도 않는다 (10-02 전하: Kalkine Media 는 눌러도 Cloudflare 에 막혀 못 읽는다). 판별·기록은 그대로
                muted = source_muted(self.cfg, r.get("source", ""))
                alert = (score >= self.cfg["threshold"] and not late and not muted
                         and not self.topic_alerted(topic, r.get("tickers", ""))
                         and not self.is_dup(r["title"]))
                if muted and score >= self.cfg["threshold"] and not late:
                    log(f"가린 언론사라 알리지 않음 ({r.get('source', '')}) {ko or r['title']}"[:90])
                buzz = 0
                if (not alert and not muted and self.cfg.get("buzz_sources") and score >= self.cfg.get("buzz_min_score", 5)
                        and not late and not self.topic_alerted(topic, r.get("tickers", ""))
                        and not self.is_dup(r["title"])):
                    buzz = self.outlets(topic, r.get("tickers", ""), r.get("source", ""))
                    if buzz >= self.cfg["buzz_sources"]:
                        alert = True
                        reason = f"{buzz}곳 보도 · {reason}"
                    else:
                        buzz = 0
                pub = None
                if alert and r.get("feed") == "google":
                    # 알리기 전에 원문 날짜를 본다. 구글이 막는 중이거나 날짜를 못 찾으면 그대로 알린다
                    try:
                        _, pub = self.summarizer.fetch_article(r, urgent=True)
                        self.summarizer.dated.add(r["id"])
                        if not pub:
                            log(f"원문 날짜 못 봄 ({r.get('source', '')}: 페이지에 날짜 표기 없음)")
                    except (article.Busy, article.Skip) as e:
                        # 까닭을 남겨야 어느 곳이 왜 안 읽히는지 셀 수 있다 (10-03: 알림 300번 중 104번이 까닭 모르게 날짜 없이 나갔다)
                        log(f"원문 날짜 못 봄 ({r.get('source', '')}: {e})")
                    except requests.RequestException as e:
                        log(f"원문 날짜 못 봄 ({r.get('source', '')}: {type(e).__name__})")
                    if self.summarizer.is_stale(pub):
                        alert = False
                        log(f"옛 기사라 알리지 않음 (원문 {pub:%Y-%m-%d}) {ko or r['title']}"[:90])
                rec = {"id": r["id"], "title": r["title"], "url": r["url"], "source": r.get("source", ""),
                       "tickers": r.get("tickers", ""),
                       "created_at": r["created_at"], "score": score, "reason": reason, "con": con, "topic": topic,
                       "say": say, "title_ko": ko, "alerted": alert, "late": late, "by": by,
                       "at": datetime.now(KST).isoformat(timespec="seconds")}
                if summ:   # 야후 설명을 판별 때 줄인 것. 구글 기사는 요약 스레드가 나중에 채운다
                    rec.update(summary_ko=summ, summary_by="rss", summary_state="done")
                self.summarizer.mark_date(rec, pub)
                self.judged[r["id"]] = rec
                store.save_judged(rec)
                mark = "🔔" if alert else "⏰" if late and score >= self.cfg["threshold"] else "  "
                log(f"{mark} {score:>2} [{topic}] {r['title'][:70]}  — ＋{reason} －{con}")
                if alert:
                    self.recent_alerts.append((time.time(), r["title"]))
                    toast(self.cfg, r, score, reason)
                    telegram_alert(self.cfg, r, score, reason)
                    self.ver += 1   # 목록 페이지가 2초 안에 알아채고 다시 그린다
                    say_alert(self.cfg, spoken(r, ko) or say or topic or reason, self.ver)

    def fill_missing_ko(self, batch: list, result: dict):
        """판별 모델이 영어 제목의 ko 를 빈칸으로 돌려줄 때가 있다 (09-26 까지 영어 535건 중 16건).
        그런 것만 번역 프롬프트로 한 번 더 묻는다. 그래도 못 하면 원문 제목을 그대로 쓴다."""
        miss = [r for r in batch if r["id"] in result and not result[r["id"]][4] and is_english(r["title"])]
        if not miss:
            return
        try:
            kos = translate_titles(self.cfg, [r["title"] for r in miss])
        except Exception as e:
            log(f"번역 다시 묻기 실패: {type(e).__name__}: {str(e)[:100]}")
            return
        for r, ko in zip(miss, kos):
            if ko:
                result[r["id"]] = result[r["id"]][:4] + (ko,) + result[r["id"]][5:]
        log(f"판별이 빠뜨린 번역 {sum(1 for k in kos if k)}/{len(miss)}건 다시 번역")

    def backfill_translations(self, hours: int = 48, size: int = 20):
        """번역이 없는 영어 제목을 한 번 번역해 둔다 (시작할 때 뒤에서).
        번역이 생기기 전 기록(칸 없음)과, 판별 모델이 ko 를 빈칸으로 준 기록("")을 함께 한다."""
        recs = [r for r in self.recent(hours)
                if not r.get("title_ko") and is_english(r.get("title", "")) and r.get("by") != "rule"]
        done = 0
        for k in range(0, len(recs), size):
            batch = recs[k:k + size]
            try:
                kos = translate_titles(self.cfg, [r["title"] for r in batch])
            except Exception as e:
                log(f"번역 실패: {type(e).__name__}: {str(e)[:100]}")
                return
            for r, ko in zip(batch, kos):
                if not ko and "title_ko" in r:
                    continue   # 이번에도 못 했다. 빈칸은 이미 적혀 있다
                r["title_ko"] = ko
                store.save_judged(r)
                done += bool(ko)
        if recs:
            log(f"지난 영어 제목 {done}/{len(recs)}건 번역")

    def targets_loop(self):
        """목표가 뉴스에서 증권사·목표가를 뽑는다 (1분마다, 처음에는 지난 7일치를 채운다).
        Ollama 에 먼저 묻고, 꺼져 있으면 Claude 에 묻는다 (ask_targets). 둘 다 안 되면 5분 뒤 다시 본다."""
        tries = {}   # 뉴스 id → 모델이 답을 빠뜨린 횟수. 세 번이면 "목표가 뉴스 아님" 으로 적는다
        down = False  # Ollama 가 꺼진 것을 한 번만 적는다 (게임하는 동안 5분마다 찍히지 않게)
        while True:
            try:
                self.check_targets(tries)
                down, wait = False, 60
            except requests.RequestException as e:
                if not down:
                    log(f"🎯 목표가 뽑기: Ollama 에 못 물음 ({type(e).__name__}). 켜질 때까지 5분마다 다시 본다")
                down, wait = True, 300
            except Exception as e:
                log(f"🎯 목표가 뽑기 오류: {type(e).__name__}: {str(e)[:120]}")
                wait = 300
            time.sleep(wait)

    def check_targets(self, tries: dict, days: int = 7, size: int = 8, most: int = 3) -> int:
        """아직 묻지 않은 목표가 뉴스 후보를 size 건씩 most 번까지 묻는다. 물어본 건수를 돌려준다."""
        done = store.targets_checked()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        todo = sorted((r for r in list(self.judged.values())
                       if r["id"] not in done and r.get("created_at", "") >= cutoff and targets.is_candidate(r)),
                      key=lambda r: r.get("created_at", ""), reverse=True)[:size * most]
        stocks = load_stocks()
        names = [s["name"] for s in stocks]
        ticker = {s["name"]: s.get("yahoo", "") for s in stocks}   # saveticker 쪽 기록과 티커로 맞춘다
        for k in range(0, len(todo), size):
            batch = todo[k:k + size]
            text, by = ask_targets(self.cfg, targets.build_prompt(batch, names))
            got = targets.parse(text, batch, names)
            for items in got.values():
                for x in items:
                    x["ticker"] = ticker.get(x["stock"], "")
            for r in batch:
                if r["id"] not in got:
                    tries[r["id"]] = tries.get(r["id"], 0) + 1
                    if tries[r["id"]] < 3:
                        continue
                store.save_targets(r, got.get(r["id"], []))
            n = sum(len(v) for v in got.values())
            log(f"🎯 목표가 {n}건 뽑음 (뉴스 {len(batch)}건 중 {sum(1 for v in got.values() if v)}건이 목표가 뉴스, {by})")
        return len(todo)

    def run(self):
        log(f"감시 시작: {DATA}  (모델 {self.cfg['model']}, 기준 {self.cfg['threshold']}점, "
            f"RSS {self.cfg['fetch_min']}분마다)")
        threading.Thread(target=self.backfill_translations, daemon=True).start()
        moved = store.move_targets_to_shared({s["name"]: s.get("yahoo", "") for s in load_stocks()})
        if moved:
            log(f"🎯 목표가 기록 {moved}건을 공용 DB 로 옮김 ({targets_db.PATH})")
        threading.Thread(target=self.targets_loop, daemon=True).start()
        while True:
            try:
                self.step()
            except Exception as e:   # 한 번의 오류로 감시가 멈추지 않게
                log(f"오류: {type(e).__name__}: {e}")
            time.sleep(self.cfg["poll_sec"])


# ─────────────────────────────────────────────────────────────
# 반응 기록용 로컬 페이지
# ─────────────────────────────────────────────────────────────

def make_handler(watcher: Watcher):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send_page(self, body: str, code: int = 200):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/fb" and q.get("id") in watcher.judged:
                rec = watcher.judged[q["id"]]
                # v=10 10점, v=1 👍, v=0 👎, v=00 0점, v=x 누른 것을 다시 눌러 취소
                like, strong = FB_VALUES.get(q.get("v"), (None, False))
                store.add_feedback({"id": rec["id"], "title": rec["title"], "like": like, "strong": strong,
                                    "at": datetime.now(KST).isoformat(timespec="seconds")})
                log(f"{FB_LABELS.get(q.get('v'), '취소')} {rec['title'][:70]}")
                if q.get("ajax"):     # 페이지 스크립트가 부른 것: 이동 없이 기록만
                    self.send_response(204)
                    self.end_headers()
                    return
                self.send_response(303)
                self.send_header("Location", f"/?done={rec['id']}" + ("&all=1" if q.get("all") else "")
                                 + (f"&s={quote(q['s'])}" if q.get("s") else ""))
                self.end_headers()
            elif u.path == "/fbwhy":   # 👎·🔕0 을 누른 까닭
                if q.get("id") in watcher.judged and q.get("why") in WHY_CHOICES:
                    store.set_why(q["id"], q["why"])
                    log(f"까닭 [{q['why']}] {watcher.judged[q['id']]['title'][:70]}")
                    self.send_response(204)
                else:
                    self.send_response(400)
                self.end_headers()
            elif u.path == "/ver":   # 페이지가 2초마다 묻는다. 바뀌었으면 목록을 다시 받는다
                data = str(watcher.ver).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/week":
                self.send_page(week_page(watcher))
            elif u.path == "/targets":
                self.send_page(targets_page(watcher, q.get("o", "")))
            elif u.path == "/sources":
                self.send_page(sources_page(watcher))
            elif u.path == "/react":
                self.send_page(react_page(watcher))
            elif u.path == "/":
                watcher.summarizer.poke()   # 목록이 갱신될 때마다 Ollama 가 켜졌는지 보고 밀린 요약을 한다
                ver = watcher.ver
                n = int(q["n"]) if q.get("n", "").isdigit() else PAGE_SIZE
                self.send_page(page(watcher, q.get("done"), bool(q.get("all")), n, q.get("s", "")))
                mark_shown(ver)   # 이 판까지는 화면에 보였다. 기다리던 음성이 나간다
            else:
                self.send_page("not found", 404)

        def do_POST(self):
            # 다른 사이트가 몰래 보내지 못하게 사용자 정의 헤더를 요구한다 (브라우저가 막는다).
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if self.headers.get("X-Settings") == "yes" and u.path in SETTING_ROUTES:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                    self.send_json(SETTING_ROUTES[u.path](watcher, body))
                except (ValueError, SystemExit, OSError) as e:
                    self.send_json({"ok": False, "msg": str(e)})
                return
            # 처음부터 다시: 페이지에서 한 번 더 확인받은 뒤에만 온다.
            if u.path != "/reset" or self.headers.get("X-Reset") != "yes" or q.get("what") not in ("feedback", "all"):
                self.send_page("bad request", 400)
                return
            self.send_json({"moved": reset_records(watcher, q["what"])})

        def send_json(self, obj):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return H


# ─────────────────────────────────────────────────────────────
# 설정 창 (⚙ 설정) 이 부르는 것들. 모두 {"ok": ..., "msg": ...} 를 돌려준다
# ─────────────────────────────────────────────────────────────

SETTING_LABELS = {"toast": "윈도우 알림", "threshold": "기준 점수", "max_age_min": "알림 시한", "tts": "음성",
                  "tts_voice": "목소리", "tts_voice_en": "영어 언론사 목소리", "tts_rate": "빠르기", "tts_volume": "목소리 크기", "tts_chime": "말머리 소리", "quiet_on": "조용한 시각",
                  "tts_quiet": "조용한 시각", "telegram": "텔레그램", "fetch_min": "받는 간격",
                  "catchup_hours": "밀린 뉴스", "topic_hours": "같은 사건 알림", "buzz_sources": "여러 곳 보도 알림", "backend": "판별 LLM", "claude_model": "Claude 모델",
                  "model": "Ollama 모델", "hide_max_score": "숨기기",
                  "summarize": "구글 기사 요약"}


def set_setting(watcher: Watcher, body: dict) -> dict:
    key = body.get("key", "")
    before, after = settings.apply(watcher.cfg, key, body.get("value"))
    if key == "telegram" and after and not tg_ready():
        watcher.cfg[key] = before
        return {"ok": False, "msg": "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 없어 켤 수 없다"}
    settings.save({k: v for k, v in watcher.cfg.items() if not k.startswith("_")}, CONFIG)
    if before != after:
        log(f"설정: {key} {before!r} → {after!r}")
    return {"ok": True, "label": SETTING_LABELS.get(key, key)}


def say_test(watcher: Watcher, body: dict) -> dict:
    text = str(body.get("text") or "").strip()[:60] or "삼성전자 목표가 상향"
    by = []
    # 음성을 꺼 두었어도 들어 볼 수 있게 대기열을 거치지 않고 바로 읽는다
    t = threading.Thread(target=lambda: by.append(speak(watcher.cfg, text)), daemon=True)
    t.start()
    t.join(1.5)   # 첫 소리가 날 때까지는 기다리지 않는다. 실패는 로그로
    return {"ok": True, "by": {"edge": "Edge 음성", "sapi": "윈도우 기본 음성"}.get(by[0], "") if by else ""}


def telegram_test(watcher: Watcher, body: dict) -> dict:
    err = tg_send("종목 뉴스 필터 시험 메시지입니다. 이 메시지가 보이면 알림도 여기로 옵니다.")
    return {"ok": not err, "msg": err}


def fetch_now(watcher: Watcher, body: dict) -> dict:
    n = fetch_news(watcher.cfg)
    watcher.last_fetch = time.time()
    watcher.fetch_note = f"{datetime.now(KST):%H:%M} 새 뉴스 {n}건"
    return {"ok": True, "new": n}


KEYWORD_PROMPT = """종목 뉴스 필터에 넣을 종목 설정을 제안하라.
종목 이름: {name}
야후 티커: {yahoo}

- google: 구글 뉴스 검색어. 미국 종목이면 영어로 "회사 이름 stock", 한국 종목이면 한국어 회사 이름.
- lang: 미국 종목 "en", 한국 종목 "ko".
- keywords: 기사 제목이나 요약에 이 가운데 하나라도 있어야 뉴스를 남긴다. 회사 이름의 흔한 표기
  (영어 이름, 짧은 이름, 한국어 이름), 그리고 티커. 3~6개.
  영어는 낱말 단위로, 대소문자를 가리지 않고 맞춘다. 그러니 흔한 영어 낱말과 같은 짧은 티커는 넣지 마라
  (예: BE 는 "be", MU 는 "mu", ON 은 "on" 과 겹친다). ETF 는 티커와 ETF 이름을 넣어라.
- exclude: 이 말이 들어가면 뉴스를 버린다. 이름이 비슷한 다른 회사·상품·우선주처럼 헷갈리는 것만. 없으면 [].
JSON 만 출력하라: {{"google": "...", "lang": "en", "keywords": ["..."], "exclude": []}}
"""


def suggest_keywords(watcher: Watcher, body: dict) -> dict:
    """설정 창 '제안받기': 판별 LLM 에게 검색어·키워드·뺄 말을 물어 돌려준다. 저장은 전하가 살펴본 뒤에."""
    name = str(body.get("name") or "").strip()
    if not name:
        return {"ok": False, "msg": "이름이 없다"}
    prompt = KEYWORD_PROMPT.format(name=name, yahoo=str(body.get("yahoo") or "").strip() or "(없음)")
    cfg, text = watcher.cfg, ""
    if cfg["backend"] in ("auto", "ollama"):
        try:
            text = ask_ollama(cfg, prompt, cfg["ollama_timeout_sec"])
        except Exception as e:
            if cfg["backend"] == "ollama":
                return {"ok": False, "msg": f"Ollama 실패 ({type(e).__name__})"}
    by = "ollama" if text else "claude"
    if not text:
        try:
            text = ask_claude(cfg, prompt)
        except Exception as e:
            return {"ok": False, "msg": f"Claude 실패 ({type(e).__name__})"}
    m = re.search(r"\{.*\}", text, re.S)
    try:
        got = json.loads(m.group(0)) if m else {}
    except ValueError:
        got = {}
    words = lambda k: [str(w).strip() for w in (got.get(k) or []) if str(w).strip()][:8]
    if not got:
        return {"ok": False, "msg": "모델 답을 읽지 못했다"}
    log(f"키워드 제안 ({by}) {name}: {got}")
    return {"ok": True, "by": by, "google": str(got.get("google") or "").strip(),
            "lang": "en" if got.get("lang") == "en" else "ko",
            "keywords": words("keywords"), "exclude": words("exclude")}


def source_muted(cfg: dict, source: str) -> bool:
    """가린 언론사인가. 이름 갈래(`Kalkine Media` 와 `kalkinemedia.com`)는 source_key 로 합쳐 본다."""
    return bool(source) and stocknews.source_key(source) in {stocknews.source_key(x) for x in cfg.get("hide_sources") or []}


def hide_source(watcher: Watcher, body: dict) -> dict:
    """언론사 성적표의 가리기/되살리기. 목록에서 가리고 알림도 내지 않는다 (판별은 그대로)."""
    src = str(body.get("source") or "").strip()
    if not src:
        return {"ok": False, "msg": "언론사 이름이 없다"}
    key = stocknews.source_key(src)
    muted = [x for x in watcher.cfg.get("hide_sources") or [] if stocknews.source_key(x) != key]
    if body.get("hide"):
        muted.append(src)
    watcher.cfg["hide_sources"] = sorted(muted)
    settings.save({k: v for k, v in watcher.cfg.items() if not k.startswith("_")}, CONFIG)
    log(f"언론사 {'가림' if body.get('hide') else '되살림'}: {src}")
    return {"ok": True, "hidden": bool(body.get("hide"))}


def watch_change(watcher: Watcher, body: dict) -> dict:
    """종목 추가·고치기·빼기. 추가하면 다음 차례(10초 안)에 바로 뉴스를 받는다."""
    op, name = body.get("op"), str(body.get("name") or "")
    if op == "add":
        s = stocknews.add_stock(name, str(body.get("yahoo") or ""))
        watcher.last_fetch = 0
        log(f"종목 추가: {s['name']}" + (f" ({s['yahoo']})" if s.get("yahoo") else ""))
    elif op == "update":
        s = stocknews.update_stock(name, body.get("fields") or {})
        log(f"종목 고침: {name} → {s}")
    elif op == "remove":
        stocknews.remove_stock(name)
        log(f"종목 삭제: {name}  (받아 둔 뉴스와 판별 기록은 그대로 둔다)")
    else:
        return {"ok": False, "msg": "알 수 없는 요청"}
    return {"ok": True}


SETTING_ROUTES = {"/settings": set_setting, "/say": say_test, "/telegram-test": telegram_test,
                  "/fetch": fetch_now, "/watch": watch_change,
                  "/hide-source": hide_source, "/suggest-keywords": suggest_keywords,
                  "/summarize-one": lambda w, body: w.summarizer.summarize_now(str(body.get("id") or ""))}


def load_stocks() -> list:
    try:
        return stocknews.load_watchlist()
    except (SystemExit, OSError, ValueError):
        return []


def reset_records(watcher: Watcher, what: str) -> list:
    """반응 기록(과 판별 기록)을 지운다. 실제로는 DB 표 이름을 바꿔 백업으로 남긴다."""
    moved = store.reset(what)
    if what == "all":
        watcher.judged.clear()
        watcher.first_seen.clear()
    log(f"처음부터 다시 ({what}): {', '.join(moved) or '지울 기록 없음'}")
    return moved


SOURCE_NAMES = {"finance.yahoo.com": "Yahoo Finance", "v.daum.net": "다음"}


def group_sources(stats: list) -> list:
    """source_stats() 의 줄을 같은 언론사끼리 합친다 (MarketBeat 와 marketbeat.com, Yahoo Finance UK 등).
    이름은 건수가 가장 많은 것, 단 소문자 도메인(marketbeat.com)·지역판(Investing.com India)은 뒤로 미룬다."""
    groups = {}
    for r in stats:
        groups.setdefault(stocknews.source_key(r["source"] or ""), []).append(r)
    out = []
    for rs in groups.values():
        def rank(r):
            raw = r["source"] or ""
            shown = SOURCE_NAMES.get(raw, raw)
            domain = shown == raw.lower() and "." in raw and " " not in raw
            return (domain, bool(stocknews.SOURCE_REGION.search(shown)), -r["n"])
        rs.sort(key=rank)
        best = rs[0]["source"] or ""
        n = sum(r["n"] for r in rs)
        out.append({"source": SOURCE_NAMES.get(best, best),
                    "names": [r["source"] for r in rs if r["source"]],
                    "n": n, "avg": sum(r["avg"] * r["n"] for r in rs) / n,
                    "hi": sum(r["hi"] or 0 for r in rs), "up": sum(r["up"] or 0 for r in rs),
                    "down": sum(r["down"] or 0 for r in rs),
                    "first": min((r["first"] for r in rs if r["first"]), default="")})
    return out


FB_LABELS = {"10": "🔔10점", "1": "👍", "0": "👎", "00": "🔕0점"}


def grading_trend(recs: list, th: int, low: int) -> str:
    """날짜별 맞힘률. interests.md 를 고친 뒤 나아지는지 보려고 둔다.
    날짜는 뉴스가 들어온 날 (판별은 들어온 지 몇 분 안에 한다)."""
    days = {}
    for r in recs:
        t = parse_ts(r.get("created_at", ""))
        if t:
            days.setdefault(t.astimezone(KST).strftime("%m-%d"), []).append(r)
    if not days:
        return ""
    rows = []
    for d in sorted(days, reverse=True)[:14]:
        rs = days[d]
        up = [r["score"] for r in rs if r["like"]]
        dn = [r["score"] for r in rs if not r["like"]]
        uh, dh = sum(1 for x in up if x >= th), sum(1 for x in dn if x <= low)
        miss = sum(1 for x in up if x <= low) + sum(1 for x in dn if x >= th)
        pct = lambda h, n: f"{h}/{n} ({h / n:.0%})" if n else "-"
        rows.append(f"<tr><td>{d}</td><td>{pct(uh, len(up))}</td><td>{pct(dh, len(dn))}</td><td>{miss or ''}</td></tr>")
    changed = ""
    if INTERESTS.exists():
        changed = f" · interests.md 마지막 수정 {datetime.fromtimestamp(INTERESTS.stat().st_mtime, KST):%m-%d %H:%M}"
    return (f"<p class=why>날짜별 (뉴스가 들어온 날{changed})</p>"
            "<table class=g><tr><th>날짜</th><th>👍 알림</th><th>👎 숨김</th><th>크게 어긋남</th></tr>"
            + "".join(rows) + "</table>")


def grading_html(watcher: Watcher) -> str:
    """판별 채점: 전하가 준 반응별로 모델 점수가 어땠나, 그리고 크게 어긋난 뉴스."""
    th, low = watcher.cfg["threshold"], watcher.cfg["hide_max_score"]
    recs = store.judged_with_feedback()
    if not recs:
        return "<p class=why>아직 반응(🔔10·👍·👎·🔕0)이 없어 채점할 것이 없다.</p>"
    kinds = (("🔔10", 1, 1), ("👍", 1, 0), ("👎", 0, 0), ("🔕0", 0, 1))
    rows = []
    for label, like, strong in kinds:
        sc = [r["score"] for r in recs if r["like"] == like and bool(r["strong"]) == bool(strong)]
        if not sc:
            continue
        hit = sum(1 for x in sc if x >= th) if like else sum(1 for x in sc if x <= low)
        rows.append(f"<tr><td>{label}</td><td>{len(sc)}</td><td>{sum(sc) / len(sc):.1f}</td>"
                    f"<td>{min(sc)}~{max(sc)}</td><td>{hit}건 ({hit / len(sc):.0%})</td></tr>")
    trend = grading_trend(recs, th, low)
    # 크게 어긋난 것: 좋다 했는데 숨김 점수 이하, 싫다 했는데 알림 점수 이상
    miss = [r for r in recs if (r["like"] and r["score"] <= low) or (not r["like"] and r["score"] >= th)]
    items = "".join(
        f"<li>{'👍' if r['like'] else '👎'} <b>{r['score']}점</b> "
        f"<a href=\"{html.escape(r['url'])}\" target=_blank>{html.escape(r['title'][:90])}</a> "
        f"<small>{html.escape(r['created_at'][:10])}</small></li>" for r in miss[:20])
    return (f"<table class=g><thead><tr><th>반응</th><th>건수</th><th>평균 점수</th><th>범위</th>"
            f"<th>맞힘</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
            f"<p class=why>맞힘: 좋다(🔔10·👍) 한 것은 {th}점 이상(알림), 싫다(👎·🔕0) 한 것은 {low}점 이하(숨김)였던 비율. "
            f"그 사이 점수는 맞히지도 틀리지도 않은 것으로 본다.</p>"
            + trend
            + (f"<p class=why>크게 어긋난 것 {len(miss)}건 — interests.md 를 고칠 때 볼 것</p><ul class=miss>{items}</ul>"
               if miss else "<p class=why>크게 어긋난 것은 없다.</p>"))


# 알림을 종류로 나눈다 (알림 뒤 주가 성적표). 위에서부터 먼저 맞는 것. 제목·번역 제목·사건 이름으로 본다
ALERT_KINDS = (
    ("여러 곳 보도", None),
    ("목표가·의견", re.compile(r"애널리스트|증권|analyst", re.I)),
    ("실적", re.compile(r"실적|earnings|가이던스|매출|영업이익|EPS|revenue|guidance", re.I)),
    ("주가 움직임", re.compile(r"주가|급등|급락|신고가|상승|하락|soar|surge|plunge|jump|slip|rall|drop|fall|shares", re.I)),
    ("계약·투자·M&A", re.compile(r"계약|수주|공급|인수|합병|상장|IPO|투자|deal|contract|acqui|order", re.I)),
    ("규제·정책·소송", re.compile(r"규제|제재|관세|소송|수출|중국|특허|ban|tariff|lawsuit|export|china", re.I)),
    ("발언", re.compile(r"발언|CEO|머스크|젠슨|Musk|Huang|says", re.I)),
    ("업황", re.compile(r"HBM|D램|낸드|DRAM|NAND|메모리|수요|가격|memory|demand", re.I)),
)


def alert_kind(rec: dict) -> str:
    if "곳 보도" in (rec.get("reason") or ""):
        return "여러 곳 보도"
    text = " ".join(rec.get(k) or "" for k in ("title", "title_ko", "topic"))
    if targets.CANDIDATE.search(text):
        return "목표가·의견"
    return next((name for name, rx in ALERT_KINDS[1:] if rx.search(text)), "그 밖")


def react_page(watcher: Watcher) -> str:
    """알림 뒤 주가 성적표: 알림을 점수·종류·종목·언론사로 묶어, 1시간 뒤 주가가 얼마나 움직였는지와 👍·👎 를 본다.
    주가는 장이 열려 있을 때 나간 알림만 잰다 (reactions 표). 움직임은 오르내림을 가리지 않은 크기(절댓값)다."""
    rx = store.reactions()
    fb = {k: fb_key(v) for k, v in latest_feedback().items()}
    recs = [r for r in list(watcher.judged.values()) if r.get("alerted")]
    first = min((r.get("at", "") for r in recs), default="")

    def table(title: str, key, least: int = 1, order=None) -> str:
        by = {}
        for r in recs:
            for k in key(r):
                by.setdefault(k, []).append(r)
        rows = []
        for k, rs in sorted(by.items(), key=order or (lambda x: -len(x[1]))):
            ch = sorted(abs(rx[r["id"]]["change"]) for r in rs if rx.get(r["id"]) and rx[r["id"]]["change"] is not None)
            if len(rs) < least:
                continue
            up = sum(fb.get(r["id"]) in ("1", "10") for r in rs)
            down = sum(fb.get(r["id"]) in ("0", "00") for r in rs)
            big = sum(c >= 1 for c in ch)
            rows.append(f"<tr><td>{html.escape(str(k))}</td><td>{len(rs)}</td><td>{len(ch) or ''}</td>"
                        f"<td>{f'{ch[len(ch) // 2]:.2f}%' if ch else ''}</td>"
                        f"<td>{f'{big} ({big * 100 // len(ch)}%)' if ch else ''}</td><td>{up or ''}</td><td>{down or ''}</td></tr>")
        return (f"<h3>{title}</h3><table class=g><thead><tr><th></th><th>알림</th><th>주가 잰 것</th><th>움직임 중간값</th>"
                f"<th>1% 넘게</th><th>👍</th><th>👎</th></tr></thead><tbody>{''.join(rows)}</tbody></table>")

    moved = sorted((r for r in recs if rx.get(r["id"]) and rx[r["id"]]["change"] is not None),
                   key=lambda r: -abs(rx[r["id"]]["change"]))[:15]
    top = "".join(
        f"<li>{reaction_html(rx[r['id']])} <small>{r.get('at', '')[5:16].replace('T', ' ')} · {r.get('score')}점 · "
        f"{html.escape(alert_kind(r))}</small> <a href=\"{html.escape(r.get('url', ''))}\" target=_blank>"
        f"{html.escape(r.get('title_ko') or r.get('title', ''))}</a></li>" for r in moved)
    measured = sum(1 for r in recs if rx.get(r["id"]) and rx[r["id"]]["change"] is not None)
    return f"""<!doctype html><meta charset=utf-8><title>알림 뒤 주가 · 종목 뉴스 필터</title>
<style>
body{{font:var(--fs) system-ui,sans-serif;background:#16181c;color:#e6e6e6;margin:16px;max-width:900px}}
a{{color:#8ab4f8}} h2{{margin:0 0 4px;font-size:1.3em}} .why{{color:#8a9099;font-size:.86em}}
table{{border-collapse:collapse}} th,td{{padding:4px 12px;border-bottom:1px solid #2a2d33;text-align:right}}
th:first-child,td:first-child{{text-align:left}} th{{color:#b8bec6;font-weight:600;white-space:nowrap}}
h3{{margin:18px 0 4px;font-size:1.1em}} ul{{margin:4px 0;padding-left:20px}} li{{margin:3px 0}}
li a{{color:#e6e6e6;text-decoration:none}} li small{{color:#8a9099}} .rx{{font-weight:600}}
</style>
{settings.FS_BAR}
<p class=why><a href='/'>← 판별 목록</a></p>
<h2>알림 뒤 주가</h2>
<p class=why>{html.escape(first[:10])} 부터 보낸 알림 {len(recs)}번, 그중 1시간 뒤 주가를 잰 것 {measured}번 (장이 닫혀 있을 때 나간 알림은 재지 않는다) ·
움직임은 오르내림을 가리지 않은 크기 · '1% 넘게' 는 잰 것 가운데 1% 넘게 움직인 수 · 건수가 적으면 우연일 수 있다</p>
{table("점수별", lambda r: [f"{r.get('score')}점"], order=lambda x: x[0].zfill(4))}
{table("종류별", lambda r: [alert_kind(r)])}
{table("종목별", lambda r: [t.strip() for t in (r.get("tickers") or "").split(",") if t.strip()])}
{table("언론사별 (알림 5번 이상)", lambda r: [SOURCE_NAMES.get(r.get("source") or "", r.get("source") or "(없음)")], least=5)}
<h3>가장 크게 움직인 알림</h3>
<ul>{top}</ul>"""


NAME_MAX = 50   # 언론사 성적표의 이름 칸 글자 수 (합친 이름 포함)


def sources_page(watcher: Watcher) -> str:
    """언론사 성적표: 언론사마다 건수·평균 점수·알림 대상(기준 점수 이상)·👍/👎. 가리면 목록에서 안 보이고 알림도 안 온다."""
    muted = {stocknews.source_key(x) for x in watcher.cfg.get("hide_sources") or []}
    stats = sorted(group_sources(store.source_stats()), key=lambda r: -r["n"])
    th = watcher.cfg["threshold"]
    first = min((r["first"] for r in stats if r["first"]), default="")
    rows = []
    for r in stats:
        src = r["source"]
        name = src or "(언론사 없음)"
        off = stocknews.source_key(src) in muted
        others = ", ".join(x for x in r["names"] if x != src)
        # 이름 칸은 합친 이름까지 50자 안으로, 넘으면 "..." (전하 요청). 전체는 마우스를 올리면 보인다
        full = name + (f" + {others}" if others else "")
        if len(name) > NAME_MAX:
            name, others = name[:NAME_MAX - 3] + "...", ""
        elif others and len(full) > NAME_MAX:
            others = others[:max(0, NAME_MAX - len(name) - 6)] + "..."
        rows.append(
            # 가리기 단추를 맨 왼쪽에 둔다 (전하 요청). 이 칸 머리글을 누르면 가린 것끼리 모인다
            f"<tr{' class=off' if off else ''}><td data-v={1 if off else 0}>"
            f"<button class=hs data-src=\"{html.escape(src, quote=True)}\" data-hide={'0' if off else '1'}>"
            f"{'되살리기' if off else '가리기'}</button></td><td title=\"{html.escape(full, quote=True)}\">{html.escape(name)}"
            f"{' <small>+ ' + html.escape(others) + '</small>' if others else ''}</td>"
            f"<td data-v={r['n']}>{r['n']}</td><td data-v={r['avg']:.2f}>{r['avg']:.1f}</td>"
            f"<td data-v={r['hi'] or 0}>{r['hi'] or ''}</td>"
            f"<td data-v={r['up'] or 0}>{r['up'] or ''}</td><td data-v={r['down'] or 0}>{r['down'] or ''}</td></tr>")
    return f"""<!doctype html><meta charset=utf-8><title>성적표 · 종목 뉴스 필터</title>
<style>
body{{font:var(--fs) system-ui,sans-serif;background:#16181c;color:#e6e6e6;margin:16px}}
a{{color:#8ab4f8}} h2{{margin:0 0 4px;font-size:1.3em}} .why{{color:#8a9099;font-size:.86em}}
table{{border-collapse:collapse}} th,td{{padding:4px 10px;border-bottom:1px solid #2a2d33;text-align:right}}
th:nth-child(-n+2),td:nth-child(-n+2){{text-align:left}} th{{cursor:pointer;color:#b8bec6;font-weight:600;white-space:nowrap}}
td small{{color:#8a9099}} tr.off td{{color:#6b7078}}
button.hs{{font:inherit;font-size:.85em;background:#2a2d33;color:#e6e6e6;border:1px solid #3a3f47;border-radius:6px;padding:2px 8px;cursor:pointer}}
tr.off button.hs{{background:#3a4a6b}}
h3{{margin:18px 0 4px;font-size:1.1em}} table.g td,table.g th{{cursor:default}} ul.miss{{margin:4px 0;padding-left:20px}}
ul.miss li{{margin:2px 0}} ul.miss a{{color:#e6e6e6;text-decoration:none}} ul.miss small{{color:#8a9099}}
</style>
{settings.FS_BAR}
<p class=why><a href='/'>← 판별 목록</a></p>
<h2>판별 채점</h2>
{grading_html(watcher)}
<h2 style="margin-top:22px">언론사 성적표</h2>
<p class=why>{html.escape(first[:10])} 부터 판별한 {sum(r['n'] for r in stats)}건, 언론사 {len(stats)}곳 ·
'알림' 은 {th}점 이상 · 👍·👎 는 🔔10·🔕0 포함, 뉴스마다 마지막 반응만 ·
가린 언론사는 판별 목록에서 안 보이고 알림도 오지 않는다 (판별은 그대로, '모두 보기' 로 볼 수 있다) · 머리글을 누르면 정렬</p>
<table id=t><thead><tr><th>{len(muted)}곳 가림</th><th>언론사</th><th>건수</th><th>평균 점수</th><th>알림</th><th>👍</th><th>👎</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<script>
document.querySelectorAll("#t th").forEach((th, i) => th.onclick = () => {{
  const body = document.querySelector("#t tbody"), rows = [...body.rows];
  const key = (tr) => i === 1 ? tr.cells[1].textContent.toLowerCase() : Number(tr.cells[i].dataset.v || 0);
  const dir = th.dataset.dir === "down" ? 1 : -1;
  document.querySelectorAll("#t th").forEach((x) => delete x.dataset.dir);
  th.dataset.dir = dir === -1 ? "down" : "up";
  rows.sort((a, b) => (key(a) > key(b) ? 1 : key(a) < key(b) ? -1 : 0) * (i === 1 ? -dir : dir));
  rows.forEach((tr) => body.appendChild(tr));
}});
document.querySelector("#t tbody").addEventListener("click", async (e) => {{
  const b = e.target.closest("button.hs");
  if (!b) return;
  const hide = b.dataset.hide === "1";
  const r = await fetch("/hide-source", {{method: "POST", headers: {{"X-Settings": "yes", "Content-Type": "application/json"}},
    body: JSON.stringify({{source: b.dataset.src, hide}})}});
  const d = r.ok ? await r.json() : null;
  if (!d || !d.ok) {{ alert("바꾸지 못했습니다" + (d && d.msg ? ": " + d.msg : "")); return; }}
  b.closest("tr").classList.toggle("off", hide);
  b.dataset.hide = hide ? "0" : "1";
  b.closest("td").dataset.v = hide ? "1" : "0";
  b.textContent = hide ? "되살리기" : "가리기";
}});
</script>"""


PAGE_SIZE = 50    # 목록에 한 번에 보이는 뉴스 수 (같은 사건으로 접힌 것도 센다). 맨 아래 '더 보기' 를 누르면 이만큼씩 더


def found_late(r: dict, cfg: dict) -> bool:
    """구글 기사인데, 구글이 붙인 날짜보다 max_age_min 넘게 늦게 받았는가. 이런 기사는 판별하지 않는다.
    알림도 안 나가고 목록에서도 숨기는 것이라 모델만 헛되이 부른다. 구글이 옛 기사에 새 날짜를 붙인 것이
    많고 (4월·7월 Stocktwits 기사), 종목을 새로 넣으면 하루치가 한꺼번에 들어온다 (Micron 73건).
    09-27 전하: "늦게 수집된건 지워"."""
    if r.get("feed") != "google":
        return False
    found = parse_ts(r.get("found_at", ""))
    return bool(found and r.get("ts") and found - r["ts"] > timedelta(minutes=cfg["max_age_min"]))


def unverified_late(r: dict) -> bool:
    """늦게 들어왔고(구글이 붙인 날짜로 max_age_min 넘게 지나 받음) 원문 날짜를 확인하지 못한 구글 기사.
    구글은 옛 기사에 새 날짜를 붙이기도 한다 (09-27 에 4월·7월 Stocktwits 기사가 09-25 날짜로 옴).
    이런 뉴스는 알림도 안 보내니 목록에서도 기본으로 숨긴다 ('모두 보기' 에서는 보인다)."""
    return bool(r.get("late")) and "news.google.com" in r.get("url", "") and not r.get("published_real")


def tickers_of(r: dict) -> list:
    """판별 기록의 종목 이름들. 두 종목에 걸린 기사는 tickers 가 "SK하이닉스, 삼성전자" 처럼 온다."""
    return [x.strip() for x in (r.get("tickers") or "").split(",") if x.strip()]


def page(watcher: Watcher, done: str = None, show_all: bool = False, limit: int = PAGE_SIZE,
         stock: str = "") -> str:
    """판별 목록. stock 을 주면 그 종목 뉴스만 (목록의 종목 이름을 누르면 ?s=종목)."""
    rx = store.reactions()
    fbrecs = latest_feedback()
    fb = {k: fb_key(v) for k, v in fbrecs.items()}
    whys = {k: v.get("why", "") for k, v in fbrecs.items()}
    recs = sorted(watcher.judged.values(), key=lambda r: r.get("created_at", ""), reverse=True)
    # 👎·0점 준 뉴스와 점수가 낮은 뉴스는 기본으로 숨긴다.
    # 방금 누른 것은 기록됐다는 표시를 위해, 👍·10점 준 것은 점수와 상관없이 남긴다.
    low = watcher.cfg["hide_max_score"]

    muted = {stocknews.source_key(x) for x in watcher.cfg.get("hide_sources") or []}

    def hide(r):
        if r["id"] == done or fb.get(r["id"]) in ("1", "10"):
            return False
        return (fb.get(r["id"]) in ("0", "00") or r["score"] <= low or bool(r.get("stale"))
                or unverified_late(r) or stocknews.source_key(r.get("source", "")) in muted)

    # 종목 줄에 붙일 것: 최근 24시간 목록에 보이는 뉴스 수와 최고 점수
    day_ago = datetime.now(timezone.utc) - timedelta(hours=24)
    counts = {}
    for r in recs:
        t = parse_ts(r.get("created_at", ""))
        if t and t >= day_ago and not hide(r):
            for name in tickers_of(r):
                n, top = counts.get(name, (0, 0))
                counts[name] = (n + 1, max(top, r["score"]))
    if stock:
        recs = [r for r in recs if stock in tickers_of(r)]
    hidden = 0 if show_all else sum(1 for r in recs if hide(r))
    if not show_all:
        recs = [r for r in recs if not hide(r)]
    more = max(0, len(recs) - limit)
    recs = recs[:limit]
    qs = ("&all=1" if show_all else "") + (f"&s={quote(stock)}" if stock else "")
    # 같은 사건은 가장 최근 뉴스 한 줄로 접는다. 6시간 넘게 떨어지면 다른 묶음으로 본다.
    # 같은 사건인지는 알림과 같은 same_event 로 본다 ("삼성전자 3분기 실적" · "삼성전자 3분기 영업익" · "삼성전자 100조 실적")
    members, order = {}, []
    for r in recs:
        tp, t = r.get("topic"), parse_ts(r.get("created_at", ""))
        h = next((x for x in reversed(order) if x.get("topic")
                  and same_event(tp, r.get("tickers", ""), x["topic"], x.get("tickers", ""))), None) if tp else None
        if h and t and (parse_ts(h["created_at"]) - t) <= timedelta(hours=6):
            members[h["id"]].append(r)
        else:
            members[r["id"]] = []
            order.append(r)
    rows = []
    for head in order:
        kids = members[head["id"]]
        # 묶음 id: 사건 이름만 쓰면 6시간 넘게 떨어져 나뉜 같은 이름의 묶음이 함께 펼쳐진다.
        # 가장 오래된 뉴스를 섞는다. 새 뉴스는 위에 붙으니 자동 갱신 뒤에도 id 가 그대로다.
        gid = (hashlib.md5(f"{head.get('topic', '')}|{kids[-1]['id']}".encode()).hexdigest()[:10]
               if kids else "")
        rows.append(row_html(head, fb, done, qs, gid=gid, kids=kids, muted=muted, whys=whys, rx=rx))
        rows.extend(row_html(k, fb, done, qs, child_of=gid, muted=muted, whys=whys, rx=rx) for k in kids)
    if stock and not rows:
        rows.append(f"<tr><td colspan=4 class=why>{html.escape(stock)} 뉴스가 " + ("없습니다." if show_all else
                    f"보이는 것이 없습니다 (숨긴 {hidden}건은 '모두 보기').") + "</td></tr>")
    note = "<p class=ok>반응을 기록했습니다. 다음 판별부터 반영됩니다.</p>" if done else ""
    if more:   # 목록 표 안에 두어야 15초 자동 갱신 때 같이 바뀐다
        rows.append(f"<tr><td colspan=4 style='text-align:center'><a id=more class=grp>"
                    f"더 보기 (남은 {more}건" + (f" 가운데 {PAGE_SIZE}건" if more > PAGE_SIZE else "") + ")</a></td></tr>")
    return page_html(watcher, rows, note, show_all, low, hidden, sum(1 for v in fb.values() if v), stock, counts)


def reasons_html(r: dict) -> str:
    """볼 까닭(＋, 초록)과 안 볼 까닭(－, 빨강). 09-27 전 기록은 점수를 준 까닭 하나(reason)뿐이라 그대로 보인다."""
    if not r.get("con"):
        return html.escape(r.get("reason", ""))
    pro = f"<span class=pro>＋ {html.escape(r['reason'])}</span> " if r.get("reason") else ""
    return pro + f"<span class=con>－ {html.escape(r['con'])}</span>"


def why_html(nid: str, state, why: str) -> str:
    """👎·🔕0 을 누른 뉴스 밑: 고른 까닭, 아직 안 골랐으면 고를 단추들."""
    if state not in ("0", "00"):
        return ""
    if why:
        return f"<div class=whyset>까닭: {html.escape(why)}</div>"
    return ("<div class=whypick>까닭? " + "".join(
        f"<a class=whyc data-id='{nid}' data-why='{html.escape(w, quote=True)}'>{html.escape(w)}</a>"
        for w in WHY_CHOICES) + "</div>")


def reaction_html(x) -> str:
    """알림 1시간 뒤 주가 (한국식: 오르면 빨강, 내리면 파랑)."""
    if not x or x.get("change") is None:
        return ""
    c = x["change"]
    color = "#e06c6c" if c > 0 else "#6c9be0" if c < 0 else "#8a9099"
    return (f" <span class=rx style='color:{color}' title='알림 때와 1시간 뒤 {html.escape(x['ticker'])} 값 (야후 5분봉)'>"
            f"1시간 뒤 {c:+.1f}%</span>")


def row_html(r: dict, fb: dict, done: str, qs: str, gid: str = "", kids=(), child_of: str = "",
             muted=frozenset(), whys=None, rx=None) -> str:
    t = parse_ts(r.get("created_at", ""))
    when = t.astimezone(KST).strftime("%m-%d %H:%M") if t else ""
    real = parse_ts(r.get("published_real") or "")
    if real and t and abs((real - t).total_seconds()) > 86400:   # 구글이 붙인 날짜와 원문 날짜가 하루 넘게 다르면
        when += f"<div class=old title='구글 뉴스가 새 날짜를 붙여 다시 올린 기사'>원문 {real.astimezone(KST):%m-%d}</div>"
    state = fb.get(r["id"])   # "10" / "1" / "0" / "00" / None
    classes = ["hit"] if r.get("alerted") else []
    if r["id"] == done:
        classes = ["done"]
    if child_of:
        classes += ["child", f"g-{child_of}"]
    cls = f' class="{" ".join(classes)}"' if classes else ""
    group = ""
    if kids:
        top = max(k["score"] for k in kids)
        n_src = len({stocknews.source_key(x.get("source", "")) for x in (r, *kids)} - {""})
        group = (f" <a class=grp data-g='{gid}'>같은 사건 +{len(kids)}건 (최고 {top}점"
                 f"{f', {n_src}곳' if n_src > 1 else ''}) ▾</a>")
    # 누가 판별했는지: 🦙 Ollama, ✴ Claude
    by = {"ollama": "<div class=by title='Ollama 가 판별'>🦙</div>",
          "claude": "<div class='by cl' title='Claude 가 판별'>✴</div>",
          "rule": "<div class=by title='모델에 묻지 않고 규칙으로 거름'>📏</div>"}.get(r.get("by"), "")
    topic = f"<span class=tp>{html.escape(r['topic'])}</span>" if r.get("topic") else ""
    source = SOURCE_NAMES.get(r.get("source", ""), r.get("source", ""))
    # 언론사 이름표를 누르면 그 자리에서 목록에서 가린다 (가린 것은 '모두 보기' 에서 눌러 되살린다)
    off = stocknews.source_key(r.get("source", "")) in muted
    src = (f"<a class='src{' off' if off else ''}' data-src=\"{html.escape(source, quote=True)}\" data-off={int(off)} "
           f"title='{'눌러서 이 언론사 되살리기' if off else '눌러서 이 언론사를 목록에서 가리고 알림도 끄기'}'>{html.escape(source)}</a>"
           if source else "")
    src = "".join(f"<a class=stk href='/?s={quote(t)}' title='{html.escape(t, quote=True)} 뉴스만 보기'>"
                  f"{html.escape(t)}</a>" for t in tickers_of(r)) + src
    # 누른 버튼은 불이 켜지고, 다시 누르면 취소된다.
    btns = "".join(
        f"<a class='fb{' num' if v in ('10', '00') else ''}{' on' if state == v else ''}' title='{tip}' "
        f"href='/fb?id={r['id']}&v={'x' if state == v else v}{qs}'>{label}</a>"
        for v, label, tip in (("10", "🔔10", "반드시 알려라 (10점)"), ("1", "👍", "관심"),
                              ("0", "👎", "별로"), ("00", "🔕0", "절대 알리지 마라 (0점)")))
    return (
        f"<tr{cls} data-at='{html.escape(r.get('at', ''), quote=True)}'><td class=b>{btns}</td>"
        f"<td class=t>{when}</td><td class=s>{r['score']}{by}</td>"
        f"<td><a{' class=rated' if state else ''} href='{html.escape(r['url'])}' target=_blank"
        f"{' title=' + chr(39) + html.escape(r['title'], quote=True) + chr(39) if r.get('title_ko') else ''}>"
        f"{html.escape(r.get('title_ko') or r['title'])}</a>"
        # 요약은 접어 둔다. '요약 ▾' 을 누르면 편다
        + (f"<div class=sum data-id='{r['id']}'>{html.escape(r['summary_ko'])}</div>" if r.get("summary_ko") else "")
        + f"<div class=why>{src}{topic}{reasons_html(r)}{reaction_html((rx or {}).get(r['id']))}"
        + (f" <a class=sumbtn data-id='{r['id']}'>요약 ▾</a>" if r.get("summary_ko") else
           f" <a class=sumget data-id='{r['id']}' title='원문을 받아 Ollama 로 요약한다 (구글에 한 번 묻는다)'>요약 받기</a>"
           if "news.google.com" in r.get("url", "") and not r.get("summary_state") else "")
        + f"{group}</div>{why_html(r['id'], state, (whys or {}).get(r['id'], ''))}</td></tr>")


def movers_html(watcher: Watcher) -> str:
    """주간 리포트: 알린 뒤 1시간에 주가가 크게 움직인 뉴스 다섯."""
    rx = store.reactions()
    cutoff = (datetime.now(KST) - timedelta(days=7)).isoformat(timespec="seconds")
    got = [(rx[r["id"]], r) for r in watcher.judged.values()
           if r.get("alerted") and r.get("at", "") >= cutoff and rx.get(r["id"], {}).get("change") is not None]
    if not got:
        return "<p class=why>주가를 움직인 알림: 아직 기록 없음 (알림 1시간 뒤 값을 적는다)</p>"
    got.sort(key=lambda x: -abs(x[0]["change"]))
    items = "".join(f"<li><b style='color:{'#e06c6c' if x['change'] > 0 else '#6c9be0'}'>{x['change']:+.1f}%</b> "
                    f"<span class=why>{html.escape(x['ticker'])} · {r['at'][5:16].replace('T', ' ')}</span> "
                    f"<a href=\"{html.escape(r['url'])}\" target=_blank>{html.escape(r.get('title_ko') or r['title'])}</a></li>"
                    for x, r in got[:5])
    return (f"<div class=card><b>주가를 움직인 알림</b> <span class=why>알린 뒤 1시간, 크게 움직인 순 "
            f"({len(got)}건 가운데)</span><ul>{items}</ul></div>")


def week_page(watcher: Watcher) -> str:
    """주간 리포트 페이지: 지난 7일, 종목별."""
    r = watcher.weekly_report()
    now = datetime.now(KST)
    tg = target_groups(load_stocks(), 7)
    cards = []
    for st in sorted(r["stocks"], key=lambda x: (-x["alerts"], -x["news"])):
        pr = st["price"]
        move = (f"<b style='color:{'#e06c6c' if pr[2] > 0 else '#6c9be0'}'>{pr[2]:+.1f}%</b> "
                f"<span class=why>{pr[0]:.2f} → {pr[1]:.2f}</span>") if pr else "<span class=why>값 없음</span>"
        def meta(x):   # 뉴스 앞에 날짜(한국 시각, 원문 날짜를 확인했으면 그것)와 언론사
            t = parse_ts(x.get("published_real") or "") or parse_ts(x.get("created_at", ""))
            src = SOURCE_NAMES.get(x.get("source", ""), x.get("source", ""))
            k = t.astimezone(KST) if t else None
            when = f"<span class=meta>{k:%m-%d}({'월화수목금토일'[k.weekday()]}) {k:%H:%M}</span> " if k else ""
            return when + (f"<span class=src>{html.escape(src)}</span> " if src else "")

        top = "".join(f"<li{' class=unsure' if x.get('unsure') else ''}>{meta(x)}{x['score']}점 <a href=\"{html.escape(x['url'])}\" target=_blank>"
                      f"{html.escape(x.get('title_ko') or x['title'])}</a>"
                      + (" <small title='들어왔을 때 이미 2시간 넘게 지난 뉴스라 원문 날짜를 확인하지 않았다. "
                         "구글이 옛 기사에 새 날짜를 붙인 것일 수 있다'>날짜 확인 안 됨</small>" if x.get("unsure") else "")
                      + "</li>" for x in st["top"])
        cards.append(f"<div class=card><div><b>{html.escape(st['name'])}</b> <span class=why>{html.escape(st['ticker'])}</span> · {move}</div>"
                     f"<div class=why>뉴스 {st['news']}건 · 알림 {st['alerts']}건</div>"
                     + week_targets_html(tg.get(st["name"], []))
                     + (f"<ul>{top}</ul>" if top else "") + "</div>")
    topics = " · ".join(f"{html.escape(t)} {n}건" for t, n in r["topics"]) or "없음"
    return f"""<!doctype html><meta charset=utf-8><title>주간 리포트 · 종목 뉴스 필터</title>
<style>
body{{font:var(--fs) system-ui,sans-serif;background:#16181c;color:#e6e6e6;margin:16px;max-width:980px}}
a{{color:#e6e6e6;text-decoration:none}} a:hover{{text-decoration:underline}} .why{{color:#8a9099;font-size:.88em}}
h2{{margin:0 0 4px;font-size:1.3em}} .card{{background:#1f2228;border-radius:8px;padding:8px 12px;margin:8px 0}}
.card ul{{margin:4px 0 0;padding-left:20px}} .card li{{margin:2px 0}} .card li.unsure{{opacity:.6}} .card .meta{{color:#8a9099;font-size:.86em;margin-right:4px}} .card .src{{display:inline-block;margin-right:6px;padding:0 5px;border-radius:4px;background:#2a2d33;color:#b8bec6;font-size:.79em}} .card li small{{color:#e0a44a}} .back{{color:#8ab4f8}}
</style>
{settings.FS_BAR}
<p class=why><a class=back href='/'>← 판별 목록</a></p>
<h2>주간 리포트</h2>
<p class=why>{(now - timedelta(days=7)):%m-%d} ~ {now:%m-%d %H:%M} · 판별한 뉴스 {r['news']}건 · 알림 {r['alerts']}건 ·
👍 {r['up']} · 👎 {r['down']} · 등락은 야후 일봉 종가 (7일 전 → 마지막)</p>
<p class=why>많이 나온 사건 ({watcher.cfg['threshold']}점 이상): {topics}</p>
{movers_html(watcher)}
{''.join(cards)}"""


def money(v, cur: str) -> str:
    """목표가 표시: $1,520 / $12.50 / 2,640,000원."""
    if v is None:
        return ""
    s = f"{v:,.2f}" if v < 100 and v != int(v) else f"{v:,.0f}"
    return {"USD": f"${s}", "KRW": f"{s}원"}.get(cur, f"{s} {cur}".strip())


TARGET_COLORS = {"상향": "#e06c6c", "의견상향": "#e06c6c", "하향": "#6c9be0", "의견하향": "#6c9be0",
                 "신규": "#e0a44a", "유지": "#8a9099", "제시": "#8a9099"}


# 목표가 표 골라 보기: 기간(오늘·3일·7일·전체)과 "유지·제시 빼기". 브라우저에서 줄을 감추기만 한다 (그림으로 복사도 보이는 줄만 담는다)
TG_FILTER = """<script>
addEventListener("DOMContentLoaded", () => {
  const sel = document.getElementById("tgd"), chk = document.getElementById("tgk"), cnt = document.getElementById("tgc");
  if (!sel) return;
  const get = k => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const set = (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} };
  sel.value = get("tgd") || "0"; chk.checked = get("tgk") === "1";
  const apply = () => {
    const days = Number(sel.value), d = new Date();
    d.setDate(d.getDate() - (days - 1));
    const cut = days ? d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0") : "";
    let shown = 0, all = 0;
    for (const tr of document.querySelectorAll("tr[data-d]")) {
      all++;
      tr.hidden = (cut && tr.dataset.d < cut) || (chk.checked && (tr.dataset.a === "유지" || tr.dataset.a === "제시"));
      if (!tr.hidden) shown++;
    }
    for (const c of document.querySelectorAll(".card")) c.hidden = !c.querySelector("tr:not([hidden])");
    cnt.textContent = shown === all ? all + "줄" : shown + " / " + all + "줄";
    set("tgd", sel.value); set("tgk", chk.checked ? "1" : "0");
  };
  sel.onchange = chk.onchange = apply;
  apply();
});
</script>"""


# 목표가 표를 그림(PNG)으로 만들어 클립보드에 넣는다 (10-01 전하: PC 에서 캡처해 카톡으로 보낸다).
# 표를 복제해 계산된 모양을 줄마다 박아 넣고, SVG foreignObject 로 그려 canvas 에서 PNG 를 뽑는다. 바깥 라이브러리는 쓰지 않는다
COPY_IMG = """<script>
const CPY_PROPS = ["display","grid-template-columns","grid-column-start","grid-column-end","column-gap","color","background-color",
  "font-family","font-size","font-weight","line-height","padding-top","padding-right","padding-bottom","padding-left",
  "margin-top","margin-right","margin-bottom","margin-left","border-top","border-right","border-bottom","border-left",
  "border-radius","white-space","width","min-width","max-width","text-decoration","list-style-type","vertical-align",
  "box-sizing","opacity","text-align","overflow-wrap","word-break"];
async function targetsPng(limit) {
  const box = document.createElement("div"), bs = getComputedStyle(document.body);
  const first = document.querySelector(".card, body > table.tg");
  if (!first) throw new Error("표가 비어 있음");
  box.style.cssText = "position:absolute;left:-99999px;top:0;box-sizing:border-box;padding:10px 12px;background:#16181c;color:" + bs.color +
    ";font:" + bs.font + ";width:" + (first.getBoundingClientRect().width + 24) + "px";
  const d = new Date(), head = document.createElement("div");
  const fd = document.getElementById("tgd"), fk = document.getElementById("tgk");   // 골라 보기를 켰으면 머리글에 적는다
  const pick = [fd && fd.value !== "0" ? fd.options[fd.selectedIndex].text : "", fk && fk.checked ? "유지·제시 뺌" : ""].filter(Boolean).join(", ");
  head.textContent = "목표가 표 · " + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0") + (pick ? " (" + pick + ")" : "");
  head.style.cssText = "font-weight:700;margin:0 0 6px";
  box.appendChild(head);
  for (const el of document.querySelectorAll(".card, body > table.tg")) box.appendChild(el.cloneNode(true));
  document.body.appendChild(box);
  try {
    for (const x of box.querySelectorAll("[hidden]")) x.remove();   // 골라 보기로 감춘 줄
    [...box.querySelectorAll("tr")].forEach((tr, i) => { if (limit && i >= limit) tr.remove(); });
    for (const c of box.querySelectorAll(".card")) if (!c.querySelector("tr")) c.remove();
    for (const dt of box.querySelectorAll("details")) {   // 그림에서는 "N곳" 을 펼칠 수 없으니 첫 기사 제목을 적는다
      const lis = dt.querySelectorAll("li"), ul = document.createElement("ul");
      if (!lis.length) continue;
      ul.className = "one"; ul.appendChild(lis[0]);
      const more = document.createElement("span");
      more.className = "why"; more.textContent = " 외 " + (lis.length - 1) + "곳";
      lis[0].appendChild(more); dt.replaceWith(ul);
    }
    for (const a of box.querySelectorAll("a")) a.removeAttribute("href");
    const all = [...box.querySelectorAll("*")];
    const styles = all.map(el => { const cs = getComputedStyle(el); return CPY_PROPS.map(p => p + ":" + cs.getPropertyValue(p)).join(";"); });
    all.forEach((el, i) => { el.removeAttribute("class"); el.setAttribute("style", styles[i]); });
    const w = Math.ceil(box.getBoundingClientRect().width), h = Math.ceil(box.getBoundingClientRect().height);
    box.style.position = "static"; box.style.left = "auto";
    const xml = new XMLSerializer().serializeToString(box);
    const svg = "<svg xmlns='http://www.w3.org/2000/svg' width='" + w + "' height='" + h + "'><foreignObject width='100%' height='100%'>" + xml + "</foreignObject></svg>";
    const img = new Image();
    await new Promise((ok, no) => { img.onload = ok; img.onerror = () => no(new Error("그림을 만들지 못함")); img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg); });
    const k = Math.min(2, 16000 / h), cv = document.createElement("canvas");
    cv.width = Math.round(w * k); cv.height = Math.round(h * k);
    const cx = cv.getContext("2d"); cx.scale(k, k); cx.drawImage(img, 0, 0);
    return await new Promise((ok, no) => cv.toBlob(b => b ? ok(b) : no(new Error("PNG 를 만들지 못함")), "image/png"));
  } finally { box.remove(); }
}
addEventListener("DOMContentLoaded", () => {
  const a = document.getElementById("cpy"), sel = document.getElementById("cpyn"), note = document.getElementById("cpymsg");
  if (!a) return;
  const say = t => { note.textContent = t; };
  a.onclick = async () => {
    say("그림 만드는 중…");
    const job = targetsPng(Number(sel.value));
    try {
      await navigator.clipboard.write([new ClipboardItem({"image/png": job})]);
      say("클립보드에 넣었습니다. 카톡 창에서 Ctrl+V.");
    } catch (e) {
      try {   // 클립보드가 막혔으면 파일로 내려 준다
        const url = URL.createObjectURL(await job), dl = document.createElement("a");
        dl.href = url; dl.download = "targets.png"; dl.click(); URL.revokeObjectURL(url);
        say("클립보드에 못 넣어 targets.png 파일로 내려받았습니다.");
      } catch (e2) { say("실패: " + e2.message); }
    }
  };
});
</script>"""


def target_row(g: dict, stock: str = "") -> str:
    """목표가 표 한 줄. stock 을 주면(최신순 보기) 종목 칸을 넣는다. 날짜는 월-일 시:분 (10-01 전하 분부: 요일은 뺌)."""
    k = g["at"].astimezone(KST)
    pt = money(g["pt_new"], g["currency"])
    if g["pt_old"] and g["pt_new"] and g["pt_old"] != g["pt_new"]:
        pt = (f"{money(g['pt_old'], g['currency'])} → {pt} "
              f"<span class=pct>({(g['pt_new'] / g['pt_old'] - 1) * 100:+.1f}%)</span>")
    links = "".join(f"<li><span class=src>{html.escape(SOURCE_NAMES.get(x['source'] or '', x['source'] or ''))}</span>"
                    f"<a href=\"{html.escape(x['url'])}\" target=_blank>{html.escape(x['title_ko'] or x['title'])}</a></li>"
                    for x in g["news"])
    news = (f"<details><summary>{len(g['news'])}곳</summary><ul>{links}</ul></details>" if len(g["news"]) > 1
            else f"<ul class=one>{links}</ul>")
    return (f"<tr data-d='{k:%Y-%m-%d}' data-a='{g['action']}'><td class=d>{k:%m-%d %H:%M}</td>"
            + (f"<td class=sk>{html.escape(stock)}</td>" if stock else "")
            + f"<td class=br>{html.escape(g['broker'])}</td>"
            f"<td class=ac><b style='color:{TARGET_COLORS.get(g['action'], '#e6e6e6')}'>{g['action']}</b></td>"
            f"<td class=pt>{pt}</td><td class=rt>{html.escape(g['rating'])}</td>"
            f"<td class=nw>{news}</td></tr>")


def consensus_html(gs: list, ticker: str, prices: dict) -> str:
    """종목별 표의 종목 칸 머리에 붙는 한 줄: 평균 목표가(증권사 수, 최저 → 최고) · 현재가 · 괴리율.
    괴리율은 평균 목표가가 현재가보다 얼마나 높은가다. 현재가를 못 받았거나 통화가 다르면 평균만 보인다."""
    c = targets.consensus(gs)
    if not c:
        return ""
    cur = c["currency"]
    out = f"평균 목표가 <b style='color:#e6e6e6'>{money(c['avg'], cur)}</b> ({c['n']}곳"
    out += f", {money(c['low'], cur)} → {money(c['high'], cur)})" if c["n"] > 1 else ")"
    price = prices.get(ticker)
    if price and cur == ("KRW" if ticker[:1].isdigit() else "USD"):
        gap = targets.gap_pct(c["avg"], price[0])
        out += (f" · 현재가 {money(price[0], cur)} <span class=pct>({price[1].astimezone(KST):%m-%d %H:%M})</span>"
                f" · 괴리율 <b style='color:{'#e06c6c' if gap >= 0 else '#6c9be0'}'>{gap:+.1f}%</b>")
    return f"<div class='why cs'>{out}</div>"


def target_groups(stocks: list, days: int) -> dict:
    """{종목 이름: [한 줄로 합친 목표가 조치, 새것부터]} 최근 days 일. 목표가 표와 주간 리포트가 쓴다.
    공용 DB 에는 saveticker 가 뽑은 모든 회사가 있다. 관찰 종목만, 이름은 이쪽 이름으로 (티커로 맞춘다)."""
    by_ticker = {targets_db.ticker_key(s.get("yahoo", "")): s["name"] for s in stocks if s.get("yahoo")}
    by_name = {s["name"].lower(): s["name"] for s in stocks}
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    by = {}
    for r in store.read_targets(since):
        name = by_ticker.get(r["ticker"] or "") or by_name.get((r["stock"] or "").lower())
        at = parse_ts(r["created_at"])
        if name and at:
            by.setdefault(name, []).append(dict(r, stock=name, at=at))
    return {name: targets.group(rs) for name, rs in by.items()}


def week_targets_html(gs: list) -> str:
    """주간 리포트 종목 칸의 목표가 한 줄: "목표가 5건 (상향 2 · 유지 3) · Baird 상향 $1,520 · …" (새것부터 넷)."""
    if not gs:
        return ""
    count = {}
    for g in gs:
        count[g["action"]] = count.get(g["action"], 0) + 1
    head = " · ".join(f"{a} {count[a]}" for a in targets.ACTIONS if count.get(a))
    items = " · ".join(
        f"{html.escape(g['broker'])} <b style='color:{TARGET_COLORS.get(g['action'], '#e6e6e6')}'>{g['action']}</b>"
        + (f" {money(g['pt_new'], g['currency'])}" if g["pt_new"] else "") for g in gs[:4])
    return (f"<div class=why><a href='/targets?o=name' style='text-decoration:underline'>목표가</a> {len(gs)}건 ({head}) · "
            f"{items}{' · …' if len(gs) > 4 else ''}</div>")


def targets_page(watcher: Watcher, order: str = "", days: int = 30) -> str:
    """목표가 표: 최근 days 일. 같은 조치를 여러 곳이 쓰면 한 줄 (targets.group).
    order "" 는 모든 종목을 한 표에 최신순, "name" 은 종목마다 칸을 나눠 이름 순 (영어 A→Z 다음 한국 종목).
    09-30 전하 분부로 최신순이 기본이 됐다 (전에는 이름 순이 기본, 최신순이 ?o=new)."""
    now = datetime.now(timezone.utc)
    stocks = load_stocks()
    ticker = {s["name"]: s.get("yahoo", "") for s in stocks}
    groups = by = target_groups(stocks, days)
    count = {}
    for gs in groups.values():
        for g in gs:
            count[g["action"]] = count.get(g["action"], 0) + 1
    if order != "name":
        # 종목 칸은 티커로 (10-01 전하 분부). 한국 종목(005930.KS)과 티커 없는 종목은 이름 그대로
        short = lambda name: name if not ticker.get(name) or ticker[name][0].isdigit() else ticker[name]
        flat = sorted(((g, short(name)) for name, gs in groups.items() for g in gs), key=lambda x: x[0]["at"], reverse=True)
        cards = ([f"<div class=card><table>{''.join(target_row(g, name) for g, name in flat)}</table></div>"]
                 if flat else [])
    else:
        # 종목별 표에는 증권사마다 가장 새 조치만 (10-04 전하 분부). 최신순 표는 모든 줄을 그대로 둔다
        last = {name: targets.latest_per_broker(gs) for name, gs in groups.items()}
        older = lambda name: len(groups[name]) - len(last[name])
        prices = moves.last_prices(ticker.get(name) for name in groups)   # 야후, 5분 묵혀 씀. 못 받으면 평균만 보인다
        cards = [f"<div class=card><div><b>{html.escape(name)}</b> <span class=why>{html.escape(ticker.get(name, ''))}"
                 f" · {len(last[name])}건{f' (같은 증권사의 앞선 조치 {older(name)}건 뺌)' if older(name) else ''}</span></div>"
                 f"{consensus_html(groups[name], ticker.get(name, ''), prices)}"
                 f"<table>{''.join(target_row(g) for g in last[name])}</table></div>"
                 for name in sorted(groups, key=str.lower)]
    order = "name" if order == "name" else ""
    sort = " · ".join(f"<b>{label}</b>" if order == o else f"<a class=back href='/targets{'?o=' + o if o else ''}'>{label}</a>"
                      for o, label in (("", "최신순"), ("name", "종목 이름 순")))
    checked, week = store.targets_checked(), (now - timedelta(days=7)).isoformat(timespec="seconds")
    todo = sum(1 for r in list(watcher.judged.values())
               if r["id"] not in checked and r.get("created_at", "") >= week and targets.is_candidate(r))
    empty = [s["name"] for s in stocks if s["name"] not in by]
    summary = " · ".join(f"{a} {count[a]}" for a in targets.ACTIONS if count.get(a)) or "아직 없음"
    return f"""<!doctype html><meta charset=utf-8><title>목표가 표 · 종목 뉴스 필터</title><meta name=viewport content='width=device-width,initial-scale=1'>
<style>
body{{font:var(--fs) system-ui,sans-serif;background:#16181c;color:#e6e6e6;margin:16px;max-width:1100px}}
a{{color:#e6e6e6;text-decoration:none}} a:hover{{text-decoration:underline}} .why{{color:#8a9099;font-size:.88em}}
h2{{margin:0 0 4px;font-size:1.3em}} .card{{background:#1f2228;border-radius:8px;padding:8px 12px;margin:8px 0}}
table{{border-collapse:collapse;width:100%;margin-top:4px}} td{{padding:3px 8px 3px 0;vertical-align:top;border-top:1px solid #2a2d33}}
.sort{{margin:4px 0}} td.sk{{white-space:nowrap;font-weight:600;min-width:8em}} td.d,td.br,td.ac,td.pt,td.rt,td.bl{{white-space:nowrap}} td.d{{color:#8a9099;font-size:.9em;min-width:5.5em}} td.br{{min-width:9em}} td.ac{{min-width:4.5em}} td.pt{{min-width:13em}} td.rt{{min-width:5em}} td.bl{{min-width:1.5em}} td.nw{{width:100%;font-size:.9em}}
.pct{{color:#8a9099;font-size:.9em}} ul{{margin:0;padding-left:18px}} ul.one{{list-style:none;padding:0}}
summary{{cursor:pointer;color:#8ab4f8}} .src{{display:inline-block;margin-right:6px;padding:0 5px;border-radius:4px;background:#2a2d33;color:#b8bec6;font-size:.8em}}
.back{{color:#8ab4f8}} .cs{{margin:2px 0}}
html.nar body{{max-width:660px;margin:8px}} html.nar .card{{padding:6px 8px}}
html.nar table,html.nar table tbody{{display:block}}
html.nar table tr{{display:grid;grid-template-columns:6.2em 9.5em 4.6em 1fr 4.6em;column-gap:8px;padding:5px 0;border-top:1px solid #2a2d33}}
html.nar table tr:has(td.sk){{grid-template-columns:6.2em 5.6em 9.5em 4.6em 1fr 4.6em}}
html.nar td{{border:0;padding:0;min-width:0!important;white-space:normal}} html.nar td.d{{white-space:nowrap}}
html.nar td.nw{{grid-column:1/-1;width:auto;padding:2px 0 0}} html.nar td.bl{{display:none}}
#tgw,#cpy{{color:#8ab4f8;cursor:pointer;margin-left:10px}} tr[hidden],.card[hidden]{{display:none!important}} #cpyn,#tgd{{background:#2a2d33;color:#e6e6e6;border:1px solid #3a3f47;border-radius:4px;font:inherit}}
</style>
<script>
// 좁게 보기가 기본이다 (캡처해서 카톡으로 보내면 휴대폰으로 본다). "넓게" 를 고르면 기억한다
function tgw(){{try{{return localStorage.getItem("tgw")}}catch(e){{return null}}}}
if(tgw()!=="wide"||innerWidth<700)document.documentElement.classList.add("nar");
addEventListener("DOMContentLoaded",()=>{{
  const a=document.getElementById("tgw"),nar=()=>document.documentElement.classList.contains("nar");
  const show=()=>{{a.textContent=nar()?"넓게 보기":"좁게 보기"}};
  a.onclick=()=>{{document.documentElement.classList.toggle("nar");try{{localStorage.setItem("tgw",nar()?"narrow":"wide")}}catch(e){{}};show()}};
  show();
}});
</script>
{settings.FS_BAR}
<p class=why><a class=back href='/'>← 판별 목록</a></p>
<h2>목표가 표</h2>
{COPY_IMG}
{TG_FILTER}
<p class=sort>정렬: {sort} <a id=tgw></a> <a id=cpy>그림으로 복사</a> <select id=cpyn><option value=20>위 20줄<option value=40>위 40줄<option value=0>전체</select> <span id=cpymsg class=why></span></p>
<p class=sort>보기: <select id=tgd><option value=0>30일 전체<option value=1>오늘<option value=3>3일<option value=7>7일</select> <label><input type=checkbox id=tgk> 유지·제시 빼기</label> <span id=tgc class=why></span></p>
<p class=why>최근 {days}일 · {summary} · 판별 모델(Ollama, 꺼져 있으면 Claude)이 뉴스 제목에서 증권사·목표가를 뽑았다. 틀릴 수 있으니 기사로 확인할 것.
같은 종목·증권사의 조치를 이틀 안에 여러 곳이 쓰면 한 줄로 합쳤다 (목표가가 다르면 따로, 구분은 가장 많이 나온 것). "제시" 는 목표가만 적혀 있고 올렸는지·내렸는지 제목으로 알 수 없는 것.{" 종목 이름 순 표에는 증권사마다 가장 새 조치만 보인다 (앞선 조치는 최신순 표에). 새 조치의 제목에 목표가가 없으면 그 증권사의 앞선 목표가를 적었다. 평균 목표가는 증권사마다 가장 새 목표가 하나씩의 평균이고, 괴리율은 평균 목표가가 현재가(야후, 장 전·장 뒤 포함)보다 얼마나 높은가다." if order == "name" else ""}{f" 아직 뽑지 않은 후보 {todo}건." if todo > 0 else ""}</p>
{''.join(cards) or "<p>아직 뽑은 목표가가 없다.</p>"}
<p class=why>목표가 소식 없음: {html.escape(", ".join(empty)) or "없음"}</p>"""


def spoken_when(at: datetime, now: datetime) -> str:
    """읽기 좋은 때: '내일 새벽 5시', '오늘 밤 9시 30분'."""
    day = {0: "오늘", 1: "내일", 2: "모레"}.get((at.date() - now.date()).days, f"{at.month}월 {at.day}일")
    h = at.hour
    part = "새벽" if h < 6 else "아침" if h < 12 else "오후" if h < 18 else "밤"
    h12 = h if h <= 12 else h - 12
    return f"{day} {part} {h12}시" + (f" {at.minute}분" if at.minute else "")


RECENT_ALERT_HOURS = 6
RECENT_ALERT_MAX = 5


def recent_alerts_html(watcher: Watcher) -> str:
    """목록 위 '최근 알림': 알림을 보낸 뉴스를 알린 순서대로. 목록은 기사 시각 순이라,
    늦게 들어와 알린 뉴스는 목록 아래에 묻힌다 (PC 가 잠들었다 깼을 때, 구글이 늦게 올린 기사)."""
    cutoff = (datetime.now(KST) - timedelta(hours=RECENT_ALERT_HOURS)).isoformat(timespec="seconds")
    recs = sorted((r for r in watcher.judged.values() if r.get("alerted") and r.get("at", "") >= cutoff),
                  key=lambda r: r["at"], reverse=True)[:RECENT_ALERT_MAX]
    if not recs:
        return "<div id=recent></div>"
    rows = []
    for r in recs:
        at = datetime.fromisoformat(r["at"]).astimezone(KST)
        src = SOURCE_NAMES.get(r.get("source", ""), r.get("source", ""))
        rows.append(f"<div class=ra><span class=why>{at:%H:%M}</span> <b>{r['score']}</b> "
                    f"<a href=\"{html.escape(r['url'])}\" target=_blank>{html.escape(r.get('title_ko') or r['title'])}</a>"
                    f" <span class=why>{html.escape(src)}</span></div>")
    return (f"<div id=recent><div class=why>최근 알림 ({RECENT_ALERT_HOURS}시간 안, 알린 순서)</div>"
            + "".join(rows) + "</div>")


def moves_html(watcher: Watcher, stock: str = "") -> str:
    """목록 위 급등락 칸: 최근 6시간 것만, 새것부터. 원인일 만한 뉴스를 밑에 붙인다.
    종목별 보기(stock)에서는 그 종목의 지난 급등락을 모두 (최근 20번)."""
    if stock:
        items = store.read_moves(stock, 20)
        title = f"{html.escape(stock)} 급등락 기록 ({len(items)}번" + (", 최근 20번까지" if len(items) >= 20 else "") + ")"
        if not items:
            return f"<div id=moves><div class=why>{html.escape(stock)} 급등락 기록 없음</div></div>"
    else:
        cutoff = datetime.now(KST) - timedelta(hours=6)
        items = [m for m in watcher.moves if datetime.fromisoformat(m["at"]) >= cutoff]
        title = "급등락 (최근 6시간)"
        if not items:
            return "<div id=moves></div>"
    rows, brief = [], []
    for m in items:
        at = datetime.fromisoformat(m["at"])
        color = "#e06c6c" if m["change"] > 0 else "#6c9be0"   # 한국식: 오르면 빨강, 내리면 파랑
        brief.append(f"<span style='color:{color}'>{html.escape(m['ticker'])} {m['change']:+.1f}%</span>")
        news ="".join(f"<div class=mvn>{x['score']}점 <a href=\"{html.escape(x['url'])}\" target=_blank>"
                       f"{html.escape(x['title'])}</a></div>" for x in m["news"]) or \
            "<div class=mvn>최근 2시간 안에 이 종목 뉴스가 없다</div>"
        rows.append(f"<div class=mv><b style='color:{color}'>{html.escape(m['ticker'])} 15분 {m['change']:+.1f}%</b> "
                    f"<span class=why>{at:%m-%d %H:%M} · {m['price']:.2f} · {html.escape(m['name'])}</span>{news}</div>")
    # 접을 수 있다. 접힌 줄에는 종목과 등락률만 보인다. 접은 상태는 이 브라우저에 기억한다 (기본은 편 상태)
    return (f"<div id=moves><details id=mvd open><summary class=why>{title}"
            f"<span class=mvs> · {' · '.join(brief)}</span></summary>" + "".join(rows) + "</details></div>")


def earnings_line() -> str:
    """목록 위 한 줄: 60일 안의 실적 발표 (한 번의 실적 시즌), 한국 시각. 사흘 안이면 주황."""
    items = earnings.upcoming(60)
    if not items:
        return ""
    parts = []
    for t, at, left, item in items:
        sure = item.get("confirmed")
        tip = {True: "회사가 공지한 날짜 (나스닥)",
               False: f"추정: 회사가 아직 공지하지 않아 지난 발표일로 어림잡은 날짜. 나스닥 추정은 {item.get('nasdaq_day', '')[5:]} (미국)",
               None: "나스닥에 날짜가 없다 (한국 종목이거나 아직 안 올라옴) — 야후 날짜"}[sure]
        cls = " ".join(c for c in ("warn" if left <= 3 else "", "guess" if sure is False else "") if c)
        parts.append(
            f"<span{f' class={chr(39)}{cls}{chr(39)}' if cls else ''} title='{tip}'>{html.escape(t)} "
            f"{at:%m-%d}({'월화수목금토일'[at.weekday()]}) {at:%H:%M}{'께' if item.get('approx') else ''}"
            f"{' ' + item['session'] if item.get('session') else ''}"
            f"{' 확정' if sure else ' 추정' if sure is False else ''} {'오늘' if left == 0 else f'D-{left}'}</span>")
    # 접어 둔다. 접힌 줄에는 가장 가까운 한 종목만 보인다. 편 상태는 이 브라우저에 기억한다
    t, at, left, _ = items[0]
    near = f"{html.escape(t)} {at:%m-%d} {'오늘' if left == 0 else f'D-{left}'}"
    return (f"<details class=why id=earn><summary>실적 발표 {len(items)}종목 · 가장 가까운 "
            f"<span{' class=warn' if left <= 3 else ''}>{near}</span></summary>"
            "한국 시각: " + " · ".join(parts) + "</details>")


def page_html(watcher: Watcher, rows: list, note: str, show_all: bool, low: int, hidden: int, n_fb: int,
              stock: str = "", counts: dict = None) -> str:
    sq = f"&s={quote(stock)}" if stock else ""
    # 모두 보기/숨기기 단추는 줄 맨 앞에 둔다 (설명 글 끝에 있으면 찾기 힘들다)
    note += ("<p class=why><a class=tog href='/?" + sq[1:] + "'>숨기기</a> 숨긴 뉴스까지 모두 보는 중</p>" if show_all else
             f"<p class=why><a class=tog href='/?all=1{sq}'>모두 보기</a> "
             f"👎·🔕0 준 뉴스{f', {low}점 이하 뉴스' if low >= 0 else ''}, 옛 기사, 늦게 들어온 구글 기사"
             f"{', 가린 언론사' if watcher.cfg.get('hide_sources') else ''} {hidden}건은 숨겼습니다.</p>")
    note += "<p class=why>🔔10 👍 👎 🔕0 가운데 누른 것에 불이 켜집니다. 🔔10 은 '반드시 알려라', 🔕0 은 '절대 알리지 마라'로 👍/👎 보다 강하게 반영됩니다. 같은 버튼을 다시 누르면 취소됩니다.</p>"
    # 종목별 보기 띠는 종목 줄 바로 밑에 둔다 (실적·급등락·최근 알림 칸 아래에 두면 내려가야 보인다)
    home = "/" + ("?all=1" if show_all else "")
    filt = (f"<p class=filt><b>{html.escape(stock)}</b> 뉴스만 보는 중 "
            f"<a class=unfilt href='{home}'>✕ 모든 종목 보기</a></p>") if stock else ""
    stocks = load_stocks()
    counts, th = counts or {}, watcher.cfg["threshold"]

    def count(name):
        n, top = counts.get(name, (0, 0))
        return (f" <small class=cnt title='최근 24시간 목록에 보이는 뉴스 {n}건, 최고 {top}점'>{n}"
                f"<b{' class=hi' if top >= th else ''}>·{top}</b></small>") if n else ""

    def link(name):   # 고른 종목을 다시 누르면 모든 종목으로 돌아간다
        if name == stock:
            return f"<a class='sname on' href='{home}' title='다시 누르면 모든 종목'>{html.escape(name)} ✕{count(name)}</a>"
        return f"<a class=sname href='/?s={quote(name)}'>{html.escape(name)}{count(name)}</a>"

    names = " · ".join(link(x["name"]) for x in stocks) or "없음 (⚙ 설정에서 추가)"
    menu = settings.menu(dict(watcher.cfg, _tg_ready=tg_ready()), stocks)
    return f"""<!doctype html><meta charset=utf-8><title>구글·야후 뉴스 필터링</title>
<style>{settings.CSS}
body{{font:var(--fs) system-ui,sans-serif;background:#16181c;color:#e6e6e6;margin:16px}}
table{{border-collapse:collapse;width:100%}} td{{padding:6px 8px;border-bottom:1px solid #2a2d33;vertical-align:top}}
a{{color:#e6e6e6;text-decoration:none}} .s{{text-align:right;font-weight:600}} .why{{color:#8a9099;font-size:.86em}}
.b,.t,.s{{width:1%;white-space:nowrap}} a.fb{{display:inline-block;margin-right:4px;padding:2px 5px;border-radius:6px;font-size:1.14em;opacity:.3;filter:grayscale(1)}} a.fb:hover{{opacity:.8}} a.fb.num{{font-weight:700;font-size:.93em;white-space:nowrap;text-align:center;color:#fff;background:#2a2d33}} a.fb.on{{opacity:1;filter:none;background:#3a4a6b;outline:1px solid #6d8fd6}} tr.hit{{background:#1d2a45}} tr.done{{background:#2a3d23}} a.rated{{color:#8a9099}} #list a[target=_blank]:not(.rated):visited{{color:#aab0b8}} .ok{{color:#8fd18f}} .warn{{color:#e0a44a;font-size:.93em}} .warn a{{color:#e0a44a;text-decoration:underline}} .src{{display:inline-block;margin-right:6px;padding:0 5px;border-radius:4px;background:#2a2d33;color:#b8bec6;font-size:.79em}} a.src{{cursor:pointer}} a.src:hover{{background:#4a2f33;color:#e6e6e6}} a.src.off{{text-decoration:line-through;opacity:.7}} .stk{{display:inline-block;margin-right:6px;padding:0 5px;border-radius:4px;background:#23382c;color:#9fd8b0;font-size:.79em}} a.stk:hover{{background:#2e4a3a}} a.sname{{color:#8a9099}} .cnt{{color:#6f7680}} .cnt b{{font-weight:400}} .cnt b.hi{{color:#e0a44a;font-weight:600}} a.sname:hover,a.sname.on{{color:#9fd8b0}} .filt{{margin:6px 0;padding:6px 10px;background:#23382c;border-radius:6px;color:#9fd8b0}} a.tog{{display:inline-block;margin-right:8px;padding:1px 10px;border-radius:6px;background:#2a2d33;color:#e6e6e6;border:1px solid #3a3f47}} a.tog:hover{{background:#3a3f47}} .filt a.unfilt{{margin-left:10px;padding:2px 10px;border-radius:6px;background:#2a2d33;color:#e6e6e6;border:1px solid #3a3f47}} .filt a.unfilt:hover{{background:#3a3f47}} .tp{{display:inline-block;margin-right:6px;padding:0 5px;border-radius:4px;background:#2d2640;color:#c9b8ef;font-size:.79em}} .by{{font-size:.86em;font-weight:400;opacity:.75;margin-top:2px}} .by.cl{{color:#d97757}} .reset{{margin-top:24px}} .reset a{{color:#e0a44a;text-decoration:underline;cursor:pointer}} a.grp{{margin-left:8px;color:#8ab4f8;cursor:pointer;text-decoration:underline}} tr.new td.t::before{{content:"● ";color:#8ab4f8}} .whypick{{margin-top:3px;font-size:.86em;color:#d9918f}} .whypick a.whyc{{display:inline-block;margin:0 4px 2px 0;padding:0 7px;border-radius:10px;background:#3a2a2c;color:#e6c3c1;cursor:pointer}} .whypick a.whyc:hover{{background:#5a3a3d}} .whyset{{margin-top:2px;font-size:.8em;color:#8a9099}} tr.child{{display:none}} tr.child.show{{display:table-row}} tr.child td{{background:#1b1e23}} tr.child td:nth-child(4){{padding-left:56px}}
.old{{color:#e0a44a;font-size:.8em}} #earn .guess{{opacity:.55}} #moves .mv{{margin:4px 0 8px;padding:6px 10px;background:#1f2228;border-radius:6px}}
#moves .mvn{{font-size:.9em;margin:2px 0 0 12px}} #moves a{{color:#e6e6e6}} #mvd summary{{cursor:pointer}} #mvd[open] .mvs{{display:none}}
#recent{{margin:6px 0 10px;padding:6px 10px;background:#1d2a45;border-radius:6px}} #recent:empty{{display:none}} .pro{{color:#8fc79a}} .con{{color:#d9918f;margin-left:4px}} #upd:empty{{display:none}}
#recent .ra{{margin:2px 0}} #recent a{{color:#e6e6e6}} #earn{{margin:6px 0}} #earn summary{{cursor:pointer}} .sum{{display:none;color:#b8bec6;font-size:.9em;line-height:1.45;margin:2px 0 3px}} .sum.show{{display:block}} a.sumbtn,a.sumget{{margin-left:8px;color:#8ab4f8;cursor:pointer;text-decoration:underline}} a.sumget{{color:#8a9099}} h2{{margin:0;font-size:1.4em}} h2 small{{font-size:.65em;font-weight:400}}
</style>
<header><h2>구글·야후 뉴스 필터링 <small style="color:#8a9099">기준 {watcher.cfg['threshold']}점 · 파란 줄은 알림을 보낸 뉴스 · 점수 밑 🦙 Ollama / <span style="color:#d97757">✴</span> Claude 가 판별</small></h2>
  <span style="margin-left:auto"></span>
  {settings.tts_switch(watcher.cfg)}
  <span class=fsz><button class=hbtn id=fsdown title="글자 작게" aria-label="글자 작게">가-</button><button class=hbtn id=fsup title="글자 크게" aria-label="글자 크게">가+</button></span>
  <button class=hbtn id=setbtn title="종목·알림·소리 설정" aria-expanded=false>⚙ 설정</button>
</header>
{menu}
<p class=why id=stockline><a href='/sources' style='text-decoration:underline'>판별·언론사 성적표</a> · <a href='/week' style='text-decoration:underline'>주간 리포트</a> · <a href='/targets' style='text-decoration:underline'>목표가 표</a> · <a href='/react' style='text-decoration:underline'>알림 뒤 주가</a> · 종목 {names} · 구글 뉴스·야후 파이낸스에서 {watcher.cfg['fetch_min']}분마다 받습니다 · 마지막 수집 {watcher.fetch_note} · {watcher.summarizer.status()}</p>
{filt}
{earnings_line()}
{moves_html(watcher, stock)}
{recent_alerts_html(watcher)}
{note}<p class=why id=upd></p><table id=list>{''.join(rows)}</table>
<p class="why reset">처음부터 다시 ·
  <a id=reset-feedback data-n="{n_fb}">반응 기록 지우기 ({n_fb}건)</a> ·
  <a id=reset-all data-n="{n_fb}" data-j="{len(watcher.judged)}">반응과 판별 기록 모두 지우기 ({n_fb}건 · {len(watcher.judged)}건)</a>
  — 지운 기록은 DB 안에 백업 표로 남는다</p>
<script>{settings.JS}</script>
<script>
// 처음부터 다시: 지우기 전에 반드시 한 번 더 묻는다
async function resetRecords(what, msg) {{
  if (!confirm(msg + "\\n\\n정말 지우시겠습니까? (기록은 DB 안의 백업 표로 옮겨져 되살릴 수 있습니다)")) return;
  const r = await fetch("/reset?what=" + what, {{method: "POST", headers: {{"X-Reset": "yes"}}}});
  const d = r.ok ? await r.json() : null;
  alert(d ? "지웠습니다. 백업: " + (d.moved.join(", ") || "(지울 기록 없음)") : "지우지 못했습니다 (" + r.status + ")");
  location.href = "/";
}}
document.getElementById("reset-feedback").onclick = (e) => resetRecords("feedback",
  "🔔10·👍·👎·🔕0 반응 기록 " + e.target.dataset.n + "건을 지웁니다.\\n판별기는 관심사(interests.md)만 보고 처음부터 다시 배웁니다.");
document.getElementById("reset-all").onclick = (e) => resetRecords("all",
  "반응 기록 " + e.target.dataset.n + "건과 판별 기록 " + e.target.dataset.j + "건을 모두 지웁니다.\\n목록이 비고, 최근 60분 안의 뉴스는 다시 판별합니다.");

// 15초마다 목록만 바꿔 끼운다. 스크롤 위치는 그대로 남는다.
// 방금 누른 뉴스는 10초 동안 목록에 남긴다 (👎 해도 바로 사라지지 않게, 잘못 누르면 되돌릴 수 있게)
let keep = null, keepAt = 0;
let limit = {PAGE_SIZE};   // '더 보기' 를 누를 때마다 늘어난다. 자동 갱신도 이만큼 받는다
async function refresh() {{
  if (keep && Date.now() - keepAt > 30000) keep = null;   // 👎 뒤 까닭을 고를 틈으로 30초
  const params = new URLSearchParams({json.dumps(dict([("all", "1")] * show_all + [("s", stock)] * bool(stock)))});
  if (limit > {PAGE_SIZE}) params.set("n", limit);
  if (keep) params.set("done", keep);
  try {{
    const r = await fetch("/?" + params, {{cache: "no-store"}});
    const doc = new DOMParser().parseFromString(await r.text(), "text/html");
    document.getElementById("list").innerHTML = doc.getElementById("list").innerHTML;
    for (const id of ["moves", "recent", "stockline"]) {{
      const src = doc.getElementById(id), dst = document.getElementById(id);
      if (src && dst) dst.innerHTML = src.innerHTML;
    }}
    applyOpen();
    applyMoves();
    document.getElementById("upd").textContent = "";   // 갱신 시각은 보이지 않는다. 연결이 끊겼을 때만 적는다
  }} catch (e) {{
    document.getElementById("upd").textContent = "판별기에 연결할 수 없습니다 (" + new Date().toLocaleTimeString("ko-KR", {{hour12: false}}) + ")";
  }}
}}
setInterval(refresh, 15000);

// 실적 발표 줄: 편 채로 두었으면 다음에도 펴 둔다 (이 브라우저에만 기억)
const earn = document.getElementById("earn");
if (earn) {{
  try {{ earn.open = localStorage.getItem("earnOpen") === "1"; }} catch (e) {{}}
  earn.addEventListener("toggle", () => {{ try {{ localStorage.setItem("earnOpen", earn.open ? "1" : "0"); }} catch (e) {{}} }});
}}

// 급등락 칸: 접어 두었으면 다음에도 접어 둔다. 15초마다 칸을 바꿔 끼우므로 그때마다 다시 맞춘다
function applyMoves() {{
  const d = document.getElementById("mvd");
  if (!d) return;
  try {{ d.open = localStorage.getItem("movesOpen") !== "0"; }} catch (e) {{}}
  d.addEventListener("toggle", () => {{ try {{ localStorage.setItem("movesOpen", d.open ? "1" : "0"); }} catch (e) {{}} }});
}}
applyMoves();

// 새 알림은 화면에 먼저 띄우고, 그다음 서버가 읽는다. 판 번호가 바뀌면 곧바로 다시 받는다.
let ver = "{watcher.ver}";
setInterval(async () => {{
  try {{
    const v = await (await fetch("/ver", {{cache: "no-store"}})).text();
    if (v !== ver) {{ ver = v; refresh(); }}
  }} catch (e) {{}}
}}, 2000);

// 지난번에 이 목록을 본 뒤 판별된 뉴스는 시각 앞에 파란 점. 기준은 이 브라우저에 기억한다
// (창을 닫거나 다른 탭으로 갈 때 그때 보이던 가장 새 판별 시각을 적는다). 처음 여는 브라우저에서는 점이 없다
let seenBefore = null;
try {{ seenBefore = localStorage.getItem("seenAt"); }} catch (e) {{}}
function markNew() {{
  if (!seenBefore) return;
  document.querySelectorAll("#list tr[data-at]").forEach((tr) => tr.classList.toggle("new", tr.dataset.at > seenBefore));
}}
function saveSeen() {{
  const last = [...document.querySelectorAll("#list tr[data-at]")].map((tr) => tr.dataset.at).filter(Boolean).sort().pop();
  if (last && (!seenBefore || last > seenBefore)) try {{ localStorage.setItem("seenAt", last); }} catch (e) {{}}
}}
document.addEventListener("visibilitychange", () => {{ if (document.hidden) saveSeen(); }});
window.addEventListener("pagehide", saveSeen);
markNew();

// 같은 사건 묶음과 요약: 펼친 것은 자동 갱신 뒤에도 펼친 채로 둔다
const opened = new Set(), openedSum = new Set();
function applyOpen() {{
  markNew();
  document.querySelectorAll("div.sum").forEach((d) => d.classList.toggle("show", openedSum.has(d.dataset.id)));
  document.querySelectorAll("a.sumbtn").forEach((a) => {{
    a.textContent = openedSum.has(a.dataset.id) ? "요약 ▴" : "요약 ▾";
  }});
  document.querySelectorAll("tr.child").forEach((tr) => {{
    const g = [...tr.classList].find((c) => c.startsWith("g-"));
    tr.classList.toggle("show", opened.has(g.slice(2)));
  }});
  document.querySelectorAll("a.grp").forEach((a) => {{
    a.textContent = a.textContent.replace(/[▾▴]$/, opened.has(a.dataset.g) ? "▴" : "▾");
  }});
}}

// 👍/👎 는 페이지를 옮기지 않고 기록한다. 그래서 스크롤 위치가 그대로 남는다.
document.getElementById("list").addEventListener("click", async (e) => {{
  const wc = e.target.closest("a.whyc");
  if (wc) {{   // 👎·🔕0 을 누른 까닭
    await fetch("/fbwhy?" + new URLSearchParams({{id: wc.dataset.id, why: wc.dataset.why}}), {{cache: "no-store"}});
    keep = wc.dataset.id;
    keepAt = Date.now();
    await refresh();
    return;
  }}
  const badge = e.target.closest("a.src");
  if (badge) {{   // 언론사 이름표: 목록에서 가리기 / 되살리기 (가리면 알림도 안 낸다)
    const off = badge.dataset.off === "1";
    if (!confirm(badge.dataset.src + (off ? " 를 목록에 되살릴까요?" : " 를 목록에서 가릴까요? (판별·알림은 그대로, 성적표에서 되살릴 수 있다)"))) return;
    const r = await fetch("/hide-source", {{method: "POST", headers: {{"X-Settings": "yes", "Content-Type": "application/json"}},
      body: JSON.stringify({{source: badge.dataset.src, hide: !off}})}});
    if (!r.ok) {{ alert("바꾸지 못했습니다 (" + r.status + ")"); return; }}
    await refresh();
    return;
  }}
  const sg = e.target.closest("a.sumget");
  if (sg) {{
    if (sg.dataset.busy) return;
    sg.dataset.busy = "1";
    sg.textContent = "요약 받는 중…";
    try {{
      const r = await fetch("/summarize-one", {{method: "POST",
        headers: {{"X-Settings": "yes", "Content-Type": "application/json"}}, body: JSON.stringify({{id: sg.dataset.id}})}});
      const d = await r.json();
      if (d.ok) {{ openedSum.add(sg.dataset.id); await refresh(); return; }}
      sg.textContent = "요약 못 함: " + (d.msg || r.status);
    }} catch (err) {{
      sg.textContent = "요약 못 함";
    }}
    delete sg.dataset.busy;
    return;
  }}
  const sb = e.target.closest("a.sumbtn");
  if (sb) {{
    openedSum.has(sb.dataset.id) ? openedSum.delete(sb.dataset.id) : openedSum.add(sb.dataset.id);
    applyOpen();
    return;
  }}
  if (e.target.closest("#more")) {{
    limit += {PAGE_SIZE};
    e.target.textContent = "불러오는 중…";
    await refresh();
    return;
  }}
  const g = e.target.closest("a.grp");
  if (g) {{
    opened.has(g.dataset.g) ? opened.delete(g.dataset.g) : opened.add(g.dataset.g);
    applyOpen();
    return;
  }}
  const a = e.target.closest("a.fb");
  if (!a) return;
  e.preventDefault();
  const url = new URL(a.href);
  try {{
    const r = await fetch(url.pathname + url.search + "&ajax=1", {{cache: "no-store"}});
    if (!r.ok) throw new Error(r.status);
    keep = url.searchParams.get("id");
    keepAt = Date.now();
    await refresh();
  }} catch (err) {{
    location.href = a.href;   // 스크립트로 안 되면 예전처럼 페이지 이동
  }}
}});
</script>"""


class Server(ThreadingHTTPServer):
    # HTTPServer 는 SO_REUSEADDR 를 켠다. 윈도우에서는 그러면 두 번째 실행도 같은 포트를 잡아
    # "이미 실행 중" 검사가 통하지 않고 알림이 두 번 온다 (시작프로그램 + 손으로 켠 것).
    allow_reuse_address = False


def main():
    ap = argparse.ArgumentParser(description="종목 뉴스 필터")
    ap.add_argument("--test", type=int, metavar="N", help="최근 N건만 판별해 출력하고 끝낸다")
    ap.add_argument("--before", metavar="TIME", help="--test 에서 이 한국 시각까지의 뉴스만 (예: '2026-09-24 23:50')")
    ap.add_argument("--say", metavar="TEXT", help="음성 알림을 한 번 내 보고 끝낸다")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = load_config()
    DATA.mkdir(exist_ok=True)   # claude CLI 를 이 폴더에서 부른다

    if args.say:
        t0 = time.time()
        by = speak(cfg, args.say)
        print(f"{by or '못 읽음'} · {time.time() - t0:.1f}초")
        return

    if args.test:
        store.migrate(log)
        log(f"RSS 새 뉴스 {fetch_news(cfg)}건")
        rows = sorted(read_news(), key=lambda r: r["ts"])
        if args.before:   # 예: "2026-09-24 23:50" (한국 시각)
            until = datetime.fromisoformat(args.before).replace(tzinfo=KST)
            rows = [r for r in rows if r["ts"] <= until]
        rows = rows[-args.test:]
        if not rows:
            sys.exit(f"{DATA} 에 오늘·어제 CSV 가 없다.")
        topics = []
        for k in range(0, len(rows), cfg["batch"]):
            batch = rows[k:k + cfg["batch"]]
            t0 = time.time()
            res, by = judge(cfg, batch, topics)
            log(f"{len(batch)}건 판별 {time.time() - t0:.1f}초 ({by})")
            for r in batch:
                score, reason, topic, say, ko, summ, con = res.get(r["id"], (None, "(응답 없음)", "", "", "", "", ""))
                mark = "🔔" if score is not None and score >= cfg["threshold"] else "  "
                print(f"{mark} {score if score is not None else '-':>2} [{topic}] {r['title'][:70]}  — ＋{reason} －{con}  🗣 {say}")
                if topic and topic not in topics:
                    topics.insert(0, topic)
        return

    store.migrate(log)
    watcher = Watcher(cfg)
    try:
        server = Server(("127.0.0.1", cfg["port"]), make_handler(watcher))
    except OSError as e:
        # 이미 하나 떠 있는 경우가 대부분이다. 종료 코드 3 이면 stock_alert.bat 이 다시 띄우지 않는다.
        log(f"포트 {cfg['port']} 를 쓸 수 없다 ({e}). 이미 실행 중이거나, {CONFIG.name} 의 port 를 바꿔라.")
        sys.exit(3)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=_speech_worker, args=(cfg,), daemon=True).start()
    log(f"판별 목록: http://127.0.0.1:{cfg['port']}/")
    try:
        watcher.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
