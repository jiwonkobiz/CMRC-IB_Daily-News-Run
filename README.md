# CMRC IB Daily News Run — 종가 수집기

`_26-2__IB_Daily_News_Run.xlsx` 의 Data 시트 표 7개를 채우기 위한 종가를
매 평일 아침 자동 수집해 `data/latest.json` 으로 게시한다.

경제지표 5개 표(CPI/PPI/PCE/PMI/NFP)는 월간 발표라 수기 유지한다.

## 전체 구조

두 단계로 나뉜다. 수집은 무인이고, 시트 기입만 사람이 버튼을 누른다.

| 단계 | 실행 주체 | 자동 여부 |
|---|---|---|
| 1. 종가 수집 → `data/latest.json` 게시 | GitHub Actions | 매 평일 08:30 자동 |
| 2. JSON 읽어 Data 시트 표 7개에 행 삽입 | Office Script | 당번이 클릭 1회 |

2단계를 자동화할 수 없는 이유는, Power Automate 를 통해 실행하면 Office Script 의
`fetch` 가 동작하지 않기 때문이다. 반드시 Excel 에서 직접 실행해야 한다.

## 실행 규칙

| 항목 | 값 |
|---|---|
| 실행 시각 | 한국시간 평일 08:30 (`cron: "30 23 * * 0-4"`, UTC 기준) |
| 목표 날짜 | 미국장 마감이 확정된 마지막 평일 |
| 목표일 인정 조건 | 현재 시각 >= D+1 08:00 KST (`CUTOFF`) |
| 백필 | 매 실행마다 최근 평일 10개분을 통째로 게시 |
| 휴장 처리 | 직전 거래일 종가를 이어 적고 `carried_forward: true` 표시 |

**`CUTOFF` 는 cron 시각보다 앞서야 한다.** 이 조건이 깨지면 스크립트가 전날을
거부하고 그 전날로 물러나 하루 밀린 데이터가 나온다. 실행 시각을 바꿀 때는
`fetch_market_data.py` 의 `CUTOFF` 도 함께 확인할 것.

미국 정규장 종가는 KST 새벽 5시(서머타임 해제 시 6시), 미 재무부 수익률곡선은
KST 아침 7시경 확정된다. 08:00 커트오프는 두 소스 모두를 넘긴 시점이다.

## 행 구성

행 날짜는 모든 표가 **한국 평일(월~금) 기준**으로 통일한다. 원본 파일이
5/25 메모리얼데이(미국 휴장) 행을 5/22 값으로 채워 둔 관행을 따른 것이다.

- **단일 시장 표**(미국증시 / 국내증시 / 미국채 / 국내채권)는 기준 컬럼 하나로
  개장 여부를 판정해 행 전체가 같은 날짜에서 온 값이 되게 한다. 노동절에 VIX 만
  유령 값이 들어오는 식의 혼합 행을 막는다.
- **복수 시장 표**(해외주요국증시 / 환율 / 원자재)는 컬럼별로 판정한다. 영국만
  쉬고 나머지는 여는 날을 제대로 처리하려면 이쪽이 맞다.

한국 공휴일이 평일에 걸리면 그날 행도 생성되고 국내 표는 직전값이 들어간다.
그날 뉴스런을 쉬었다면 다음 실행 때 백필로 함께 삽입된다.

## 세팅

1. 저장소를 **public** 으로 생성 (Office Script 가 raw URL 로 fetch 하기 때문)
2. https://ecos.bok.or.kr 에서 오픈API 인증키 발급 (무료)
3. https://data.krx.co.kr 회원가입
   — 2025-12-27 회원제 전환으로 로그인이 필수가 됐다. 조회 자체는 무료.
4. Settings > Secrets and variables > Actions 에서 3개 등록
   - `ECOS_API_KEY` : 한국은행 ECOS 인증키
   - `KRX_ID` / `KRX_PW` : KRX 계정 (pykrx 가 환경변수로 직접 읽는다)
5. Settings > Actions > General > Workflow permissions 를
   **Read and write permissions** 로 설정
   — 이걸 빼면 수집은 성공해도 마지막 커밋 단계에서 403 으로 죽는다.
