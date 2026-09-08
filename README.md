# CMRC IB Daily News Run - 종가 수집기

`_26-2__IB_Daily_News_Run.xlsx` 의 Data 시트 표 7개를 채우기 위한 종가를
매 평일 아침 자동 수집해 `data/latest.json` 으로 게시한다.

경제지표 5개 표(CPI/PPI/PCE/PMI/NFP)는 월간 발표라 수기 유지한다.

## 실행 규칙

| 항목 | 값 |
|---|---|
| 실행 시각 | 한국시간 평일 08:00 (`cron: 0 23 * * 0-4`, UTC 기준) |
| 목표 날짜 | 실행일 기준 직전 영업일 |
| 백필 | 매 실행마다 최근 평일 10개분을 통째로 게시 |
| 휴장 처리 | 직전 거래일 종가를 이어 적고 `carried_forward: true` 표시 |

행 날짜는 **모든 표가 한국 평일(월~금) 기준**으로 통일한다. 원본 파일이
5/25 메모리얼데이(미국 휴장) 행을 5/22 값으로 채워 둔 관행을 따른 것이다.
미국·영국 휴장일에도 행은 생기고 직전값이 들어가며 `carried_forward: true`
가 붙는다.

한국 공휴일이 평일에 걸리면 그날 행도 생성되고 국내 표는 직전값이 들어간다.
그날 뉴스런을 쉬었다면 다음 실행 때 백필로 함께 삽입되므로, 필요 없으면
Office Script 쪽에서 걸러야 한다.

## 세팅

1. 이 저장소를 **public** 으로 생성 (Office Script 가 raw URL 로 fetch 하기 때문)
2. https://ecos.bok.or.kr 에서 오픈API 인증키 발급 (무료)
3. KRX 데이터 마켓플레이스(정보데이터시스템) 계정 생성
   - 2025-12-27 회원제 전환으로 로그인이 필수가 됐다. 조회 자체는 무료.
4. Settings > Secrets and variables > Actions > New repository secret 로 3개 등록
   - `ECOS_API_KEY` : 한국은행 ECOS 인증키
   - `KRX_ID` / `KRX_PW` : KRX 계정 아이디/비밀번호 (pykrx 가 직접 읽는다)
5. Actions 탭 > `daily-market-close` > **Run workflow** 로 1회 수동 실행해서 검증

## 게시 URL

https://raw.githubusercontent.com/{계정}/{저장소}/main/data/latest.json

## 소스

| 표 | 소스 | 키 필요 |
|---|---|---|
| 미국증시 / 해외주요국증시 / 환율 / 원자재 | Yahoo Finance (yfinance) | 없음 |
| 국내증시 | KRX (pykrx) | `KRX_ID` / `KRX_PW` |
| 국내채권 | 한국은행 ECOS 오픈API (817Y002 시장금리 일별) | `ECOS_API_KEY` |
| 미국채 | U.S. Treasury 일별 수익률곡선 CSV | 없음 |

## 출력 형식

```json
{
  "generated_at": "2026-09-21T08:00:12+09:00",
  "target_date": "2026-09-18",
  "backfill_business_days": 10,
  "tables": {
    "미국증시": {
      "columns": ["Dow Jones", "Nasdaq", "S&P 500", "Phili 반도체", "Russell 2000", "VIX"],
      "rows": [
        { "date": "2026-09-18", "values": { "Dow Jones": 50668.97 }, "carried_forward": false },
        { "date": "2026-09-17", "values": { "Dow Jones": 50644.28 }, "carried_forward": true }
      ]
    }
  },
  "errors": []
}
```

`rows` 는 최신순(시트와 같은 방향)이다. Office Script 는 시트 맨 위 날짜와 비교해
그보다 새로운 행만 삽입한다. 그래서 매일 눌러도 중복이 생기지 않고, 연휴로
며칠 건너뛰어도 한 번에 메워진다.

`tables` 의 키는 엑셀 파일의 ListObject 이름과, `columns` 는 표 헤더와 정확히
일치한다. 표 이름이나 헤더를 바꾸면 `fetch_market_data.py` 의 `TABLE_COLUMNS` 도
같이 고쳐야 한다.

## 알려진 이슈

- 미국채 표의 `US 11Y` 는 존재하지 않는 만기다. 원본 파일의 오기로 보여 30Y 값을
  넣고 있다. 헤더를 고칠 때 `fetch_us_rates()` 의 mapping 도 같이 바꿀 것.
- Treasury 일별 수익률곡선은 미 동부시간 저녁에 갱신된다. 08:00 KST 실행이면
  보통 여유가 있지만, 갱신이 늦으면 그날 값이 carry-forward 로 채워진다.
- 국내증시는 pykrx 가 KRX 를 스크래핑하는 방식이라, KRX 가 화면이나 로그인
  절차를 바꾸면 깨진다. 안정성이 필요해지면 openapi.krx.co.kr 의 공식 Open API
  (무료 인증키 + 서비스별 승인)로 옮기는 편이 낫다.
- `yfinance` 는 야후 비공식 엔드포인트를 쓴다. 안정 버전이 확인되면
  `requirements.txt` 에 핀을 걸어둘 것.
