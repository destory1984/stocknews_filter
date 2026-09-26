# stocknews_filter (종목 뉴스 알리미)

내가 고른 종목의 뉴스를 구글 뉴스와 야후 파이낸스에서 모아, LLM 이 알릴 만한 것만 골라 윈도우 알림과 음성으로 알려준다.
화면·음성·알림·판별 방식은 [saveticker_filter](https://github.com/destory1984/saveticker_filter) 와 같다. 다른 점은 뉴스를 받아 오는 곳뿐이다.

## 왜 만들었나

종목 이름으로 뉴스를 검색하면 하루에 종목당 27~64건이 나왔다 (2026-09-26, 삼성전자·SK하이닉스·NVIDIA). 대부분은 같은 소식을 여러 매체가 다시 쓴 기사이거나,
"펀드 X 가 주식 N 주를 샀다" 같은 자동 생성 기사다. 야후 파이낸스의 종목별 피드는 더 심해서,
2026-09-26 에 `NVDA` 피드 20건 가운데 제목이나 요약에 엔비디아가 나오는 기사는 3건뿐이었다.
그래서 키워드로 한 번 거르고, 남은 것을 LLM 이 내 관심사와 👍/👎 반응을 보고 다시 거르게 만들었다.

## 구동 방식

```
구글 뉴스 RSS ─┐                             stock_alert.py
               ├─ stocknews.py ──▶ data/stocknews_날짜.csv ──▶ LLM 판별 (ollama → 안 되면 claude CLI)
야후 RSS ──────┘  5분마다, 종목 키워드로 1차 거름                  7점 이상이면 윈도우 토스트 + 음성
                                                               http://127.0.0.1:18766  판별 목록 · 🔔10 👍 👎 🔕0 반응
```

saveticker_filter 는 사이트가 봇을 막아서 Edge 확장으로 뉴스를 모은다.
구글 뉴스와 야후 파이낸스는 RSS 를 공개하므로 여기서는 확장이 필요 없고, 브라우저를 띄워 둘 필요도 없다.

## 구성

| 파일 | 역할 |
|---|---|
| `stock_alert.py` | 5분마다 뉴스를 받아 판별하고 알린다. 판별 목록 페이지(18766)도 연다. saveticker_filter 의 `news_alert.py` 에서 뉴스 출처만 바꿨다 |
| `stocknews.py` | 구글 뉴스·야후 RSS 를 읽고 종목 키워드로 거른다. 혼자 돌려 목록만 볼 수도 있다 |
| `watchlist.json` | 종목 목록. `watchlist.example.json` 을 복사해 고친다 |
| `interests.md` | 판별 기준이 되는 관심사. 고치면 다음 판별부터 반영된다 |
| `stock_alert_config.json` | 모델, 기준 점수, 포트, 수집 간격 등. 처음 실행할 때 만들어진다 |
| `stock_alert_bg.vbs` / `stock_alert_stop.bat` | 창 없이 백그라운드 실행 / 종료 |

## 설치

1. `pip install requests winotify edge-tts pywin32`, 그리고 터미널에서 `claude` 실행 후 `/login` 한 번.
   ollama 가 켜져 있으면 그쪽을 먼저 쓴다 (`stock_alert_config.json` 의 `model`).
2. `watchlist.example.json` 을 `watchlist.json` 으로 복사하고 종목을 고친다.
3. `stock_alert_bg.vbs` 실행. 로그는 `stock_alert.log`, 목록은 `http://127.0.0.1:18766`.

## watchlist.json

| 항목 | 뜻 |
|---|---|
| `name` | 화면에 보일 이름. `google` 이 없으면 구글 뉴스 검색어로도 쓴다 |
| `google` | 구글 뉴스 검색어 |
| `lang` | 구글 뉴스 언어·지역. `ko`(기본) 또는 `en` |
| `yahoo` | 야후 파이낸스 티커. `NVDA`, `005930.KS`(코스피), `035720.KQ`(코스닥). 없으면 구글만 본다 |
| `keywords` | 제목이나 요약에 이 가운데 하나라도 있어야 남긴다. 없으면 이름과 티커 |
| `exclude` | 이 말이 들어간 기사는 버린다 |
| `exclude_sources` | 이 언론사 기사는 버린다. 영어 엔비디아 검색에는 MarketBeat 의 보유 공시 기사가 하루 10건 넘게 나와서 예시에 넣어 두었다 |

## 판별

saveticker_filter 와 같다.

- 새 뉴스를 최대 10건씩 모아(또는 90초 기다렸다가) 한 번에 묻는다. 프롬프트에 `interests.md` 와 최근 반응(🔔10·👍·👎·🔕0)이 들어간다.
- 뉴스마다 사건 이름을 붙여 같은 소식을 한 줄로 접는다. 알림은 같은 사건이면 한 시간에 한 번만 보낸다.
- 알림은 말로도 읽는다. saveticker_filter 와 구별되게 말머리 소리를 `Windows Notify Email.wav` 로 바꿨다.
  `python stock_alert.py --say "삼성전자 목표가 상향"` 으로 들어 볼 수 있다.
- 나온 지 120분(`max_age_min`)이 넘은 뉴스는 목록에만 올리고 알리지 않는다. saveticker_filter 는 60분이다.
  구글 뉴스는 기사가 나온 뒤 늦게 잡히기도 해서 늘렸다.
- `python stock_alert.py --test 15` 로 최근 15건을 판별만 해 볼 수 있다.

## 목록만 보고 싶을 때

LLM 없이 키워드 거름만 거친 목록이 필요하면 `stocknews.py` 를 쓴다. 표준 라이브러리만 쓴다.

```
python stocknews.py                  # watchlist.json 종목, 최근 하루
python stocknews.py NVDA 삼성전자     # 명령줄에서 바로
python stocknews.py --days 3 --all   # 3일치, 이미 본 기사도
python stocknews.py --telegram       # 새 기사를 텔레그램으로 (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 환경변수)
```

## 한계

- 구글 뉴스 링크는 `news.google.com` 을 한 번 거쳐 원문으로 간다.
- 구글 뉴스 RSS 는 검색 한 번에 최대 100건을 준다.
- 야후 파이낸스는 영어 기사 위주라 국내 종목은 외신 보도만 잡힌다.
- 구글 뉴스 RSS 약관은 개인적·비상업적 사용만 허용한다.
