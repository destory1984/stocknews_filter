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
from urllib.parse import parse_qs, urlparse

import requests

import article
import settings
import stocknews
import store

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
    "catchup_hours": 12,           # 절전·재시작으로 밀린 뉴스는 이만큼까지 거슬러 판별해 목록에만 올린다
    "batch": 10,                   # 한 번에 묻는 뉴스 수
    "poll_sec": 10,
    "max_wait_sec": 90,            # 뉴스를 모아서 한 번에 묻는다. batch 가 차거나 가장 오래 기다린 뉴스가 이만큼 되면 묻는다
    "port": 18766,                 # 18765 는 saveticker 필터링
    "examples": 15,                # 프롬프트에 넣을 👍, 👎 각각의 최대 개수
    "dup_ratio": 0.6,              # 최근 알린 제목과 이만큼 비슷하면 알리지 않는다
    "hide_max_score": 3,           # 판별 목록에서 이 점수 이하는 기본으로 숨긴다 (👍·🔔10 준 것은 보인다)
    "tts": True,                   # 알림을 말로도 읽는다: 말머리 소리 → "언론사, 제목" (영어 제목은 번역한 것)
    "tts_voice": "ko-KR-SunHiNeural",   # Edge 읽어주기 음성. 안 되면 윈도우 기본 음성(SAPI)
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


def _speak_edge(cfg: dict, text: str):
    import asyncio
    import edge_tts
    fd, tmp = tempfile.mkstemp(prefix="news_tts_", suffix=".mp3")
    os.close(fd)
    try:
        asyncio.run(asyncio.wait_for(
            edge_tts.Communicate(text, cfg["tts_voice"], rate=cfg["tts_rate"]).save(tmp), 15))
        play_file(tmp)
    finally:
        try:
            os.remove(tmp)
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


def _speech_worker(cfg: dict):
    # 한 판별 묶음에서 알림이 여럿 나와도 겹치지 않게 차례로 읽는다. 판별은 기다리지 않는다.
    while True:
        text = _speech.get()
        try:
            speak(cfg, text)
        except Exception as e:
            log(f"음성 오류: {type(e).__name__}: {e}")


def spoken(r: dict, ko: str = "") -> str:
    """음성으로 읽을 말: "언론사, 제목". 영어 제목은 번역한 제목을 읽는다."""
    title = (ko or r.get("title_ko") or r.get("title") or "").strip()
    src = SOURCE_NAMES.get(r.get("source", ""), r.get("source", "")).strip()
    return f"{src}, {title}" if src and title else title


