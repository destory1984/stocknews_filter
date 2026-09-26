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
    # 이보다 오래된 뉴스는 알리지 않는다 (판별은 한다).
    # 구글 뉴스는 기사가 나온 뒤 늦게 잡히기도 해서 saveticker(60분)보다 길게 둔다.
    "max_age_min": 120,
    # 같은 사건은 이 시간 안에 한 번만 알린다. 같은 종목이고 사건 이름의 낱말(회사 이름 뒤)이 겹치면 같은 사건으로 본다.
    # 1시간·같은 이름일 때 09-26 하루 35번 (마이크론 실적 예고만 10번 가까이) → 12시간·낱말 겹침으로 되돌려 보니 21번
    "topic_hours": 12,
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
    "hide_sources": [],            # 판별 목록에서 가릴 언론사 (언론사 성적표에서 고른다). 판별·알림은 그대로 한다
    "hide_max_score": 3,           # 판별 목록에서 이 점수 이하는 기본으로 숨긴다 (👍·🔔10 준 것은 보인다)
    "tts": True,                   # 알림을 말로도 읽는다: 말머리 소리 → "언론사, 제목" (영어 제목은 번역한 것)
    "tts_voice": "ko-KR-SunHiNeural",   # Edge 읽어주기 음성. 안 되면 윈도우 기본 음성(SAPI)
    "tts_voice_en": "en-US-JennyNeural",  # 영어 언론사 이름("The Motley Fool, …")을 읽는 음성 (여). 비우면 한국어 음성이 다 읽는다
    "tts_rate": "+0%",
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
[과거 반응]의 🔔 는 "이런 뉴스는 반드시 알려라", 🔕 는 "이런 뉴스는 절대 알리지 마라"는 강한 표시다.
🔔 와 같은 종류의 뉴스는 9~10점, 🔕 와 같은 종류는 0~1점을 줘라. 이것이 👍/👎 와 관심사보다 우선한다.

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

[새 뉴스]
{news}

JSON 만 출력하라. 다른 말은 쓰지 마라.
{{"results": [{{"i": 번호, "score": 0~10 정수, "reason": "왜 관심 있을지 15자 이내 한국어", "topic": "사건 이름", "say": "제목을 소리내 읽기 좋게 12자 안팎으로 줄인 말. 예: 이란 휴전안 거부", "ko": "제목이 영어면 자연스러운 한국어 제목으로 번역, 한국어 제목이면 빈 문자열", "sum": "[설명] 이 있으면 그 내용을 한국어 1~2문장으로 요약, 없으면 빈 문자열"}}]}}
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


def fetch_news(cfg: dict) -> int:
    """구글 뉴스·야후 RSS 에서 종목 뉴스를 받아 DB 에 넣는다. 새로 넣은 건수를 돌려준다.
    id 는 링크의 해시. 두 종목에 함께 걸린 기사는 한 줄로 두고 tickers 에 둘 다 적는다.
    구글과 야후가 같은 기사를 다른 링크로 주는 일이 있어, 최근 이틀 안에 같은 제목이 있으면 넣지 않는다."""
    try:
        stocks = stocknews.load_watchlist()
    except SystemExit as e:
        log(str(e))
        return 0
    rows = stocknews.collect(stocks, cfg["lookback_days"], ["google", "yahoo"],
                             on_error=lambda m: log(f"수집 실패 {m}"))
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


def examples_text(n: int) -> str:
    recs = sorted(latest_feedback().values(), key=lambda x: x.get("at", ""), reverse=True)
    groups = {k: [r["title"] for r in recs if fb_key(r) == k] for k in FB_VALUES}
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


