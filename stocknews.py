"""Collect news for chosen stocks from Google News RSS and Yahoo Finance RSS.

Usage:
    python stocknews.py                      # stocks from watchlist.json
    python stocknews.py NVDA 삼성전자          # ad-hoc stocks from the command line
    python stocknews.py --days 3 --all       # last 3 days, include already-seen items
    python stocknews.py --telegram           # also send new items to Telegram

Only the standard library is used.
"""

import argparse
import csv
import email.utils
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
WATCHLIST = BASE / "watchlist.json"
SEEN_FILE = BASE / "seen.json"
OUT_DIR = BASE / "news"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) stocknews/1.0"
KST = timezone(timedelta(hours=9))
SEEN_KEEP_DAYS = 30

GOOGLE_LOCALE = {
    "ko": {"hl": "ko", "gl": "KR", "ceid": "KR:ko"},
    "en": {"hl": "en-US", "gl": "US", "ceid": "US:en"},
}


def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_date(text):
    if not text:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def clean(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_rss(raw):
    root = ET.fromstring(raw)
    for item in root.iter("item"):
        yield {
            "title": clean(item.findtext("title")),
            "link": (item.findtext("link") or "").strip(),
            "published": parse_date(item.findtext("pubDate")),
            "summary": clean(item.findtext("description")),
            "source": clean(item.findtext("source")),
        }


def google_news(stock, days):
    lang = stock.get("lang", "ko")
    query = stock.get("google") or stock["name"]
    params = dict(GOOGLE_LOCALE.get(lang, GOOGLE_LOCALE["ko"]))
    params["q"] = f"{query} when:{days}d"
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)
    for it in parse_rss(fetch(url)):
        # Google appends " - Publisher" to the title; the publisher is also in <source>.
        if it["source"] and it["title"].endswith(" - " + it["source"]):
            it["title"] = it["title"][: -len(it["source"]) - 3]
        # The Google description is just the title again wrapped in a link.
        it["summary"] = ""
        yield it


def yahoo_news(stock, days):
    symbol = stock.get("yahoo")
    if not symbol:
        return
    params = {"s": symbol, "region": "US", "lang": "en-US"}
    url = "https://feeds.finance.yahoo.com/rss/2.0/headline?" + urllib.parse.urlencode(params)
    for it in parse_rss(fetch(url)):
        it["source"] = it["source"] or "Yahoo Finance"
        yield it


SOURCES = {"google": google_news, "yahoo": yahoo_news}


def keep(item, stock, since):
    if item["published"] and item["published"] < since:
        return False
    if item["source"].lower() in (x.lower() for x in stock.get("exclude_sources", [])):
        return False
    text = (item["title"] + " " + item["summary"]).lower()
    excludes = [w.lower() for w in stock.get("exclude", [])]
    if any(w in text for w in excludes):
        return False
    keywords = [w.lower() for w in stock.get("keywords", []) if w]
    return not keywords or any(w in text for w in keywords)


def default_keywords(stock):
    """Words that must appear in the title or summary. Google search already
    matches the query, so this mostly trims Yahoo's loosely related items."""
    if "keywords" in stock:
        return stock["keywords"]
    words = [stock["name"], stock.get("google", "")]
    sym = stock.get("yahoo", "")
    if sym:
        words.append(sym.split(".")[0])
    return [w for w in dict.fromkeys(words) if w]


def stock_from_arg(arg):
    """Turn a bare command-line word into a stock entry.
    'NVDA' -> Google (English) + Yahoo, '005930.KS' -> Yahoo + Google by code,
    anything else (e.g. '삼성전자') -> Google Korean only."""
    if re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", arg):
        return {"name": arg, "google": f"{arg} stock", "yahoo": arg, "lang": "en",
                "keywords": [arg]}
    if re.fullmatch(r"\d{6}\.(KS|KQ)", arg):
        return {"name": arg, "google": arg.split(".")[0], "yahoo": arg}
    return {"name": arg}