def say_alert(cfg: dict, text: str):
    if not cfg["tts"] or quiet_now(cfg) or not text:
        return
    _speech.put(text)


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
        self.busy_until = 0.0      # 구글이 429 로 막으면 이때까지 구글 기사는 쉰다
        self.blocks = 0            # 잇달아 막힌 횟수. 한 번 통하면 0 으로
        self.last_google = 0.0
        self.done = 0
        self.gate = threading.Lock()   # 구글 링크 풀기는 판별 루프와 요약 스레드가 함께 쓴다
        self.cache = {}                # id → 본문. 알리기 전에 받은 본문을 요약에 다시 쓴다 (메모리에만)
        self.dated = set()             # 원문 날짜를 본 id (Ollama 가 꺼져 있어도 옛 기사는 가린다)

    def fetch_article(self, rec: dict, urgent: bool = False):
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
        log(f"구글 링크 풀기 ({'알림 전' if urgent else '요약'}) {rec.get('title', '')[:50]}")
        try:
            _, body, pub = article.fetch_body(rec["url"])
        except article.Busy:
            with self.gate:
                rest = self.cfg["google_block_min"] * 2 ** min(self.blocks, 2)
                self.blocks += 1
                self.busy_until = time.time() + rest * 60
            log(f"구글이 요청이 많다며 막음 → {rest}분 쉬고 다시")
            raise
        self.blocks = 0
        self.cache[rec["id"]] = (body, pub)
        return body, pub

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

    def pending(self, include_busy: bool = False) -> list:
        """요약할 것. 야후는 목록에 보이는 뉴스 모두, 구글은 기준 점수(알림 대상) 이상만.
        구글 기사는 원문 주소를 풀 때마다 구글에 묻는데, 300건을 30초 간격으로 물어도 429 로 막혔다 (2026-09-26)."""
        todo = [r for r in store.summary_todo(self.cfg["hide_max_score"], self.cfg["summary_hours"])
                if r.get("feed") != "google" or r["score"] >= self.cfg["threshold"]]
        if not include_busy and time.time() < self.busy_until:
            todo = [r for r in todo if r.get("feed") != "google"]
        return todo

    def status(self) -> str:
        if not self.cfg.get("summarize"):
            return "요약 꺼짐"
        n = len(self.pending(include_busy=True))
        if self.alive is False:
            return f"요약: Ollama 꺼짐 · 켜지면 {n}건 요약" if n else "요약: Ollama 꺼짐"
        if time.time() < self.busy_until:
            return f"요약: 구글이 잠시 막아 {datetime.fromtimestamp(self.busy_until):%H:%M} 부터 다시 · 남은 {n}건"
        return f"요약 중 · 남은 {n}건" if self.running else (f"요약 대기 {n}건" if n else "요약 다 됨")

    def poke(self):
        if self.running or not self.cfg.get("summarize"):
            return
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        """구글 기사는 Ollama 와 상관없이 원문 날짜부터 본다 (옛 기사 가리기). 요약은 Ollama 가 켜져 있을 때만."""
        try:
            todo = self.pending()
            if not todo:
                return
            alive = self.ollama_alive()
            for rec in todo[:20]:
                if not self.cfg.get("summarize"):
                    break
                if rec.get("feed") == "google" and rec["id"] not in self.dated:
                    if not self.check_date(rec):   # 구글이 막는 중
                        break
                    if self.w.judged.get(rec["id"], rec).get("summary_state"):   # 옛 기사·본문 없음
                        continue
                if alive and not self.one(rec):
                    break
        except Exception as e:
            log(f"요약 오류: {type(e).__name__}: {str(e)[:120]}")
        finally:
            self.running = False

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

    def topic_alerted(self, topic: str) -> bool:
        """한 시간 안에 이 사건으로 알림을 보냈는가."""
        return bool(topic) and any(r.get("alerted") and r.get("topic") == topic for r in self.recent(1))

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

    def is_late(self, r: dict) -> bool:
        """알리기엔 늦은 뉴스인가. PC 가 잠든 사이 나온 뉴스를 깨어나서 한꺼번에 울리지 않게."""
        return r["ts"] < datetime.now(timezone.utc) - timedelta(minutes=self.cfg["max_age_min"])

    def step(self):
        self.fetch()
        self.summarizer.poke()
        todo = self.pending()
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
            for r in batch:
                if r["id"] not in result:
                    continue
                score, reason, topic, say, ko, summ = result[r["id"]]
                if ko:
                    r["title_ko"] = ko   # 토스트·텔레그램이 번역 제목을 쓴다
                # 같은 사건(시진핑 발언 문장마다 뜨는 속보 등)은 한 시간에 한 번만 알린다
                late = self.is_late(r)
                alert = (score >= self.cfg["threshold"] and not late and not self.topic_alerted(topic)
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
                    say_alert(self.cfg, spoken(r, ko) or say or topic or reason)

    def backfill_translations(self, hours: int = 48, size: int = 20):
        """번역이 생기기 전에 판별한 영어 제목을 한 번 번역해 둔다 (시작할 때 뒤에서)."""
        recs = [r for r in self.recent(hours) if "title_ko" not in r and is_english(r.get("title", ""))]
        done = 0
        for k in range(0, len(recs), size):
            batch = recs[k:k + size]
            try:
                kos = translate_titles(self.cfg, [r["title"] for r in batch])
            except Exception as e:
                log(f"번역 실패: {type(e).__name__}: {str(e)[:100]}")
                return
            for r, ko in zip(batch, kos):
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
                self.send_header("Location", f"/?done={rec['id']}" + ("&all=1" if q.get("all") else ""))
                self.end_headers()
            elif u.path == "/":
                watcher.summarizer.poke()   # 목록이 갱신될 때마다 Ollama 가 켜졌는지 보고 밀린 요약을 한다
                self.send_page(page(watcher, q.get("done"), bool(q.get("all"))))
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
                  "tts_voice": "목소리", "tts_rate": "빠르기", "tts_chime": "말머리 소리", "quiet_on": "조용한 시각",
                  "tts_quiet": "조용한 시각", "telegram": "텔레그램", "fetch_min": "받는 간격",
                  "catchup_hours": "밀린 뉴스", "backend": "판별 LLM", "claude_model": "Claude 모델",
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
                  "/fetch": fetch_now, "/watch": watch_change, "/mb": mb_rows}


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


