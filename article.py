"""구글 뉴스 기사의 원문 주소를 찾고 본문 글자만 뽑는다 (요약용).

- 구글 뉴스 RSS 링크(news.google.com/rss/articles/...)는 원문 주소를 감춰 둔다.
  기사 페이지의 서명(data-n-a-sg)과 시각(data-n-a-ts)으로 batchexecute 를 불러 원문 주소를 받는다.
- 원문 사이트의 robots.txt 가 막으면 받지 않는다.
- 본문은 요약에만 쓰고 저장하지 않는다.
"""
import html
import json
import re
import urllib.parse
import urllib.robotparser

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) stocknews_filter/1.0"
HEADERS = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"}
TIMEOUT = 12
_robots = {}


class Skip(Exception):
    """요약할 수 없는 기사 (robots.txt 가 막음, 본문 없음 등). 까닭은 str(e)."""


class Busy(Exception):
    """구글이 요청이 많다며 막았다 (429, /sorry 페이지). 기사 탓이 아니니 나중에 다시 한다."""


# 기사 본문이 아닌 문단 (브라우저 안내, 저작권 문구 등)
BOILER = ("Internet Explorer", "최신 브라우저", "무단 전재", "무단전재", "재배포 금지", "저작권자",
          "Copyright ©", "All rights reserved")


def decode_google(url: str) -> str:
    """news.google.com 기사 링크 → 원문 주소. 구글 링크가 아니면 그대로."""
    if "news.google.com" not in url:
        return url
    aid = urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1]
    r = requests.get(f"https://news.google.com/articles/{aid}", headers=HEADERS, timeout=TIMEOUT)
    if r.status_code == 429 or "/sorry/" in r.url:
        raise Busy("구글이 잠시 막음 (429)")
    page = r.text
    sg = re.search(r'data-n-a-sg="([^"]+)"', page)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not (sg and ts):
        raise Skip("구글 링크를 풀지 못함")
    inner = (f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
             f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{aid}",{ts.group(1)},"{sg.group(1)}"]')
    r = requests.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                      headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                      data="f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner, None, "generic"]]])),
                      timeout=TIMEOUT)
    if r.status_code == 429:
        raise Busy("구글이 잠시 막음 (429)")
    try:
        body = r.text.split("\n\n", 1)[1]
        real = json.loads(json.loads(body)[0][2])[1]
    except (IndexError, ValueError, TypeError):
        raise Skip("구글 링크를 풀지 못함")
    if not str(real).startswith("http"):
        raise Skip("구글 링크를 풀지 못함")
    return real


def allowed(url: str) -> bool:
    """원문 사이트의 robots.txt 가 우리를 막는가. robots.txt 를 못 읽으면 허락으로 본다."""
    p = urllib.parse.urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    rp = _robots.get(base)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = requests.get(base + "/robots.txt", headers=HEADERS, timeout=6)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except requests.RequestException:
            rp.parse([])
        _robots[base] = rp
    return rp.can_fetch(UA, url)


def _text(fragment: str) -> str:
    fragment = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"[ \t\r\f\v]+", " ", html.unescape(fragment)).strip()


def extract(page: str) -> str:
    """기사 페이지 → 본문 글자. <p> 문단을 모으고, 모자라면 본문 칸(article·articleBody 등)을 통째로 읽는다."""
    page = re.sub(r"(?is)<(script|style|noscript|header|footer|nav|aside|form|figure)\b.*?</\1>", " ", page)
    paras = [_text(p) for p in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", page)]
    paras = [p for p in paras if len(p) >= 40 and not any(b in p for b in BOILER)]
    body = "\n".join(paras)
    if len(body) < 300:   # 국내 언론사는 <p> 없이 <br> 로 줄을 나누는 곳이 많다
        m = re.search(r'(?is)<(article|div)\b[^>]*(?:id|class|itemprop)="[^"]*(?:article[_-]?body|articleBody|'
                      r'article_txt|news_body|newsct_article|view_con|article-view-content)[^"]*"[^>]*>(.*?)</\1>', page)
        if m and len(_text(m.group(2))) > len(body):
            body = _text(m.group(2))
    if len(body) < 200:
        m = re.search(r'(?is)<meta[^>]+(?:property|name)="(?:og:description|description)"[^>]+content="([^"]+)"', page)
        if m:
            body = html.unescape(m.group(1)).strip()
    return body[:4000]


def fetch_body(url: str) -> tuple:
    """(원문 주소, 본문). 막히거나 비었으면 Skip."""
    real = decode_google(url)
    if not allowed(real):
        raise Skip("robots.txt 가 막음")
    r = requests.get(real, headers=HEADERS, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise Skip(f"HTTP {r.status_code}")
    r.encoding = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else r.apparent_encoding
    body = extract(r.text)
    if len(body) < 80:
        raise Skip("본문을 찾지 못함")
    return real, body