def load_watchlist():
    if not WATCHLIST.exists():
        sys.exit(f"{WATCHLIST.name} not found. Copy watchlist.example.json to "
                 f"{WATCHLIST.name} or pass stock names on the command line.")
    with open(WATCHLIST, encoding="utf-8") as f:
        return json.load(f)


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_KEEP_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=0)


def norm_title(title):
    return re.sub(r"[\W_]+", "", title.lower())


def collect(stocks, days, sources):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = []
    for stock in stocks:
        stock = dict(stock, keywords=default_keywords(stock))
        titles = set()
        for name in sources:
            try:
                items = list(SOURCES[name](stock, days))
            except (urllib.error.URLError, ET.ParseError, TimeoutError) as e:
                # Print only the error type/reason, never the full request.
                print(f"  [{stock['name']}] {name} failed: {type(e).__name__}", file=sys.stderr)
                continue
            for it in items:
                key = norm_title(it["title"])
                if not key or key in titles or not keep(it, stock, since):
                    continue
                titles.add(key)
                rows.append({"stock": stock["name"], "feed": name, **it})
    rows.sort(key=lambda r: r["published"] or since, reverse=True)
    return rows


def write_csv(rows):
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"{datetime.now(KST):%Y-%m-%d}.csv"
    new_file = not path.exists()
    fields = ["published", "stock", "feed", "source", "title", "link", "summary"]
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow({**r, "published": fmt_time(r["published"])})
    return path


def fmt_time(dt):
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M") if dt else ""


def send_telegram(rows):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; skipping Telegram.", file=sys.stderr)
        return
    lines = []
    for r in rows:
        lines.append(f"[{r['stock']}] {html.escape(r['title'])}\n"
                     f"<a href=\"{html.escape(r['link'])}\">{html.escape(r['source'] or r['feed'])}</a>"
                     f" {fmt_time(r['published'])}")
    # Telegram caps a message at 4096 chars; send in chunks.
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 2 > 3800:
            _tg_send(token, chat_id, chunk)
            chunk = ""
        chunk += line + "\n\n"
    if chunk:
        _tg_send(token, chat_id, chunk)


def _tg_send(token, chat_id, text):
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except urllib.error.HTTPError as e:
        # The URL contains the token, so report only the status code.
        print(f"Telegram send failed: HTTP {e.code}", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"Telegram send failed: {type(e.reason).__name__}", file=sys.stderr)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Stock news from Google News and Yahoo Finance")
    p.add_argument("stocks", nargs="*", help="stock names or tickers (default: watchlist.json)")
    p.add_argument("--days", type=int, default=1, help="look back N days (default 1)")
    p.add_argument("--source", choices=["google", "yahoo", "both"], default="both")
    p.add_argument("--all", action="store_true", help="show items already seen in earlier runs")
    p.add_argument("--no-csv", action="store_true", help="do not write news/YYYY-MM-DD.csv")
    p.add_argument("--telegram", action="store_true", help="send new items to Telegram")
    args = p.parse_args()

    stocks = [stock_from_arg(a) for a in args.stocks] if args.stocks else load_watchlist()
    sources = ["google", "yahoo"] if args.source == "both" else [args.source]

    rows = collect(stocks, args.days, sources)
    seen = load_seen()
    if not args.all:
        rows = [r for r in rows if r["link"] not in seen]
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        seen[r["link"]] = now
    save_seen(seen)

    if not rows:
        print("No new articles.")
        return
    for r in rows:
        print(f"{fmt_time(r['published'])}  [{r['stock']}] {r['title']}  "
              f"({r['source'] or r['feed']})\n    {r['link']}")
    print(f"\n{len(rows)} articles.")
    if not args.no_csv:
        print(f"Saved to {write_csv(rows).relative_to(BASE)}")
    if args.telegram:
        send_telegram(rows)


if __name__ == "__main__":
    main()
