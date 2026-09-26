# stocknews

Collects news only for the stocks you pick, from Google News and Yahoo Finance.
Both sites publish RSS feeds, so it reads those instead of scraping HTML pages.
It uses only the Python standard library (tested on 3.13), so there is nothing to install.

## Usage

```
cp watchlist.example.json watchlist.json   # then edit it
python stocknews.py                         # news from the last day for the stocks in watchlist.json
python stocknews.py NVDA 삼성전자            # quick lookup from the command line
python stocknews.py --days 3                # last 3 days
python stocknews.py --all                   # include articles already shown in earlier runs
python stocknews.py --source yahoo          # one source only (google / yahoo / both)
python stocknews.py --telegram              # also send new articles to Telegram
```

- Results are printed to the screen and appended to `news/YYYY-MM-DD.csv` (use `--no-csv` to skip).
- Links you have already seen are stored in `seen.json`, so running it again shows only new articles. Entries older than 30 days are removed from it.
- `--telegram` reads the `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` environment variables.

## watchlist.json

| Field | Meaning |
|---|---|
| `name` | Name to display. Also the default Google News search term |
| `google` | Google News search term (defaults to `name`) |
| `lang` | Google News language/region. `ko` (default) or `en` |
| `yahoo` | Yahoo Finance ticker. `NVDA`, `005930.KS` (KOSPI), `035720.KQ` (KOSDAQ). Omit it to use Google only |
| `keywords` | An article is kept only if its title or summary contains at least one of these words. Defaults to the name and the ticker |
| `exclude` | Drop articles containing these words |
| `exclude_sources` | Drop articles from these publishers (e.g. `MarketBeat`) |

## Why the keyword filter is needed

Yahoo's per-ticker feed also returns articles unrelated to the stock.
On 2026-09-26, the `NVDA` feed returned 20 articles, and only 3 of them mentioned NVIDIA in the title or summary.
The rest were about other companies, such as Microsoft and AppLovin, or about ETFs.

For English-language NVIDIA searches on Google News, MarketBeat posts about 10 articles a day along the lines of "fund X bought N shares".
Add `exclude_sources` to drop them.

## Limits

- Google News links go through a `news.google.com/rss/articles/...` redirect. They open the original article in a browser.
- Google News RSS returns at most 100 articles per search.
- Yahoo Finance is based on English articles. For Korean stocks you mostly get foreign-press coverage.
- The Google News RSS terms allow only personal, non-commercial use.