6. Actions 탭 > `daily-market-close` > Run workflow 로 1회 검증

## 게시 URL

```
https://raw.githubusercontent.com/{계정}/{저장소}/refs/heads/main/data/latest.json
```

`refs/heads/main` 은 항상 최신 커밋을 가리키므로 주소가 고정된다.
Office Script 의 `DATA_URL` 에 이 값을 넣는다.

## 데이터 소스

| 표 | 소스 | 키 |
|---|---|---|
| 미국증시 | Yahoo `^DJI ^IXIC ^GSPC ^SOX ^RUT ^VIX` | 없음 |
| 해외주요국증시 | Yahoo `^STOXX50E ^FTSE 000001.SS ^HSI ^HSCE ^N225 ^TWII` | 없음 |
| 국내증시 | KRX (pykrx) — 코스피/코스피200/코스닥/코스닥150/코리아 밸류업 | `KRX_ID` `KRX_PW` |
| 국내채권 | 한국은행 ECOS `817Y002` 시장금리(일별) | `ECOS_API_KEY` |
| 미국채 | U.S. Treasury 일별 수익률곡선 CSV | 없음 |
| 환율 | Yahoo `KRW=X EURKRW=X JPYKRW=X EURUSD=X JPY=X` | 없음 |
| 원자재 | Yahoo `CL=F BZ=F NG=F GC=F HG=F` — 선물 최근월물 | 없음 |

KRX 지수코드와 ECOS 항목코드는 하드코딩하지 않고 **이름으로 역조회**한다.
소스가 코드를 바꿔도 이름 매칭이 살아 있으면 계속 동작하며, 무엇에 매칭됐는지
실행 로그와 JSON 의 `sources` 필드에 남는다.

## 출력 형식

```json
{
  "generated_at": "2026-09-10T08:30:12+09:00",
  "target_date": "2026-09-09",
  "backfill_business_days": 10,
  "sources": { "국내증시/KOSPI": "KRX 코스피 (1001)" },
  "tables": {
    "미국증시": {
      "columns": ["Dow Jones", "Nasdaq", "S&P 500", "Phili 반도체", "Russell 2000", "VIX"],
      "rows": [
        { "date": "2026-09-09", "values": { "Dow Jones": 50668.97 }, "carried_forward": false }
      ]
    }
  },
  "errors": []
}
```

`rows` 는 최신순(시트와 같은 방향)이다. Office Script 는 시트 맨 위 날짜와 비교해
그보다 새로운 행만 삽입한다. 그래서 매일 눌러도 중복이 생기지 않고, 연휴로
며칠 건너뛰어도 한 번에 메워진다.

`tables` 의 키는 엑셀 ListObject 이름과, `columns` 는 표 헤더와 정확히 일치한다.
표 이름이나 헤더를 바꾸면 `fetch_market_data.py` 의 `TABLE_SPEC` 과
Office Script 의 `NUMBER_FORMATS` 도 같이 고쳐야 한다.

## Office Script (2단계)

`InsertMarketData.ts` 를 Excel Online > Automate > 새 스크립트에 붙여넣는다.

- **본인 OneDrive 기본 경로**(`/Documents/Office Scripts/`)에 저장해야 한다.
  SharePoint 사이트에 저장된 스크립트는 외부 호출이 지원되지 않는다.
- 컬럼은 위치가 아니라 헤더 이름으로 매칭한다. 하나라도 다르면 그 표는 통째로
  건너뛰고 경고를 남긴다 — 반쯤 잘못 채우는 것보다 안 넣는 편이 낫다.
- 삽입한 블록에만 숫자 서식과 정렬을 명시적으로 지정한다. Fixed Income 시트의
  기존 텍스트 서식(`@`) 오염이 새 행으로 번지는 것을 막기 위함이다.

## 과거 구간 백필

표 맨 위 날짜와 피드 범위 사이가 벌어져 있으면 Office Script 가 경고한다.
메우려면 `fetch_market_data.py` 에서 두 값을 올리고 한 번 실행한 뒤 되돌린다.

```python
BACKFILL_BUSINESS_DAYS = 75
FETCH_CALENDAR_DAYS = 150
