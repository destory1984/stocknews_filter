"""설정 창 (판별 목록 오른쪽 위 ⚙ 설정).

한국투자증권 RSI Monitor(koreainvest/static/index.html)의 설정 창과 같은 모양이다:
줄마다 이름·한 줄 설명·ⓘ(자세히)·오른쪽 스위치, 바꾸면 바로 적용된다 (저장 단추 없음).
값은 stock_alert_config.json 에 쓰고, 돌고 있는 판별기에도 곧바로 반영한다.
"""
import html
import json
import re
from pathlib import Path

VOICES = [("ko-KR-SunHiNeural", "선희 (여)"), ("ko-KR-InJoonNeural", "인준 (남)"),
          ("ko-KR-HyunsuMultilingualNeural", "현수 (남, 다국어)")]
RATES = [("-30%", "느리게"), ("-15%", "조금 느리게"), ("+0%", "보통"), ("+15%", "조금 빠르게"), ("+30%", "빠르게")]
MEDIA = Path(r"C:\Windows\Media")


def chimes() -> list:
    files = sorted(MEDIA.glob("*.wav")) if MEDIA.exists() else []
    return [("", "소리 없음")] + [(str(p), p.stem) for p in files]


# 키 → (종류, 선택지 또는 (최소, 최대))
FIELDS = {
    "toast": ("bool", None),
    "threshold": ("select", [(str(i), f"{i}점 이상") for i in range(5, 11)]),
    "max_age_min": ("select", [(str(m), f"{m}분") for m in (30, 60, 120, 180, 360)]),
    "tts": ("bool", None),
    "tts_voice": ("select", VOICES),
    "tts_rate": ("select", RATES),
    "tts_chime": ("select", None),
    "quiet_on": ("bool", None),
    "tts_quiet": ("quiet", None),
    "telegram": ("bool", None),
    "summarize": ("bool", None),
    "fetch_min": ("select", [(str(m), f"{m}분마다") for m in (1, 3, 5, 10, 15, 30)]),
    "catchup_hours": ("select", [(str(h), f"{h}시간") for h in (3, 6, 12, 24)]),
    "backend": ("select", [("auto", "Ollama 먼저, 안 되면 Claude"), ("ollama", "Ollama 만"), ("claude", "Claude 만")]),
    "claude_model": ("select", [("sonnet", "sonnet"), ("opus", "opus")]),
    "model": ("text", None),
    "hide_max_score": ("select", [("-1", "숨기지 않음")] + [(str(i), f"{i}점 이하") for i in range(0, 6)]),
}
INT_KEYS = {"threshold", "max_age_min", "fetch_min", "catchup_hours", "hide_max_score"}


def apply(cfg: dict, key: str, value) -> tuple:
    """값 하나를 검사해 cfg 에 넣는다. (전, 후) 를 돌려준다."""
    if key not in FIELDS:
        raise ValueError(f"바꿀 수 없는 설정: {key}")
    kind, opts = FIELDS[key]
    if kind == "bool":
        value = bool(value)
    elif kind == "select":
        allowed = [o[0] for o in (opts if opts is not None else chimes())]
        value = str(value)
        if value not in allowed:
            raise ValueError("고를 수 없는 값")
        if key in INT_KEYS:
            value = int(value)
    elif kind == "quiet":
        value = str(value).strip()
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("23:00-07:00 처럼 넣어 주세요")
    else:
        value = str(value).strip()
        if not value:
            raise ValueError("비울 수 없습니다")
    before = cfg.get(key)
    cfg[key] = value
    return before, value


def save(cfg: dict, path: Path):
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")


# ─────────────────────────────────────────────────────────────
# 화면
# ─────────────────────────────────────────────────────────────

e = html.escape


def _info(tip: str) -> str:
    return (f"<button type=button class=info aria-expanded=false title=\"{e(tip)}\" aria-label=자세히>ⓘ</button>")


