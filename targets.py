"""애널리스트 목표가·투자의견 뉴스를 표로 만든다.

판별과 따로 묻는다: 제목에 목표가 낱말이 든 뉴스만 골라(CANDIDATE), 판별 모델에게
증권사·구분·투자의견·목표가(이전 → 이후)를 뽑게 한다. 결과는 DB targets 표에 둔다.
09-22 ~ 09-29 판별한 2,563건 가운데 제목으로 걸리는 것은 117건이었다.
"""
import json
import re
from datetime import timedelta

# 제목에 이런 말이 있으면 목표가 뉴스 후보다. 후보가 아니라고 모델이 답하면 빈칸으로 남는다
CANDIDATE = re.compile(
    r"price target|target price|\bPT\b|upgrade|downgrade|initiat|reiterat|maintains|\brating\b|coverage|"
    r"outperform|underperform|overweight|underweight|목표가|목표주가|투자의견|커버리지|매수 의견|매수의견",
    re.I)

# "제시" 는 목표가만 적혀 있고 올렸는지·내렸는지·처음인지 제목으로 알 수 없는 것
ACTIONS = ("상향", "하향", "유지", "신규", "의견상향", "의견하향", "제시")

PROMPT = """아래 뉴스 제목에서 증권사·애널리스트의 목표가·투자의견 조치를 뽑아라.

[관찰 종목] (이 이름 가운데 하나로 stock 을 적는다)
{stocks}

규칙:
- 증권사, 투자은행, 이름난 리서치 회사의 애널리스트 조치만 뽑는다.
- 개인 필자·개인 투자자의 글(Seeking Alpha 기고, "Rating Upgrade" 같은 개인 등급), 기관의 보유 지분 공시,
  여러 증권사 평균(컨센서스), 목표가 이야기가 없는 시황 기사는 뽑지 않는다. 그런 뉴스는 items 를 빈 목록으로.
- 관찰 종목이 아닌 회사에 대한 조치는 뽑지 않는다.
- action: 목표가를 올렸으면 "상향", 내렸으면 "하향", 목표가를 그대로 두거나 의견만 재확인하면 "유지",
  처음 분석을 시작하면 "신규", 목표가 변화 없이 의견만 올리면 "의견상향", 내리면 "의견하향".
  의견을 유지하면서 목표가를 올리면 "상향" 이다.
  "신규" 는 제목에 initiates, begins/starts coverage, 커버리지 개시, 신규 분석 같은 말이 있을 때만 쓴다.
  "Baird Sets $1,520 Target", "목표가 56만원", "RBC analyst says" 처럼 목표가·의견만 있고 올렸는지·내렸는지·
  처음인지 제목으로 알 수 없으면 "제시".
- rating: 투자의견을 한국어 짧은 말로 (매수, 비중확대, 아웃퍼폼, 중립, 보유, 매도 등). 제목에 없으면 "".
- old, new: 제목에 적힌 목표가 숫자만 쓴다 (쉼표 없이). 제목에 없으면 null. 짐작해서 만들지 마라.
- cur: 목표가 통화 (USD, KRW 등). 목표가가 없으면 "".
- broker: 증권사 이름. 영어 제목이면 영어 이름 그대로 (Baird, Morgan Stanley), 한국어 제목이면 한국어 이름 그대로.
- 한 제목에 증권사가 여럿이면 items 에 여러 개를 넣는다.

[뉴스]
{news}

JSON 만 출력하라:
{{"results": [{{"i": 번호, "items": [{{"stock": "관찰 종목 이름", "broker": "증권사", "action": "상향", "rating": "매수", "old": 1400, "new": 1520, "cur": "USD"}}]}}]}}
"""


def is_candidate(rec: dict) -> bool:
    """판별 기록이 목표가 뉴스 후보인가 (원문 제목·번역 제목·사건 이름으로 본다). 규칙으로 0점 매긴 것은 뺀다."""
    if rec.get("by") == "rule":
        return False
    text = " ".join(rec.get(k) or "" for k in ("title", "title_ko", "topic"))
    return bool(CANDIDATE.search(text))