def parse_results(text: str, batch: list) -> dict:
    """{id: (score, reason, topic, say, ko, sum)}. 모델이 빠뜨린 뉴스는 결과에 없다.
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
                                       str(it.get("sum", "") or "").strip() if batch[i - 1].get("summary") else "")
    return out


def judge(cfg: dict, batch: list, topics: list = ()) -> tuple:
    """({id: (score, reason, topic, say, ko, sum)}, 판별한 쪽 이름). topics 는 최근에 붙인 사건 이름."""
    prompt = PROMPT.format(
        interests=INTERESTS.read_text(encoding="utf-8") if INTERESTS.exists() else "(없음)",
        examples=examples_text(cfg["examples"]),
        topics="\n".join(topics) or "(없음)",
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


def _speak_edge(cfg: dict, text: str):
    import asyncio
    import edge_tts
    parts = voice_parts(cfg, text)
    files = []
    for _ in parts:
        fd, tmp = tempfile.mkstemp(prefix="news_tts_", suffix=".mp3")
        os.close(fd)
        files.append(tmp)

    async def make():   # 조각을 한꺼번에 받아 두고 이어서 튼다 (사이가 벌어지지 않게)
        await asyncio.gather(*(edge_tts.Communicate(t, v, rate=cfg["tts_rate"]).save(f)
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


def _speak_sapi(text: str):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    try:
        win32com.client.Dispatch("SAPI.SpVoice").Speak(text)
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
        _speak_sapi(text)
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
        기다리는 동안 gate 를 쥐고 있지 않는다 — 요약이 3분 기다리는 사이 알림이 막히면 안 된다."""
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
            if done or r["id"] in seen or r["ts"] < cutoff:
                continue
            seen.add(r["id"])
            out.append(r)
        return sorted(out, key=lambda r: (self.is_late(r), r["ts"]))

    def drop_junk(self, todo: list) -> list:
        """옵션 시세 페이지·소송 모집 광고 같은 것은 모델에 묻지 않고 0점으로 적는다 (stocknews.JUNK).
        목록에서는 점수가 낮아 숨고, '모두 보기' 에서 📏 로 보인다."""
        rest = []
        for r in todo:
            why = stocknews.junk_reason(r["title"])
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
                result, by = judge(self.cfg, batch, self.recent_topics())
            except Exception as e:   # 모델이 바쁘거나 꺼져 있으면 다음 차례에 다시
                log(f"판별 실패: {e}")
                return
            log(f"{len(batch)}건 판별 {time.time() - t0:.1f}초 ({by})")
            self.fill_missing_ko(batch, result)
            for r in batch:
                if r["id"] not in result:
                    continue
                score, reason, topic, say, ko, summ = result[r["id"]]
                if ko:
                    r["title_ko"] = ko   # 토스트·텔레그램이 번역 제목을 쓴다
                # 같은 사건(시진핑 발언 문장마다 뜨는 속보, 실적 예고 기사 여럿 등)은 topic_hours 에 한 번만 알린다
                late = self.is_late(r)
                alert = (score >= self.cfg["threshold"] and not late
                         and not self.topic_alerted(topic, r.get("tickers", ""))
                         and not self.is_dup(r["title"]))
                pub = None
                if alert and r.get("feed") == "google":
                    # 알리기 전에 원문 날짜를 본다. 구글이 막는 중이거나 날짜를 못 찾으면 그대로 알린다
                    try:
                        _, pub = self.summarizer.fetch_article(r, urgent=True)
                        self.summarizer.dated.add(r["id"])
                    except (article.Busy, article.Skip, requests.RequestException):
                        pass
                    if self.summarizer.is_stale(pub):
                        alert = False
                        log(f"옛 기사라 알리지 않음 (원문 {pub:%Y-%m-%d}) {ko or r['title']}"[:90])
                rec = {"id": r["id"], "title": r["title"], "url": r["url"], "source": r.get("source", ""),
                       "tickers": r.get("tickers", ""),
                       "created_at": r["created_at"], "score": score, "reason": reason, "topic": topic,
                       "say": say, "title_ko": ko, "alerted": alert, "late": late, "by": by,
                       "at": datetime.now(KST).isoformat(timespec="seconds")}
                if summ:   # 야후 설명을 판별 때 줄인 것. 구글 기사는 요약 스레드가 나중에 채운다
                    rec.update(summary_ko=summ, summary_by="rss", summary_state="done")
                self.summarizer.mark_date(rec, pub)
                self.judged[r["id"]] = rec
                store.save_judged(rec)
                mark = "🔔" if alert else "⏰" if late and score >= self.cfg["threshold"] else "  "
                log(f"{mark} {score:>2} [{topic}] {r['title'][:70]}  — {reason}")
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

    def run(self):
        log(f"감시 시작: {DATA}  (모델 {self.cfg['model']}, 기준 {self.cfg['threshold']}점, "
            f"RSS {self.cfg['fetch_min']}분마다)")
        threading.Thread(target=self.backfill_translations, daemon=True).start()
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
            elif u.path == "/ver":   # 페이지가 2초마다 묻는다. 바뀌었으면 목록을 다시 받는다
                data = str(watcher.ver).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/week":
                self.send_page(week_page(watcher))
            elif u.path == "/sources":
                self.send_page(sources_page(watcher))
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
                  "tts_voice": "목소리", "tts_voice_en": "영어 언론사 목소리", "tts_rate": "빠르기", "tts_chime": "말머리 소리", "quiet_on": "조용한 시각",
                  "tts_quiet": "조용한 시각", "telegram": "텔레그램", "fetch_min": "받는 간격",
                  "catchup_hours": "밀린 뉴스", "topic_hours": "같은 사건 알림", "backend": "판별 LLM", "claude_model": "Claude 모델",
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