def _label(name: str, small: str = "", tip: str = "", small_id: str = "") -> str:
    sid = f" id={small_id}" if small_id else ""
    return (f"<span class=mlabel><span>{e(name)}{' ' + _info(tip) if tip else ''}</span>"
            + (f"<small{sid}>{e(small)}</small>" if small else "") + "</span>")


def _more(tip: str) -> str:
    return f"<p class=more hidden>{e(tip)}</p>" if tip else ""


def _switch(cfg: dict, key: str, aria: str) -> str:
    on = "true" if cfg.get(key) else "false"
    return f"<button class=switch data-key={key} role=switch aria-checked={on} aria-label=\"{e(aria)}\"></button>"


def _select(cfg: dict, key: str, aria: str) -> str:
    opts = FIELDS[key][1]
    items = opts if opts is not None else chimes()
    cur = str(cfg.get(key, ""))
    if cur not in [o[0] for o in items]:   # 설정 파일에 목록 밖의 값이 있으면 그대로 보여 준다
        items = items + [(cur, cur)]
    return (f"<select data-key={key} aria-label=\"{e(aria)}\">"
            + "".join(f"<option value=\"{e(o)}\"{' selected' if o == cur else ''}>{e(t)}</option>" for o, t in items)
            + "</select>")


def row(label: str, control: str, tip: str = "") -> str:
    return f"<div class=mrow>{label}{control}</div>{_more(tip)}"


# 휴지통 아이콘. 글자색(currentColor)을 따라가서 마우스를 올리면 빨개진다
TRASH = ("<svg width=14 height=14 viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=2 "
         "stroke-linecap=round stroke-linejoin=round aria-hidden=true><path d='M3 6h18'/>"
         "<path d='M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2'/><path d='M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6'/>"
         "<path d='M10 11v6M14 11v6'/></svg>")


def stocks_html(stocks: list) -> str:
    """종목 칸: 이름 칩(누르면 자세히 고치기) + 휴지통, 추가 입력."""
    chips = []
    # 티커 순으로 보인다 (yahoo 티커가 없으면 이름으로). 한국 종목(.KS/.KQ)은 맨 뒤로.
    # watchlist.json 의 순서는 그대로 둔다
    def order(s):
        t = (s.get("yahoo") or s["name"]).upper()
        return (t.endswith((".KS", ".KQ")), t)

    for s in sorted(stocks, key=order):
        j = lambda k: e(", ".join(s.get(k, [])))
        chips.append(
            f"<div class=stock data-name=\"{e(s['name'])}\">"
            f"<div class=srow><button type=button class=sdel title='종목에서 빼기' aria-label=삭제>{TRASH}</button>"
            f"<button type=button class=sname aria-expanded=false title='눌러서 자세히 고치기'>"
            # 티커 먼저, 회사 이름은 뒤에 작게. 이름이 티커와 같으면(SOXL) 한 번만
            + (f"{e(s['yahoo'])}{' <small>' + e(s['name']) + '</small>' if s['name'].upper() != s['yahoo'].upper() else ''}"
               if s.get("yahoo") else e(s["name"]))
            + "</button></div>"
            f"<div class=sedit hidden>"
            f"<label>이름 <input name=name value=\"{e(s['name'])}\"></label>"
            f"<label>구글 검색어 <input name=google value=\"{e(s.get('google', ''))}\" placeholder=\"{e(s['name'])}\"></label>"
            f"<label>구글 언어 <select name=lang><option value=ko{'' if s.get('lang') == 'en' else ' selected'}>한국어</option>"
            f"<option value=en{' selected' if s.get('lang') == 'en' else ''}>영어</option></select></label>"
            f"<label>야후 티커 <input name=yahoo value=\"{e(s.get('yahoo', ''))}\" placeholder='없으면 구글만'></label>"
            f"<label>꼭 들어갈 말 <input name=keywords value=\"{j('keywords')}\" placeholder='비우면 이름·티커'></label>"
            f"<label>뺄 말 <input name=exclude value=\"{j('exclude')}\" placeholder='쉼표로 여럿'></label>"
            f"<label>뺄 언론사 <input name=exclude_sources value=\"{j('exclude_sources')}\" placeholder='예: MarketBeat'></label>"
            f"<div class=mbtns><button type=button class='hbtn ssave'>저장</button></div></div></div>")
    return "".join(chips) or "<div class=mrow><small class=sub>종목이 없다</small></div>"