FB_LABELS = {"10": "🔔10점", "1": "👍", "0": "👎", "00": "🔕0점"}


def page(watcher: Watcher, done: str = None, show_all: bool = False) -> str:
    fb = {k: fb_key(v) for k, v in latest_feedback().items()}
    recs = sorted(watcher.judged.values(), key=lambda r: r.get("created_at", ""), reverse=True)
    # 👎·0점 준 뉴스와 점수가 낮은 뉴스는 기본으로 숨긴다.
    # 방금 누른 것은 기록됐다는 표시를 위해, 👍·10점 준 것은 점수와 상관없이 남긴다.
    low = watcher.cfg["hide_max_score"]

    def hide(r):
        if r["id"] == done or fb.get(r["id"]) in ("1", "10"):
            return False
        return fb.get(r["id"]) in ("0", "00") or r["score"] <= low or bool(r.get("stale"))

    hidden = 0 if show_all else sum(1 for r in recs if hide(r))
    if not show_all:
        recs = [r for r in recs if not hide(r)]
    recs = recs[:150]
    qs = "&all=1" if show_all else ""
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
    note = "<p class=ok>반응을 기록했습니다. 다음 판별부터 반영됩니다.</p>" if done else ""
    return page_html(watcher, rows, note, show_all, low, hidden, sum(1 for v in fb.values() if v))


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
          "claude": "<div class='by cl' title='Claude 가 판별'>✴</div>"}.get(r.get("by"), "")
    topic = f"<span class=tp>{html.escape(r['topic'])}</span>" if r.get("topic") else ""
    source = SOURCE_NAMES.get(r.get("source", ""), r.get("source", ""))
    src = f"<span class=src>{html.escape(source)}</span>" if source else ""
    if r.get("tickers"):
        src = f"<span class=stk>{html.escape(r['tickers'])}</span>" + src
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
        + (f" <a class=sumbtn data-id='{r['id']}'>요약 ▾</a>" if r.get("summary_ko") else "")
        + f"{group}</div></td></tr>")