def build_prompt(recs: list, stocks: list) -> str:
    return PROMPT.format(stocks=", ".join(stocks) or "(없음)",
                         news="\n".join(f"{i}. [{r.get('tickers', '')}] {r['title']}" for i, r in enumerate(recs, 1)))


def _num(v):
    try:
        x = float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None


def parse(text: str, recs: list, stocks: list) -> dict:
    """{뉴스 id: [항목, ...]}. 모델이 답한 뉴스만 들어 있다 (빈 목록 = 목표가 뉴스 아님)."""
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        items = json.loads(m.group(0)).get("results", []) if m else []
    except (ValueError, AttributeError):
        items = []
    names = {s.lower(): s for s in stocks}
    out = {}
    for it in items if isinstance(items, list) else []:
        try:
            i = int(it["i"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 1 <= i <= len(recs):
            continue
        got = []
        for x in it.get("items") or []:
            if not isinstance(x, dict):
                continue
            stock = names.get(str(x.get("stock", "")).strip().lower())
            broker = str(x.get("broker", "")).strip()
            action = str(x.get("action", "")).strip()
            if not stock or not broker or action not in ACTIONS:
                continue
            got.append({"stock": stock, "broker": broker, "action": action,
                        "rating": str(x.get("rating", "") or "").strip(),
                        "pt_old": _num(x.get("old")), "pt_new": _num(x.get("new")),
                        "currency": str(x.get("cur", "") or "").strip().upper()})
        out[recs[i - 1]["id"]] = got
    return out


# 뒤 꼬리를 떼고도 남는 이름 갈래 (09-29 뽑은 71건에서 본 것)
BROKER_ALIAS = {"jpmorganchase": "jpmorgan", "royalbankofcanada": "rbc", "bankofamerica": "bofa",
                "sanfordcbernstein": "bernstein", "citigroup": "citi",
                # 한국어 제목에 나오는 외국 증권사 (09-30 "번스타인, SK하이닉스 목표주가 하향")
                "번스타인": "bernstein", "골드만삭스": "goldmansachs", "모건스탠리": "morganstanley",
                "jp모건": "jpmorgan", "제이피모건": "jpmorgan", "씨티": "citi", "bofa": "bofa",
                "뱅크오브아메리카": "bofa", "맥쿼리": "macquarie", "노무라": "nomura", "ubs": "ubs",
                "제프리스": "jefferies", "바클레이즈": "barclays", "도이치": "deutsche", "도이체방크": "deutsche",
                "도이치방크": "deutsche", "deutschebank": "deutsche", "hsbc": "hsbc", "clsa": "clsa"}

# 한국어로 적힌 외국 증권사 이름 더 (10-01 공용 DB 를 훑어 넣음: saveticker 는 한국어, stocknews_filter 는 영어 이름이 많다)
BROKER_ALIAS.update({
    "bnp파리바스": "bnpparibas", "bnp파리바": "bnpparibas", "베렌베르크": "berenberg", "모닝스타": "morningstar",
    "오펜하이머": "oppenheimer", "웨드부시": "wedbush", "니덤": "needham", "미즈호": "mizuho", "베어드": "baird",
    "레이먼드제임스": "raymondjames", "키뱅크": "keybanc", "로젠블랫": "rosenblatt", "로젠블라트": "rosenblatt",
    "캔어코드제뉴이티": "canaccordgenuity", "아거스리서치": "argusresearch", "파이퍼샌들러": "pipersandler",
    "에버코어": "evercore", "에버코어isi": "evercore", "evercoreisi": "evercore", "웰스파고": "wellsfargo",
    "트루이스트": "truist", "스티펠": "stifel", "td코웬": "tdcowen", "구겐하임": "guggenheim", "캔터피츠제럴드": "cantorfitzgerald",
    "캔터": "cantorfitzgerald", "cantor": "cantorfitzgerald", "da데이비슨": "dadavidson", "멜리우스": "melius",
    "울프리서치": "wolferesearch", "번스타인소시에테제네랄": "bernstein", "뱅크오브아메리카메릴린치": "bofa", "bofa": "bofa"})

# 줄여 쓴 이름 (10-04 종목별 표의 Micron 에 "Goldman" 과 "Goldman Sachs", "Melius" 와 "Melius Research" 가 따로 나왔다)
BROKER_ALIAS.update({"goldman": "goldmansachs", "골드만": "goldmansachs", "meliusresearch": "melius"})


def broker_key(name: str) -> str:
    """같은 증권사의 이름 갈래를 합친다: "JPMorgan" / "J.P. Morgan" / "JPMorgan Chase & Co.",
    "Citi" / "Citigroup", "RBC Capital" / "Royal Bank Of Canada", "DS투자증권" / "DS투자證"."""
    s = re.sub(r"[^\w]", "", (name or "").lower())
    while True:
        t = re.sub(r"(securities|capital|markets|group|co|inc|llc|투자증권|투자證|증권|證)$", "", s)
        if t == s or not t:
            break
        s = t
    return BROKER_ALIAS.get(s, s)


def group(rows: list, days: int = 2) -> list:
    """같은 종목·같은 증권사의 조치가 days 일 안에 여러 기사로 오면 한 줄로 합친다 (목표가가 다르면 따로).
    rows 는 새것부터, 각 줄에 at(datetime) 이 있어야 한다. 합친 줄의 news 에 기사들이 들어간다.
    목표가를 제목에 안 적은 기사("DS, 하이닉스 목표가 낮춰")는 같은 조치의 목표가 적힌 기사와 합친다.
    구분은 기사들 가운데 가장 많은 것을 쓴다. 모델이 "Baird Sets $1,520 Target" 을 "신규" 로 잘못 붙이는 일이 있어서다."""
    out = []
    for r in rows:
        key = (r["stock"], broker_key(r["broker"]))
        for g in out:
            if (g["key"] == key and g["at"] - r["at"] <= timedelta(days=days)
                    and (g["pt_new"] is None or r["pt_new"] is None or g["pt_new"] == r["pt_new"])):
                g["news"].append(r)
                for k in ("rating", "pt_old", "pt_new", "currency"):
                    g[k] = g[k] or r[k]
                break
        else:
            out.append(dict(r, key=key, news=[r]))
    for g in out:
        acts = [x["action"] for x in g["news"]]
        acts = [a for a in acts if a != "제시"] or acts   # 방향을 아는 기사가 하나라도 있으면 그쪽
        g["action"] = max(dict.fromkeys(acts), key=acts.count)   # 같은 수면 새 기사 쪽
    return out


def latest_per_broker(groups: list) -> list:
    """한 종목의 줄들(새것부터)에서 증권사마다 가장 새 줄만 남긴다. 종목별 표가 쓴다.
    10-04 Micron 35줄 가운데 Goldman Sachs 가 둘($1,100 유지 → 다음 날 $1,250)처럼 겹친 증권사가 6곳이었다.
    새 줄에 목표가가 없으면 그 증권사의 앞선 줄에서 채운다 (JP Morgan "유지" 뒤에 사흘 전 "상향 $1,540" 이 가려졌다).
    새 줄이 상향·하향이면 채우지 않는다. 앞선 목표가는 바뀌기 전 값이다. 투자의견은 유지·제시·신규일 때만 채운다."""
    out, where = [], {}
    for g in groups:
        i = where.get(g["key"])
        if i is None:
            where[g["key"]] = len(out)
            out.append(dict(g))
            continue
        new = out[i]
        if new["pt_new"] is None and g["pt_new"] is not None and new["action"] not in ("상향", "하향"):
            new["pt_new"], new["currency"] = g["pt_new"], g["currency"]
        if not new["rating"] and g["rating"] and new["action"] in ("유지", "제시", "신규"):
            new["rating"] = g["rating"]
    return out