def menu(cfg: dict, stocks: list) -> str:
    q = cfg.get("tts_quiet") or "23:00-07:00"
    qfrom, qto = (q.split("-") + ["07:00"])[:2] if re.fullmatch(r"\d\d:\d\d-\d\d:\d\d", q) else ("23:00", "07:00")
    tg_ok = cfg.get("_tg_ready")
    return f"""<div class=menu id=setmenu hidden>
  <div class=mrow><span>종목</span><span class=sub>구글 뉴스 · 야후 파이낸스</span></div>
  <div id=stocksec>{stocks_html(stocks)}</div>
  <form class="mrow addrow" id=addstock>
    <input name=stock placeholder="이름 (삼성전자, Micron)" required autocomplete=off spellcheck=false>
    <input name=yahoo placeholder="야후 티커 (선택)" autocomplete=off spellcheck=false>
    <button class=hbtn>추가</button></form>
  <div class="mrow"><small class=sub id=stockmsg>영어 이름은 영어 구글 뉴스에서 찾는다. 추가하면 바로 받는다</small></div>

  <div class=mhead>알림</div>
  {row(_label("윈도우 알림", "관심 뉴스를 토스트로 띄운다", "기준 점수 이상이고 알림 시한 안의 뉴스만 띄운다. 같은 사건은 한 시간에 한 번. 끄면 목록에만 쌓인다."), _switch(cfg, "toast", "윈도우 알림"),
       "기준 점수 이상이고 알림 시한 안의 뉴스만 띄운다. 같은 사건은 한 시간에 한 번. 끄면 목록에만 쌓인다.")}
  {row(_label("기준 점수", "LLM 이 매긴 0~10점"), _select(cfg, "threshold", "기준 점수"))}
  {row(_label("알림 시한", "나온 지 이보다 오래되면 알리지 않는다", "구글 뉴스는 기사가 나온 뒤 늦게 잡히기도 한다. 너무 짧으면 놓치고, 길면 PC 가 깨어났을 때 지난 뉴스가 울린다. 시한이 지난 뉴스도 판별해서 목록에는 올린다."), _select(cfg, "max_age_min", "알림 시한"),
       "구글 뉴스는 기사가 나온 뒤 늦게 잡히기도 한다. 너무 짧으면 놓치고, 길면 PC 가 깨어났을 때 지난 뉴스가 울린다. 시한이 지난 뉴스도 판별해서 목록에는 올린다.")}
  {row(_label("텔레그램", "폰으로도 받기" if tg_ok else "환경변수가 없어 보낼 수 없다", "알림을 텔레그램으로도 보낸다. 봇 토큰과 대화방은 환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 에서 읽는다 (RSI 모니터와 같은 것).", "tgnote"),
       "<span class=mbtns><button type=button class=hbtn id=tgtest title='시험 메시지를 한 번 보낸다'>전송 테스트</button>" + _switch(cfg, "telegram", "텔레그램으로 보내기") + "</span>",
       "알림을 텔레그램으로도 보낸다. 봇 토큰과 대화방은 환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 에서 읽는다 (RSI 모니터와 같은 것).")}

  <div class=mhead>소리 <small>꺼 둔 때도 알림 목록에는 쌓인다</small></div>
  {row(_label("음성으로 읽기", "말머리 소리 뒤에 언론사와 제목을 읽는다", "「언론사, 제목」 을 Edge 음성으로 읽는다 (예: 연합뉴스, SK하이닉스 손자회사 솔리다임 이르면 내년 美상장 검토). 영어 제목은 번역한 제목을 읽는다. 인터넷이 안 되면 윈도우 기본 음성으로 읽는다. 켜 두면 토스트 소리는 끈다."), _switch(cfg, "tts", "음성으로 읽기"),
       "「언론사, 제목」 을 Edge 음성으로 읽는다 (예: 연합뉴스, SK하이닉스 손자회사 솔리다임 이르면 내년 美상장 검토). 영어 제목은 번역한 제목을 읽는다. 인터넷이 안 되면 윈도우 기본 음성으로 읽는다. 켜 두면 토스트 소리는 끈다.")}
  {row(_label("목소리"), _select(cfg, "tts_voice", "목소리"))}
  {row(_label("빠르기"), _select(cfg, "tts_rate", "빠르기"))}
  {row(_label("말머리 소리", "saveticker 는 Windows Notify Messaging"), _select(cfg, "tts_chime", "말머리 소리"))}
  {row(_label("조용한 시각", "이 PC 시각. 23:00~07:00 처럼 자정을 넘어도 된다"), _switch(cfg, "quiet_on", "조용한 시각"))}
  <div class="mrow qtimes"><input type=time id=qfrom value="{qfrom}" aria-label="조용한 시각 시작"> ~ <input type=time id=qto value="{qto}" aria-label="조용한 시각 끝"></div>
  <div class=mrow><input id=saytext value="연합뉴스, 삼성전자 목표주가 상향" aria-label="읽어 볼 말"><button type=button class=hbtn id=soundtest title="고른 목소리로 한 번 읽는다">TTS 테스트</button></div>

  <div class=mhead>수집 · 판별</div>
  {row(_label("뉴스 받는 간격", "", "구글 뉴스 RSS 는 종목마다 한 번, 야후는 티커마다 한 번 부른다. 너무 자주 부르면 막힐 수 있다."), "<span class=mbtns><button type=button class=hbtn id=fetchnow title='지금 한 번 받는다'>지금 받기</button>" + _select(cfg, "fetch_min", "뉴스 받는 간격") + "</span>",
       "구글 뉴스 RSS 는 종목마다 한 번, 야후는 티커마다 한 번 부른다. 너무 자주 부르면 막힐 수 있다.")}
  {row(_label("구글 기사 요약", "Ollama 가 켜져 있을 때만", "구글 뉴스 기사는 제목만 오니, 원문을 받아 본문을 Ollama 로 2~3문장 요약한다. Claude 는 쓰지 않는다. Ollama 가 꺼져 있으면 기다렸다가, 목록이 갱신될 때(15초) 켜진 것을 보면 밀린 것을 요약한다. 기준 점수 이상(알림 대상)인 구글 기사만 한다. 원문 주소를 풀 때마다 구글에 물어야 하는데, 자주 물으면 구글이 막기 때문이다. 원문 사이트의 robots.txt 가 막으면 건너뛴다. 본문은 저장하지 않는다. 야후 기사는 RSS 에 딸려 온 설명을 판별 때 함께 줄인다."), _switch(cfg, "summarize", "구글 기사 요약"),
       "구글 뉴스 기사는 제목만 오니, 원문을 받아 본문을 Ollama 로 2~3문장 요약한다. Claude 는 쓰지 않는다. Ollama 가 꺼져 있으면 기다렸다가, 목록이 갱신될 때(15초) 켜진 것을 보면 밀린 것을 요약한다. 기준 점수 이상(알림 대상)인 구글 기사만 한다. 원문 주소를 풀 때마다 구글에 물어야 하는데, 자주 물으면 구글이 막기 때문이다. 원문 사이트의 robots.txt 가 막으면 건너뛴다. 본문은 저장하지 않는다. 야후 기사는 RSS 에 딸려 온 설명을 판별 때 함께 줄인다.")}
  {row(_label("밀린 뉴스", "PC 가 잠들었다 깨면 이만큼 거슬러 판별"), _select(cfg, "catchup_hours", "밀린 뉴스"))}
  {row(_label("판별 LLM", "", "Ollama 는 이 PC 에서 돈다. 꺼져 있거나 엉뚱한 답을 내면 Claude CLI 로 넘긴다. Claude 는 구독 사용량을 쓴다."), _select(cfg, "backend", "판별 LLM"),
       "Ollama 는 이 PC 에서 돈다. 꺼져 있거나 엉뚱한 답을 내면 Claude CLI 로 넘긴다. Claude 는 구독 사용량을 쓴다.")}
  {row(_label("Claude 모델"), _select(cfg, "claude_model", "Claude 모델"))}
  <div class=mrow>{_label("Ollama 모델")}<input data-key=model value="{e(str(cfg.get('model', '')))}" aria-label="Ollama 모델" size=14 spellcheck=false></div>
  {row(_label("목록에서 숨기기", "👍·🔔10 준 것은 점수와 상관없이 보인다"), _select(cfg, "hide_max_score", "목록에서 숨기기"))}
  <div class=mrow><small class=sub id=setmsg>바꾸면 바로 적용된다</small></div>
</div>"""