def page_html(watcher: Watcher, rows: list, note: str, show_all: bool, low: int, hidden: int, n_fb: int) -> str:
    note += "<p class=why>🔔10 👍 👎 🔕0 가운데 누른 것에 불이 켜집니다. 🔔10 은 '반드시 알려라', 🔕0 은 '절대 알리지 마라'로 👍/👎 보다 강하게 반영됩니다. 같은 버튼을 다시 누르면 취소됩니다. "
    note += ("<a href='/' style='text-decoration:underline'>숨기기</a></p>" if show_all else
             f"👎·🔕0 준 뉴스{f', {low}점 이하 뉴스' if low >= 0 else ''}, 옛 기사 {hidden}건은 숨겼습니다. <a href='/?all=1' style='text-decoration:underline'>모두 보기</a></p>")
    stocks = load_stocks()
    names = " · ".join(html.escape(x["name"]) for x in stocks) or "없음 (⚙ 설정에서 추가)"
    menu = settings.menu(dict(watcher.cfg, _tg_ready=tg_ready()), stocks)
    return f"""<!doctype html><meta charset=utf-8><title>Google News/Yahoo Finance 종목 뉴스 필터링 크롤러</title>
<style>{settings.CSS}
body{{font:var(--fs) system-ui,sans-serif;background:#16181c;color:#e6e6e6;margin:16px}}
table{{border-collapse:collapse;width:100%}} td{{padding:6px 8px;border-bottom:1px solid #2a2d33;vertical-align:top}}
a{{color:#e6e6e6;text-decoration:none}} .s{{text-align:right;font-weight:600}} .why{{color:#8a9099;font-size:.86em}}
.b,.t,.s{{width:1%;white-space:nowrap}} a.fb{{display:inline-block;margin-right:4px;padding:2px 5px;border-radius:6px;font-size:1.14em;opacity:.3;filter:grayscale(1)}} a.fb:hover{{opacity:.8}} a.fb.num{{font-weight:700;font-size:.93em;white-space:nowrap;text-align:center;color:#fff;background:#2a2d33}} a.fb.on{{opacity:1;filter:none;background:#3a4a6b;outline:1px solid #6d8fd6}} tr.hit{{background:#1d2a45}} tr.done{{background:#2a3d23}} a.rated{{color:#8a9099}} .ok{{color:#8fd18f}} .warn{{color:#e0a44a;font-size:.93em}} .warn a{{color:#e0a44a;text-decoration:underline}} .src{{display:inline-block;margin-right:6px;padding:0 5px;border-radius:4px;background:#2a2d33;color:#b8bec6;font-size:.79em}} .stk{{display:inline-block;margin-right:6px;padding:0 5px;border-radius:4px;background:#23382c;color:#9fd8b0;font-size:.79em}} .tp{{display:inline-block;margin-right:6px;padding:0 5px;border-radius:4px;background:#2d2640;color:#c9b8ef;font-size:.79em}} .by{{font-size:.86em;font-weight:400;opacity:.75;margin-top:2px}} .by.cl{{color:#d97757}} .reset{{margin-top:24px}} .reset a{{color:#e0a44a;text-decoration:underline;cursor:pointer}} a.grp{{margin-left:8px;color:#8ab4f8;cursor:pointer;text-decoration:underline}} tr.child{{display:none}} tr.child.show{{display:table-row}} tr.child td{{background:#1b1e23}} tr.child td:nth-child(4){{padding-left:56px}}
.old{{color:#e0a44a;font-size:.8em}} .sum{{display:none;color:#b8bec6;font-size:.9em;line-height:1.45;margin:2px 0 3px}} .sum.show{{display:block}} a.sumbtn{{margin-left:8px;color:#8ab4f8;cursor:pointer;text-decoration:underline}} h2{{margin:0;font-size:1.4em}} h2 small{{font-size:.65em;font-weight:400}}
</style>
<header><h2>Google News/Yahoo Finance 종목 뉴스 필터링 크롤러 <small style="color:#8a9099">기준 {watcher.cfg['threshold']}점 · 파란 줄은 알림을 보낸 뉴스 · 점수 밑 🦙 Ollama / <span style="color:#d97757">✴</span> Claude 가 판별</small></h2>
  <span style="margin-left:auto"></span>
  <span class=fsz><button class=hbtn id=fsdown title="글자 작게" aria-label="글자 작게">가-</button><button class=hbtn id=fsup title="글자 크게" aria-label="글자 크게">가+</button></span>
  <button class=hbtn id=setbtn title="종목·알림·소리 설정" aria-expanded=false>⚙ 설정</button>
</header>
{menu}
<p class=why id=stockline>종목 {names} · 구글 뉴스·야후 파이낸스에서 {watcher.cfg['fetch_min']}분마다 받습니다 · 마지막 수집 {watcher.fetch_note} · {watcher.summarizer.status()}</p>
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
async function refresh() {{
  if (keep && Date.now() - keepAt > 10000) keep = null;
  const params = new URLSearchParams({{{"all: 1" if show_all else ""}}});
  if (keep) params.set("done", keep);
  try {{
    const r = await fetch("/?" + params, {{cache: "no-store"}});
    const doc = new DOMParser().parseFromString(await r.text(), "text/html");
    document.getElementById("list").innerHTML = doc.getElementById("list").innerHTML;
    applyOpen();
    document.getElementById("upd").textContent = "자동 갱신 " + new Date().toLocaleTimeString("ko-KR", {{hour12: false}});
  }} catch (e) {{
    document.getElementById("upd").textContent = "판별기에 연결할 수 없습니다 (" + new Date().toLocaleTimeString("ko-KR", {{hour12: false}}) + ")";
  }}
}}
setInterval(refresh, 15000);

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
  const sb = e.target.closest("a.sumbtn");
  if (sb) {{
    openedSum.has(sb.dataset.id) ? openedSum.delete(sb.dataset.id) : openedSum.add(sb.dataset.id);
    applyOpen();
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