def hide_source(watcher: Watcher, body: dict) -> dict:
    """언론사 성적표의 가리기/되살리기. 목록에서만 가린다 (판별·알림은 그대로)."""
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


def mb_rows(watcher: Watcher, body: dict) -> dict:
    """Edge 확장이 사용자 탭의 MarketBeat 목표가 표를 보낸 것. 새 줄이 처음 보인 시각을 기록한다 (속도 재기)."""
    rows = [r for r in body.get("rows") or [] if isinstance(r, dict) and str(r.get("id", "")).isdigit()][:1000]
    n = store.add_mb(rows, str(body.get("refreshed") or ""))
    if n:
        log(f"MarketBeat 새 줄 {n}건 (표 {len(rows)}줄, 페이지 갱신 {body.get('refreshed', '')})")
    return {"ok": True, "new": n}


SETTING_ROUTES = {"/settings": set_setting, "/say": say_test, "/telegram-test": telegram_test,
                  "/fetch": fetch_now, "/watch": watch_change, "/mb": mb_rows,
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


def sources_page(watcher: Watcher) -> str:
    """언론사 성적표: 언론사마다 건수·평균 점수·알림 대상(기준 점수 이상)·👍/👎. 가리면 목록에서만 안 보인다."""
    muted = {stocknews.source_key(x) for x in watcher.cfg.get("hide_sources") or []}
    stats = sorted(group_sources(store.source_stats()), key=lambda r: -r["n"])
    th = watcher.cfg["threshold"]
    first = min((r["first"] for r in stats if r["first"]), default="")
    rows = []
    for r in stats:
        src = r["source"]
        name = src or "(언론사 없음)"
        off = stocknews.source_key(src) in muted
        others = [x for x in r["names"] if x != src]
        rows.append(
            f"<tr{' class=off' if off else ''}><td>{html.escape(name)}"
            f"{' <small>+ ' + html.escape(', '.join(others)) + '</small>' if others else ''}</td>"
            f"<td data-v={r['n']}>{r['n']}</td><td data-v={r['avg']:.2f}>{r['avg']:.1f}</td>"
            f"<td data-v={r['hi'] or 0}>{r['hi'] or ''}</td>"
            f"<td data-v={r['up'] or 0}>{r['up'] or ''}</td><td data-v={r['down'] or 0}>{r['down'] or ''}</td>"
            f"<td><button class=hs data-src=\"{html.escape(src, quote=True)}\" data-hide={'0' if off else '1'}>"
            f"{'되살리기' if off else '가리기'}</button></td></tr>")
    return f"""<!doctype html><meta charset=utf-8><title>성적표 · 종목 뉴스 필터</title>
<style>
body{{font:15px system-ui,sans-serif;background:#16181c;color:#e6e6e6;margin:16px}}
a{{color:#8ab4f8}} h2{{margin:0 0 4px;font-size:1.3em}} .why{{color:#8a9099;font-size:.86em}}
table{{border-collapse:collapse}} th,td{{padding:4px 10px;border-bottom:1px solid #2a2d33;text-align:right}}
th:first-child,td:first-child{{text-align:left}} th{{cursor:pointer;color:#b8bec6;font-weight:600;white-space:nowrap}}
td small{{color:#8a9099}} tr.off td{{color:#6b7078}}
button.hs{{font:inherit;font-size:.85em;background:#2a2d33;color:#e6e6e6;border:1px solid #3a3f47;border-radius:6px;padding:2px 8px;cursor:pointer}}
tr.off button.hs{{background:#3a4a6b}}
h3{{margin:18px 0 4px;font-size:1.1em}} table.g td,table.g th{{cursor:default}} ul.miss{{margin:4px 0;padding-left:20px}}
ul.miss li{{margin:2px 0}} ul.miss a{{color:#e6e6e6;text-decoration:none}} ul.miss small{{color:#8a9099}}
</style>
<p class=why><a href='/'>← 판별 목록</a></p>
<h2>판별 채점</h2>
{grading_html(watcher)}
<h2 style="margin-top:22px">언론사 성적표</h2>
<p class=why>{html.escape(first[:10])} 부터 판별한 {sum(r['n'] for r in stats)}건, 언론사 {len(stats)}곳 ·
'알림' 은 {th}점 이상 · 👍·👎 는 🔔10·🔕0 포함, 뉴스마다 마지막 반응만 ·
가린 언론사는 판별 목록에서만 안 보인다 (판별·알림은 그대로, '모두 보기' 로 볼 수 있다) · 머리글을 누르면 정렬</p>
<table id=t><thead><tr><th>언론사</th><th>건수</th><th>평균 점수</th><th>알림</th><th>👍</th><th>👎</th><th>{len(muted)}곳 가림</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<script>
document.querySelectorAll("#t th").forEach((th, i) => th.onclick = () => {{
  const body = document.querySelector("#t tbody"), rows = [...body.rows];
  const key = (tr) => i === 0 ? tr.cells[0].textContent.toLowerCase() : Number(tr.cells[i].dataset.v || 0);
  const dir = th.dataset.dir === "down" ? 1 : -1;
  document.querySelectorAll("#t th").forEach((x) => delete x.dataset.dir);
  th.dataset.dir = dir === -1 ? "down" : "up";
  rows.sort((a, b) => (key(a) > key(b) ? 1 : key(a) < key(b) ? -1 : 0) * (i === 0 ? -dir : dir));
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
  b.textContent = hide ? "되살리기" : "가리기";
}});
</script>"""


PAGE_SIZE = 50    # 목록에 한 번에 보이는 뉴스 수 (같은 사건으로 접힌 것도 센다). 맨 아래 '더 보기' 를 누르면 이만큼씩 더


def tickers_of(r: dict) -> list:
    """판별 기록의 종목 이름들. 두 종목에 걸린 기사는 tickers 가 "SK하이닉스, 삼성전자" 처럼 온다."""
    return [x.strip() for x in (r.get("tickers") or "").split(",") if x.strip()]


def page(watcher: Watcher, done: str = None, show_all: bool = False, limit: int = PAGE_SIZE,
         stock: str = "") -> str:
    """판별 목록. stock 을 주면 그 종목 뉴스만 (목록의 종목 이름을 누르면 ?s=종목)."""
    fb = {k: fb_key(v) for k, v in latest_feedback().items()}
    recs = sorted(watcher.judged.values(), key=lambda r: r.get("created_at", ""), reverse=True)
    # 👎·0점 준 뉴스와 점수가 낮은 뉴스는 기본으로 숨긴다.
    # 방금 누른 것은 기록됐다는 표시를 위해, 👍·10점 준 것은 점수와 상관없이 남긴다.
    low = watcher.cfg["hide_max_score"]

    muted = {stocknews.source_key(x) for x in watcher.cfg.get("hide_sources") or []}

    def hide(r):
        if r["id"] == done or fb.get(r["id"]) in ("1", "10"):
            return False
        return (fb.get(r["id"]) in ("0", "00") or r["score"] <= low or bool(r.get("stale"))
                or stocknews.source_key(r.get("source", "")) in muted)

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
    # 같은 사건(topic)은 가장 최근 뉴스 한 줄로 접는다. 6시간 넘게 떨어지면 다른 묶음으로 본다.
    heads, members, order = {}, {}, []
    for r in recs:
        tp, t = r.get("topic"), parse_ts(r.get("created_at", ""))
        h = heads.get(tp) if tp else None
        if h and t and (parse_ts(h["created_at"]) - t) <= timedelta(hours=6):
            members[h["id"]].append(r)
        else:
            if tp:
                heads[tp] = r
            members[r["id"]] = []
            order.append(r)
    rows = []
    for head in order:
        kids = members[head["id"]]
        # 묶음 id: 사건 이름만 쓰면 6시간 넘게 떨어져 나뉜 같은 이름의 묶음이 함께 펼쳐진다.
        # 가장 오래된 뉴스를 섞는다. 새 뉴스는 위에 붙으니 자동 갱신 뒤에도 id 가 그대로다.
        gid = (hashlib.md5(f"{head.get('topic', '')}|{kids[-1]['id']}".encode()).hexdigest()[:10]
               if kids else "")
        rows.append(row_html(head, fb, done, qs, gid=gid, kids=kids))
        rows.extend(row_html(k, fb, done, qs, child_of=gid) for k in kids)
    if stock and not rows:
        rows.append(f"<tr><td colspan=4 class=why>{html.escape(stock)} 뉴스가 " + ("없습니다." if show_all else
                    f"보이는 것이 없습니다 (숨긴 {hidden}건은 '모두 보기').") + "</td></tr>")
    note = "<p class=ok>반응을 기록했습니다. 다음 판별부터 반영됩니다.</p>" if done else ""
    if more:   # 목록 표 안에 두어야 15초 자동 갱신 때 같이 바뀐다
        rows.append(f"<tr><td colspan=4 style='text-align:center'><a id=more class=grp>"
                    f"더 보기 (남은 {more}건" + (f" 가운데 {PAGE_SIZE}건" if more > PAGE_SIZE else "") + ")</a></td></tr>")
    return page_html(watcher, rows, note, show_all, low, hidden, sum(1 for v in fb.values() if v), stock, counts)


def row_html(r: dict, fb: dict, done: str, qs: str, gid: str = "", kids=(), child_of: str = "") -> str:
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
        group = (f" <a class=grp data-g='{gid}'>같은 사건 +{len(kids)}건 (최고 {top}점) ▾</a>")
    # 누가 판별했는지: 🦙 Ollama, ✴ Claude
    by = {"ollama": "<div class=by title='Ollama 가 판별'>🦙</div>",
          "claude": "<div class='by cl' title='Claude 가 판별'>✴</div>",
          "rule": "<div class=by title='모델에 묻지 않고 규칙으로 거름'>📏</div>"}.get(r.get("by"), "")
    topic = f"<span class=tp>{html.escape(r['topic'])}</span>" if r.get("topic") else ""
    source = SOURCE_NAMES.get(r.get("source", ""), r.get("source", ""))
    src = f"<span class=src>{html.escape(source)}</span>" if source else ""
    src = "".join(f"<a class=stk href='/?s={quote(t)}' title='{html.escape(t, quote=True)} 뉴스만 보기'>"
                  f"{html.escape(t)}</a>" for t in tickers_of(r)) + src
    # 누른 버튼은 불이 켜지고, 다시 누르면 취소된다.
    btns = "".join(
        f"<a class='fb{' num' if v in ('10', '00') else ''}{' on' if state == v else ''}' title='{tip}' "
        f"href='/fb?id={r['id']}&v={'x' if state == v else v}{qs}'>{label}</a>"
        for v, label, tip in (("10", "🔔10", "반드시 알려라 (10점)"), ("1", "👍", "관심"),
                              ("0", "👎", "별로"), ("00", "🔕0", "절대 알리지 마라 (0점)")))
    return (
        f"<tr{cls}><td class=b>{btns}</td>"
        f"<td class=t>{when}</td><td class=s>{r['score']}{by}</td>"
        f"<td><a{' class=rated' if state else ''} href='{html.escape(r['url'])}' target=_blank"
        f"{' title=' + chr(39) + html.escape(r['title'], quote=True) + chr(39) if r.get('title_ko') else ''}>"
        f"{html.escape(r.get('title_ko') or r['title'])}</a>"
        # 요약은 접어 둔다. '요약 ▾' 을 누르면 편다
        + (f"<div class=sum data-id='{r['id']}'>{html.escape(r['summary_ko'])}</div>" if r.get("summary_ko") else "")
        + f"<div class=why>{src}{topic}{html.escape(r.get('reason', ''))}"
        + (f" <a class=sumbtn data-id='{r['id']}'>요약 ▾</a>" if r.get("summary_ko") else
           f" <a class=sumget data-id='{r['id']}' title='원문을 받아 Ollama 로 요약한다 (구글에 한 번 묻는다)'>요약 받기</a>"
           if "news.google.com" in r.get("url", "") and not r.get("summary_state") else "")
        + f"{group}</div></td></tr>")


def week_page(watcher: Watcher) -> str:
    """주간 리포트 페이지: 지난 7일, 종목별."""
    r = watcher.weekly_report()
    now = datetime.now(KST)
    cards = []
    for st in sorted(r["stocks"], key=lambda x: (-x["alerts"], -x["news"])):
        pr = st["price"]
        move = (f"<b style='color:{'#e06c6c' if pr[2] > 0 else '#6c9be0'}'>{pr[2]:+.1f}%</b> "
                f"<span class=why>{pr[0]:.2f} → {pr[1]:.2f}</span>") if pr else "<span class=why>값 없음</span>"
        top = "".join(f"<li>{x['score']}점 <a href=\"{html.escape(x['url'])}\" target=_blank>"
                      f"{html.escape(x.get('title_ko') or x['title'])}</a></li>" for x in st["top"])
        cards.append(f"<div class=card><div><b>{html.escape(st['name'])}</b> <span class=why>{html.escape(st['ticker'])}</span> · {move}</div>"
                     f"<div class=why>뉴스 {st['news']}건 · 알림 {st['alerts']}건</div>"
                     + (f"<ul>{top}</ul>" if top else "") + "</div>")
    topics = " · ".join(f"{html.escape(t)} {n}건" for t, n in r["topics"]) or "없음"
    return f"""<!doctype html><meta charset=utf-8><title>주간 리포트 · 종목 뉴스 필터</title>
<style>
body{{font:15px system-ui,sans-serif;background:#16181c;color:#e6e6e6;margin:16px;max-width:980px}}
a{{color:#e6e6e6;text-decoration:none}} a:hover{{text-decoration:underline}} .why{{color:#8a9099;font-size:.88em}}
h2{{margin:0 0 4px;font-size:1.3em}} .card{{background:#1f2228;border-radius:8px;padding:8px 12px;margin:8px 0}}
.card ul{{margin:4px 0 0;padding-left:20px}} .card li{{margin:2px 0}} .back{{color:#8ab4f8}}
</style>
<p class=why><a class=back href='/'>← 판별 목록</a></p>
<h2>주간 리포트</h2>
<p class=why>{(now - timedelta(days=7)):%m-%d} ~ {now:%m-%d %H:%M} · 판별한 뉴스 {r['news']}건 · 알림 {r['alerts']}건 ·
👍 {r['up']} · 👎 {r['down']} · 등락은 야후 일봉 종가 (7일 전 → 마지막)</p>
<p class=why>많이 나온 사건 ({watcher.cfg['threshold']}점 이상): {topics}</p>
{''.join(cards)}"""


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
    rows = []
    for m in items:
        at = datetime.fromisoformat(m["at"])
        color = "#e06c6c" if m["change"] > 0 else "#6c9be0"   # 한국식: 오르면 빨강, 내리면 파랑
        news = "".join(f"<div class=mvn>{x['score']}점 <a href=\"{html.escape(x['url'])}\" target=_blank>"
                       f"{html.escape(x['title'])}</a></div>" for x in m["news"]) or \
            "<div class=mvn>최근 2시간 안에 이 종목 뉴스가 없다</div>"
        rows.append(f"<div class=mv><b style='color:{color}'>{html.escape(m['ticker'])} 15분 {m['change']:+.1f}%</b> "
                    f"<span class=why>{at:%m-%d %H:%M} · {m['price']:.2f} · {html.escape(m['name'])}</span>{news}</div>")
    return f"<div id=moves><div class=why>{title}</div>" + "".join(rows) + "</div>"


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
    note += "<p class=why>🔔10 👍 👎 🔕0 가운데 누른 것에 불이 켜집니다. 🔔10 은 '반드시 알려라', 🔕0 은 '절대 알리지 마라'로 👍/👎 보다 강하게 반영됩니다. 같은 버튼을 다시 누르면 취소됩니다. "
    note += (f"<a href='/?{sq[1:]}' style='text-decoration:underline'>숨기기</a></p>" if show_all else
             f"👎·🔕0 준 뉴스{f', {low}점 이하 뉴스' if low >= 0 else ''}, 옛 기사"
             f"{', 가린 언론사' if watcher.cfg.get('hide_sources') else ''} {hidden}건은 숨겼습니다. <a href='/?all=1{sq}' style='text-decoration:underline'>모두 보기</a></p>")
    if stock:
        note = (f"<p class=filt><b>{html.escape(stock)}</b> 뉴스만 보는 중 · "
                f"<a href='/{'?all=1' if show_all else ''}'>모든 종목 보기</a></p>") + note
    stocks = load_stocks()
    counts, th = counts or {}, watcher.cfg["threshold"]

    def count(name):
        n, top = counts.get(name, (0, 0))
        return (f" <small class=cnt title='최근 24시간 목록에 보이는 뉴스 {n}건, 최고 {top}점'>{n}"
                f"<b{' class=hi' if top >= th else ''}>·{top}</b></small>") if n else ""

    names = " · ".join(f"<a class='sname{' on' if x['name'] == stock else ''}' href='/?s={quote(x['name'])}'>"
                       f"{html.escape(x['name'])}{count(x['name'])}</a>" for x in stocks) or "없음 (⚙ 설정에서 추가)"
    menu = settings.menu(dict(watcher.cfg, _tg_ready=tg_ready()), stocks)
    return f"""<!doctype html><meta charset=utf-8><title>Google News/Yahoo Finance 종목 뉴스 필터링 크롤러</title>
<style>{settings.CSS}
body{{font:var(--fs) system-ui,sans-serif;background:#16181c;color:#e6e6e6;margin:16px}}
table{{border-collapse:collapse;width:100%}} td{{padding:6px 8px;border-bottom:1px solid #2a2d33;vertical-align:top}}
a{{color:#e6e6e6;text-decoration:none}} .s{{text-align:right;font-weight:600}} .why{{color:#8a9099;font-size:.86em}}
.b,.t,.s{{width:1%;white-space:nowrap}} a.fb{{display:inline-block;margin-right:4px;padding:2px 5px;border-radius:6px;font-size:1.14em;opacity:.3;filter:grayscale(1)}} a.fb:hover{{opacity:.8}} a.fb.num{{font-weight:700;font-size:.93em;white-space:nowrap;text-align:center;color:#fff;background:#2a2d33}} a.fb.on{{opacity:1;filter:none;background:#3a4a6b;outline:1px solid #6d8fd6}} tr.hit{{background:#1d2a45}} tr.done{{background:#2a3d23}} a.rated{{color:#8a9099}} #list a[target=_blank]:not(.rated):visited{{color:#aab0b8}} .ok{{color:#8fd18f}} .warn{{color:#e0a44a;font-size:.93em}} .warn a{{color:#e0a44a;text-decoration:underline}} .src{{display:inline-block;margin-right:6px;padding:0 5px;border-radius:4px;background:#2a2d33;color:#b8bec6;font-size:.79em}} .stk{{display:inline-block;margin-right:6px;padding:0 5px;border-radius:4px;background:#23382c;color:#9fd8b0;font-size:.79em}} a.stk:hover{{background:#2e4a3a}} a.sname{{color:#8a9099}} .cnt{{color:#6f7680}} .cnt b{{font-weight:400}} .cnt b.hi{{color:#e0a44a;font-weight:600}} a.sname:hover,a.sname.on{{color:#9fd8b0}} .filt{{margin:6px 0;padding:6px 10px;background:#23382c;border-radius:6px;color:#9fd8b0}} .filt a{{color:#e6e6e6;text-decoration:underline}} .tp{{display:inline-block;margin-right:6px;padding:0 5px;border-radius:4px;background:#2d2640;color:#c9b8ef;font-size:.79em}} .by{{font-size:.86em;font-weight:400;opacity:.75;margin-top:2px}} .by.cl{{color:#d97757}} .reset{{margin-top:24px}} .reset a{{color:#e0a44a;text-decoration:underline;cursor:pointer}} a.grp{{margin-left:8px;color:#8ab4f8;cursor:pointer;text-decoration:underline}} tr.child{{display:none}} tr.child.show{{display:table-row}} tr.child td{{background:#1b1e23}} tr.child td:nth-child(4){{padding-left:56px}}
.old{{color:#e0a44a;font-size:.8em}} #earn .guess{{opacity:.55}} #moves .mv{{margin:4px 0 8px;padding:6px 10px;background:#1f2228;border-radius:6px}}
#moves .mvn{{font-size:.9em;margin:2px 0 0 12px}} #moves a{{color:#e6e6e6}}
#recent{{margin:6px 0 10px;padding:6px 10px;background:#1d2a45;border-radius:6px}} #recent:empty{{display:none}}
#recent .ra{{margin:2px 0}} #recent a{{color:#e6e6e6}} #earn{{margin:6px 0}} #earn summary{{cursor:pointer}} .sum{{display:none;color:#b8bec6;font-size:.9em;line-height:1.45;margin:2px 0 3px}} .sum.show{{display:block}} a.sumbtn,a.sumget{{margin-left:8px;color:#8ab4f8;cursor:pointer;text-decoration:underline}} a.sumget{{color:#8a9099}} h2{{margin:0;font-size:1.4em}} h2 small{{font-size:.65em;font-weight:400}}
</style>
<header><h2>Google News/Yahoo Finance 종목 뉴스 필터링 크롤러 <small style="color:#8a9099">기준 {watcher.cfg['threshold']}점 · 파란 줄은 알림을 보낸 뉴스 · 점수 밑 🦙 Ollama / <span style="color:#d97757">✴</span> Claude 가 판별</small></h2>
  <span style="margin-left:auto"></span>
  <span class=fsz><button class=hbtn id=fsdown title="글자 작게" aria-label="글자 작게">가-</button><button class=hbtn id=fsup title="글자 크게" aria-label="글자 크게">가+</button></span>
  <button class=hbtn id=setbtn title="종목·알림·소리 설정" aria-expanded=false>⚙ 설정</button>
</header>
{menu}
<p class=why id=stockline><a href='/sources' style='text-decoration:underline'>판별·언론사 성적표</a> · <a href='/week' style='text-decoration:underline'>주간 리포트</a> · 종목 {names} · 구글 뉴스·야후 파이낸스에서 {watcher.cfg['fetch_min']}분마다 받습니다 · 마지막 수집 {watcher.fetch_note} · {watcher.summarizer.status()}</p>
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
  if (keep && Date.now() - keepAt > 10000) keep = null;
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
    document.getElementById("upd").textContent = "자동 갱신 " + new Date().toLocaleTimeString("ko-KR", {{hour12: false}});
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

// 새 알림은 화면에 먼저 띄우고, 그다음 서버가 읽는다. 판 번호가 바뀌면 곧바로 다시 받는다.
let ver = "{watcher.ver}";
setInterval(async () => {{
  try {{
    const v = await (await fetch("/ver", {{cache: "no-store"}})).text();
    if (v !== ver) {{ ver = v; refresh(); }}
  }} catch (e) {{}}
}}, 2000);

// 같은 사건 묶음과 요약: 펼친 것은 자동 갱신 뒤에도 펼친 채로 둔다
const opened = new Set(), openedSum = new Set();
function applyOpen() {{
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
                score, reason, topic, say, ko, summ = res.get(r["id"], (None, "(응답 없음)", "", "", "", ""))
                mark = "🔔" if score is not None and score >= cfg["threshold"] else "  "
                print(f"{mark} {score if score is not None else '-':>2} [{topic}] {r['title'][:70]}  — {reason}  🗣 {say}")
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