CSS = """
:root{--fs:14px;--bg:#16181c;--panel:#1c1f24;--line:#2e333b;--text:#e6e6e6;--muted:#8a9099;--pos:#3cc47c;--neg:#f0605a;--down:#5b8ff0;--sel:#23272e;--input:#16181c}
header{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.hbtn{font:inherit;border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:99px;padding:2px 9px;cursor:pointer;white-space:nowrap}
.hbtn:hover{background:var(--sel)} .hbtn:disabled{opacity:.4;cursor:default}
.fsz{display:inline-flex} .fsz .hbtn:first-child{border-radius:99px 0 0 99px;border-right:0} .fsz .hbtn:last-child{border-radius:0 99px 99px 0}
.menu{position:fixed;z-index:10;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 4px;box-shadow:0 4px 16px rgba(0,0,0,.4);display:grid;width:min(420px,calc(100vw - 16px));max-height:calc(100vh - 70px);overflow-y:auto;overflow-x:hidden}
.menu[hidden],.menu [hidden]{display:none!important}
.menu .sub{color:var(--muted);font-size:.9em}
.menu .mrow{display:flex;gap:12px;align-items:center;justify-content:space-between;padding:4px 8px}
.menu .mlabel{display:grid;gap:1px} .menu .mlabel small{color:var(--muted);font-size:.85em}
.menu .mbtns{display:inline-flex;gap:8px;align-items:center}
.menu .info{font:inherit;border:0;background:none;color:var(--muted);cursor:pointer;padding:0 2px}
.menu .info:hover,.menu .info[aria-expanded="true"]{color:var(--down)}
.menu .more{margin:0 8px 4px;padding:6px 8px;border-radius:6px;background:var(--bg);color:var(--muted);font-size:.9em;line-height:1.5}
.menu .mhead{padding:8px 8px 2px;border-top:1px solid var(--line);margin-top:4px;font-weight:600}
.menu .mhead small{font-weight:400;color:var(--muted)}
.menu input,.menu select{font:inherit;color:var(--text);background:var(--input);border:1px solid var(--line);border-radius:5px;padding:2px 5px}
.menu .qtimes{justify-content:flex-end;gap:6px;color:var(--muted)} .menu .qtimes input:disabled{opacity:.5}
.menu .addrow input{flex:1;min-width:0}
.menu #saytext{flex:1;min-width:0}
.menu .stock{padding:0 8px}
.menu .srow{display:flex;align-items:center;gap:4px;border-bottom:1px solid var(--sel)}
.menu .sname{flex:1}
.menu .sname{font:inherit;font-weight:600;border:0;background:none;color:var(--text);cursor:pointer;padding:4px 0;text-align:left}
.menu .sname small{font-weight:400;color:var(--muted)} .menu .sname:hover,.menu .sname[aria-expanded="true"]{color:var(--down)}
.menu .sdel{font:inherit;border:0;background:none;color:var(--muted);cursor:pointer;border-radius:4px;padding:3px 5px;display:inline-flex;align-items:center}
.menu .sdel:hover{color:var(--neg);background:var(--bg)}
.menu .sedit{display:grid;gap:4px;padding:6px 0 8px 8px}
.menu .sedit label{display:flex;justify-content:space-between;align-items:center;gap:8px;color:var(--muted)}
.menu .sedit input,.menu .sedit select{width:62%}
.menu .sedit .mbtns{justify-content:flex-end}
.switch:disabled{opacity:.4;cursor:default}
.switch{position:relative;flex:none;width:calc(var(--fs)*3.2);height:calc(var(--fs)*1.8);padding:0;border:0;border-radius:99px;background:var(--line);cursor:pointer;transition:background .15s}
.switch::after{content:"";position:absolute;top:2px;left:2px;width:calc(var(--fs)*1.8 - 4px);height:calc(var(--fs)*1.8 - 4px);border-radius:50%;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.3);transition:transform .15s}
.switch[aria-checked="true"]{background:var(--pos)}
.switch[aria-checked="true"]::after{transform:translateX(calc(var(--fs)*1.4))}
.switch:focus-visible{outline:2px solid var(--down);outline-offset:2px}
.err{color:var(--neg)!important} .okmsg{color:var(--pos)!important}
"""

JS = r"""
const $ = s => document.querySelector(s);
async function post(path, body) {
  try {
    const r = await fetch(path, {method: "POST", headers: {"X-Settings": "yes", "Content-Type": "application/json"},
                                 body: JSON.stringify(body || {})});
    return r.ok ? await r.json() : {ok: false, msg: "요청 실패 (" + r.status + ")"};
  } catch (err) { return {ok: false, msg: "판별기에 연결할 수 없습니다"}; }
}
function say(id, d, okText) {
  const el = $(id);
  el.classList.toggle("err", !d.ok);
  el.classList.toggle("okmsg", !!d.ok);
  el.textContent = d.ok ? okText : d.msg;
}

// 글자 크기: 가- 가+  (이 브라우저에만 기억한다)
const FS_MIN = 11, FS_MAX = 20;
let fontSize = 14;
try { fontSize = +localStorage.getItem("fs") || 14; } catch (err) {}
function setFontSize(n) {
  fontSize = Math.max(FS_MIN, Math.min(FS_MAX, n));
  document.documentElement.style.setProperty("--fs", fontSize + "px");
  try { localStorage.setItem("fs", fontSize); } catch (err) {}
  $("#fsdown").disabled = fontSize <= FS_MIN;
  $("#fsup").disabled = fontSize >= FS_MAX;
  $("#fsdown").title = `글자 작게 · 지금 ${fontSize}px`;
  $("#fsup").title = `글자 크게 · 지금 ${fontSize}px`;
}
$("#fsdown").onclick = () => setFontSize(fontSize - 1);
$("#fsup").onclick = () => setFontSize(fontSize + 1);
setFontSize(fontSize);

// ⚙ 설정: 단추 밑에 펼친다. 바깥을 누르면 닫는다
(function () {
  const btn = $("#setbtn"), menu = $("#setmenu");
  const show = open => {
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", open);
    if (!open) return;
    const b = btn.getBoundingClientRect();
    menu.style.top = `${b.bottom + 4}px`;
    menu.style.left = `${Math.max(8, Math.min(b.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 8))}px`;
  };
  btn.onclick = ev => { ev.stopPropagation(); show(menu.hidden); };
  document.addEventListener("click", ev => {
    if (!menu.contains(ev.target) && ev.target.isConnected) show(false);
  });
})();

// ⓘ: PC 는 마우스를 올리면 title 이 뜨고, 폰은 눌러서 아래에 펼친다
$("#setmenu").addEventListener("click", ev => {
  const b = ev.target.closest(".info");
  if (!b) return;
  const more = b.closest(".mrow").nextElementSibling;
  if (!more || !more.classList.contains("more")) return;
  more.hidden = !more.hidden;
  b.setAttribute("aria-expanded", !more.hidden);
});

// 설정 하나 바꾸기: 바로 적용
async function setKey(key, value) {
  const d = await post("/settings", {key, value});
  say("#setmsg", d, d.ok ? "적용했다 · " + d.label : "");
  return d.ok;
}
document.querySelectorAll("#setmenu .switch[data-key]").forEach(sw => sw.onclick = async () => {
  const on = sw.getAttribute("aria-checked") !== "true";
  if (await setKey(sw.dataset.key, on)) sw.setAttribute("aria-checked", on);
  if (sw.dataset.key === "quiet_on") quietInputs();
  if (sw.dataset.key === "tts") ttsInputs();
});
document.querySelectorAll("#setmenu select[data-key], #setmenu input[data-key]").forEach(el =>
  el.onchange = () => setKey(el.dataset.key, el.value));
function quietInputs() {
  const on = $('#setmenu .switch[data-key="quiet_on"]').getAttribute("aria-checked") === "true";
  $("#qfrom").disabled = $("#qto").disabled = !on;
}
function ttsInputs() {
  const on = $('#setmenu .switch[data-key="tts"]').getAttribute("aria-checked") === "true";
  ["tts_voice", "tts_rate", "tts_chime"].forEach(k => $(`#setmenu [data-key="${k}"]`).disabled = !on);
}
$("#qfrom").onchange = $("#qto").onchange = () => setKey("tts_quiet", $("#qfrom").value + "-" + $("#qto").value);
quietInputs(); ttsInputs();

$("#soundtest").onclick = async () => {
  const d = await post("/say", {text: $("#saytext").value});
  say("#setmsg", d, "읽는 중 · " + (d.by || ""));
};
$("#tgtest").onclick = async (ev) => {
  ev.target.disabled = true;
  const d = await post("/telegram-test");
  ev.target.disabled = false;
  say("#setmsg", d, "텔레그램으로 보냈다");
};
$("#fetchnow").onclick = async (ev) => {
  ev.target.disabled = true;
  const d = await post("/fetch");
  ev.target.disabled = false;
  say("#setmsg", d, "새 뉴스 " + d.new + "건. 판별되면 목록에 오른다");
};

// 종목: 추가·고치기·빼기. 목록 페이지를 새로 읽지 않고 종목 칸만 바꿔 끼운다
async function reloadStocks() {
  try {
    const doc = new DOMParser().parseFromString(await (await fetch("/", {cache: "no-store"})).text(), "text/html");
    $("#stocksec").innerHTML = doc.getElementById("stocksec").innerHTML;
    $("#stockline").innerHTML = doc.getElementById("stockline").innerHTML;
  } catch (err) {}
}
$("#addstock").onsubmit = async (ev) => {
  ev.preventDefault();
  const f = ev.target, b = f.querySelector("button");
  b.disabled = true;
  $("#stockmsg").classList.remove("err", "okmsg");
  $("#stockmsg").textContent = f.yahoo.value.trim() ? "야후 티커 확인 중..." : "추가하는 중...";
  const d = await post("/watch", {op: "add", name: f.stock.value, yahoo: f.yahoo.value});
  b.disabled = false;
  say("#stockmsg", d, "추가했다 · 뉴스를 받는 중");
  if (d.ok) { f.reset(); reloadStocks(); }
};
$("#stocksec").addEventListener("click", async (ev) => {
  const box = ev.target.closest(".stock");
  if (!box) return;
  const name = box.dataset.name;
  if (ev.target.closest(".sname")) {
    const ed = box.querySelector(".sedit"), nb = box.querySelector(".sname");
    ed.hidden = !ed.hidden;
    nb.setAttribute("aria-expanded", !ed.hidden);
  } else if (ev.target.closest(".sdel")) {
    if (!confirm(name + " 을(를) 종목에서 뺄까요?\n\n이미 받아 둔 뉴스와 판별 기록은 그대로 남습니다.")) return;
    const d = await post("/watch", {op: "remove", name});
    say("#stockmsg", d, "뺐다 · " + name);
    if (d.ok) reloadStocks();
  } else if (ev.target.closest(".ssave")) {
    const fields = {};
    box.querySelectorAll(".sedit input, .sedit select").forEach(el => fields[el.name] = el.value);
    ev.target.disabled = true;
    const d = await post("/watch", {op: "update", name, fields});
    ev.target.disabled = false;
    say("#stockmsg", d, "저장했다 · 다음 수집부터 적용");
    if (d.ok) reloadStocks();
  }
});
"""
