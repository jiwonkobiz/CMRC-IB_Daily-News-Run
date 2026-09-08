"""
CMRC IB Daily News Run - 전일 종가 수집기

한국시간 매 평일 08:00 에 실행되어, 직전 영업일까지의 종가를 data/latest.json 으로
게시한다. Office Script 가 이 파일을 읽어 Data 시트 표 7개에 행을 삽입한다.

운영 규칙
  - 뉴스런은 평일(월~금)만 돌아간다.
  - 행 날짜는 모든 표가 한국 평일 기준으로 통일한다. 원본 파일이 5/25
    메모리얼데이(미국 휴장) 행을 5/22 값으로 채워 둔 관행을 따른 것이다.
  - 목표일에 그 시장이 휴장이었으면 직전 거래일 종가를 이어 적고
    carried_forward=true 로 표시한다.
  - 연휴로 며칠 건너뛰어도 메워지도록, 매 실행마다 최근 평일
    BACKFILL_BUSINESS_DAYS 개분을 통째로 게시한다. 시트에 이미 있는 날짜를
    거르는 일은 Office Script 쪽에서 한다.

경제지표 5개 표(CPI/PPI/PCE/PMI/NFP)는 월간 발표라 수기 유지한다.

필요한 시크릿
  ECOS_API_KEY   한국은행 ECOS 오픈API 인증키
  KRX_ID/KRX_PW  KRX 데이터 마켓플레이스 계정. 2025-12-27 회원제 전환 이후
                 pykrx 가 이 환경변수를 직접 읽어 로그인한다.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

KST = timezone(timedelta(hours=9))
OUT_PATH = Path(__file__).parent / "data" / "latest.json"

BACKFILL_BUSINESS_DAYS = 10   # 게시할 평일 개수 (연휴 대비 2주치)
FETCH_CALENDAR_DAYS = 40      # 소스에서 끌어올 달력일 범위

# 표별 컬럼 순서와 소수점 자릿수.
# 엑셀 표 헤더와 문자열이 정확히 일치해야 하고, 자릿수는 원본 파일의 기존
# 표기를 따른다 (Gold 4499.3, 엔/원 9.3798 등).
TABLE_SPEC: dict[str, list[tuple[str, int]]] = {
    "미국증시": [
        ("Dow Jones", 2), ("Nasdaq", 2), ("S&P 500", 2),
        ("Phili 반도체", 2), ("Russell 2000", 2), ("VIX", 2),
    ],
    "해외주요국증시": [
        ("유로스톡스50", 2), ("영국", 2), ("상해종합", 2), ("항셍", 2),
        ("홍콩 H", 2), ("Nikkei 225", 2), ("대만", 2),
    ],
    "국내증시": [
        ("KOSPI", 2), ("KOSPI 200", 2), ("KOSDAQ", 2),
        ("KOSDAQ 150", 2), ("K밸류업", 2),
    ],
    "국내채권": [
        ("KR 2Y", 3), ("KR 3Y", 3), ("KR 10Y", 3),
        ("SB 3Y(AA-)", 3), ("CP91", 2),
    ],
    "미국채": [("US 2Y", 3), ("US 5Y", 3), ("US 10Y", 3), ("US 11Y", 3)],
    "환율": [
        ("달러/원", 2), ("유로/원", 2), ("엔/원", 4),
        ("유로/달러", 4), ("달러/엔", 2),
    ],
    "원자재": [
        ("WTI", 2), ("Brent", 2), ("천연가스", 3), ("Gold", 2), ("구리", 4),
    ],
}

TABLE_COLUMNS = {t: [c for c, _ in spec] for t, spec in TABLE_SPEC.items()}
TABLE_DIGITS = {t: dict(spec) for t, spec in TABLE_SPEC.items()}

errors: list[str] = []

# series[표][컬럼] = {date: raw float}  (반올림은 build_rows 에서 한 번에)
series: dict[str, dict[str, dict[date, float]]] = {}


def log(msg: str) -> None:
    print(msg, flush=True)


def prev_business_day(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def weekdays_back(end: date, count: int) -> list[date]:
    """end 를 포함해 과거로 count 개의 평일 (오래된 순)."""
    out: list[date] = []
    d = end
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


# ---------------------------------------------------------------------------
# Yahoo Finance (미국증시 / 해외주요국증시 / 환율 / 원자재)
# ---------------------------------------------------------------------------

def yahoo_series(table: str, mapping: dict[str, str]) -> dict[str, dict[date, float]]:
    import yfinance as yf

    tickers = list(mapping.values())
    end = datetime.now(KST).date() + timedelta(days=1)
    start = end - timedelta(days=FETCH_CALENDAR_DAYS)

    df = yf.download(
        tickers,
        start=start.isoformat(),
        end=end.isoformat(),
        progress=False,
        auto_adjust=False,
        group_by="column",
    )
    if df is None or df.empty:
        raise RuntimeError("yfinance 응답이 비어 있음")

    close = df["Close"]
    out: dict[str, dict[date, float]] = {}

    for col, ticker in mapping.items():
        try:
            s = close[ticker] if len(tickers) > 1 else close
        except KeyError:
            errors.append(f"{table}/{col}: 티커 {ticker} 응답 없음")
            out[col] = {}
            continue
        s = s.dropna()
        out[col] = {idx.date(): float(v) for idx, v in s.items()}
        if not out[col]:
            errors.append(f"{table}/{col}: 최근 {FETCH_CALENDAR_DAYS}일 데이터 없음")

    return out


def fetch_us_equity():
    return yahoo_series("미국증시", {
        "Dow Jones": "^DJI",
        "Nasdaq": "^IXIC",
        "S&P 500": "^GSPC",
        "Phili 반도체": "^SOX",
        "Russell 2000": "^RUT",
        "VIX": "^VIX",
    })


def fetch_global_equity():
    return yahoo_series("해외주요국증시", {
        "유로스톡스50": "^STOXX50E",
        "영국": "^FTSE",
        "상해종합": "000001.SS",
        "항셍": "^HSI",
        "홍콩 H": "^HSCE",
        "Nikkei 225": "^N225",
        "대만": "^TWII",
    })


def fetch_fx():
    # 엔/원은 1엔 기준 (원본 파일이 9.38 형태로 기록 중)
    return yahoo_series("환율", {
        "달러/원": "KRW=X",
        "유로/원": "EURKRW=X",
        "엔/원": "JPYKRW=X",
        "유로/달러": "EURUSD=X",
        "달러/엔": "JPY=X",
    })


def fetch_commodity():
    return yahoo_series("원자재", {
        "WTI": "CL=F",
        "Brent": "BZ=F",
        "천연가스": "NG=F",
        "Gold": "GC=F",
        "구리": "HG=F",
    })


# ---------------------------------------------------------------------------
# 국내증시 (pykrx - KRX 로그인 필요)
# ---------------------------------------------------------------------------

def fetch_kr_equity():
    if not os.environ.get("KRX_ID") or not os.environ.get("KRX_PW"):
        raise RuntimeError(
            "KRX_ID / KRX_PW 시크릿이 설정되지 않음. "
            "KRX 정보데이터시스템이 2025-12-27 회원제로 바뀌어 로그인이 필수다."
        )

    from pykrx import stock

    # 지수 코드는 하드코딩하지 않고 이름으로 역조회한다.
    wanted = {
        "KOSPI": ["코스피"],
        "KOSPI 200": ["코스피 200"],
        "KOSDAQ": ["코스닥"],
        "KOSDAQ 150": ["코스닥 150"],
        "K밸류업": ["코리아 밸류업", "밸류업"],
    }

    today = datetime.now(KST).date()
    name_to_code: dict[str, str] = {}
    for market in ("KOSPI", "KOSDAQ", "KRX", "테마"):
        try:
            for code in stock.get_index_ticker_list(
                date=today.strftime("%Y%m%d"), market=market
            ):
                name_to_code.setdefault(stock.get_index_ticker_name(code), code)
        except Exception as exc:
            log(f"  ! 지수 목록 조회 실패 ({market}): {type(exc).__name__}: {exc}")

    log(f"  조회된 지수 {len(name_to_code)}개")
    if not name_to_code:
        raise RuntimeError("지수 목록이 비어 있음 - KRX 로그인 또는 접근이 막혔을 가능성")

    start = (today - timedelta(days=FETCH_CALENDAR_DAYS)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    out: dict[str, dict[date, float]] = {}
    for col, keywords in wanted.items():
        code = matched = None
        for name, c in name_to_code.items():
            if name.strip() == keywords[0]:
                code, matched = c, name
                break
        if code is None:
            for name, c in name_to_code.items():
                if any(k in name for k in keywords):
                    code, matched = c, name
                    break
        if code is None:
            errors.append(f"국내증시/{col}: 지수 코드를 찾지 못함")
            out[col] = {}
            continue

        log(f"  {col} <- '{matched}' ({code})")
        df = stock.get_index_ohlcv(start, end, code)
        if df is None or df.empty:
            errors.append(f"국내증시/{col}: OHLCV 비어 있음 (code={code})")
            out[col] = {}
            continue
        out[col] = {idx.date(): float(v) for idx, v in df["종가"].items()}

    return out


# ---------------------------------------------------------------------------
# 국내채권 (한국은행 ECOS)
# ---------------------------------------------------------------------------

ECOS_STAT_CODE = "817Y002"  # 시장금리(일별)


def _ecos_item_codes(api_key: str) -> dict[str, str]:
    url = f"https://ecos.bok.or.kr/api/StatisticItemList/{api_key}/json/kr/1/500/{ECOS_STAT_CODE}"
    res = requests.get(url, timeout=30)
    res.raise_for_status()
    body = res.json()
    if "StatisticItemList" not in body:
        raise RuntimeError(
            f"ECOS 항목목록 응답 이상: {json.dumps(body, ensure_ascii=False)[:300]}"
        )
    return {r["ITEM_NAME"].strip(): r["ITEM_CODE"] for r in body["StatisticItemList"]["row"]}


def fetch_kr_rates():
    api_key = os.environ.get("ECOS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ECOS_API_KEY 시크릿이 설정되지 않음")

    items = _ecos_item_codes(api_key)
    log(f"  ECOS {ECOS_STAT_CODE} 항목 {len(items)}개")

    wanted = {
        "KR 2Y": ["국고채(2년)"],
        "KR 3Y": ["국고채(3년)"],
        "KR 10Y": ["국고채(10년)"],
        "SB 3Y(AA-)": ["회사채(3년, AA-)", "회사채(3년,AA-)"],
        "CP91": ["CD(91일)", "CP(91일)"],
    }

    today = datetime.now(KST).date()
    start = (today - timedelta(days=FETCH_CALENDAR_DAYS)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    out: dict[str, dict[date, float]] = {}
    for col, candidates in wanted.items():
        code = matched = None
        for cand in candidates:
            for name, c in items.items():
                if name.replace(" ", "") == cand.replace(" ", ""):
                    code, matched = c, name
                    break
            if code:
                break
        if code is None:
            key = candidates[0].split("(")[0]
            for name, c in items.items():
                if key in name:
                    code, matched = c, name
                    break
        if code is None:
            errors.append(f"국내채권/{col}: ECOS 항목을 찾지 못함")
            out[col] = {}
            continue

        log(f"  {col} <- '{matched}' ({code})")
        url = (
            f"https://ecos.bok.or.kr/api/StatisticSearch/{api_key}/json/kr/1/200/"
            f"{ECOS_STAT_CODE}/D/{start}/{end}/{code}"
        )
        res = requests.get(url, timeout=30)
        res.raise_for_status()
        body = res.json()
        if "StatisticSearch" not in body:
            errors.append(f"국내채권/{col}: 조회 결과 없음 ({matched})")
            out[col] = {}
            continue
        out[col] = {
            datetime.strptime(r["TIME"], "%Y%m%d").date(): float(r["DATA_VALUE"])
            for r in body["StatisticSearch"]["row"]
            if r.get("DATA_VALUE") not in (None, "")
        }

    return out


# ---------------------------------------------------------------------------
# 미국채 (U.S. Treasury 일별 수익률곡선 CSV - API 키 불필요)
# ---------------------------------------------------------------------------

def fetch_us_rates():
    today = datetime.now(KST).date()
    years = {today.year, (today - timedelta(days=FETCH_CALENDAR_DAYS)).year}

    rows: list[dict] = []
    for year in sorted(years):
        url = (
            "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
            f"daily-treasury-rates.csv/{year}/all"
            f"?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
        )
        res = requests.get(url, timeout=30, headers={"User-Agent": "cmrc-newsrun/1.0"})
        res.raise_for_status()
        rows.extend(csv.DictReader(io.StringIO(res.text)))

    if not rows:
        raise RuntimeError("Treasury CSV 가 비어 있음")

    # 'US 11Y' 는 원본 파일의 오기로 보이며 30Y 값을 넣는다.
    mapping = {"US 2Y": "2 Yr", "US 5Y": "5 Yr", "US 10Y": "10 Yr", "US 11Y": "30 Yr"}

    out: dict[str, dict[date, float]] = {c: {} for c in mapping}
    for r in rows:
        try:
            d = datetime.strptime(r["Date"], "%m/%d/%Y").date()
        except (KeyError, TypeError, ValueError):
            continue
        for col, src in mapping.items():
            raw = (r.get(src) or "").strip()
            if raw:
                out[col][d] = float(raw)

    for col in mapping:
        if not out[col]:
            errors.append(f"미국채/{col}: Treasury CSV 에 값 없음")

    return out


# ---------------------------------------------------------------------------
# 조립
# ---------------------------------------------------------------------------

JOBS = [
    ("미국증시", fetch_us_equity),
    ("해외주요국증시", fetch_global_equity),
    ("국내증시", fetch_kr_equity),
    ("국내채권", fetch_kr_rates),
    ("미국채", fetch_us_rates),
    ("환율", fetch_fx),
    ("원자재", fetch_commodity),
]


def build_rows(table: str, cal_dates: list[date]) -> list[dict]:
    """평일마다 한 행. 값이 없는 날은 직전 거래일 값을 이어 적는다."""
    digits = TABLE_DIGITS[table]
    data = series.get(table, {})
    rows: list[dict] = []

    for d in cal_dates:
        values: dict[str, float | None] = {}
        carried = False
        for col in TABLE_COLUMNS[table]:
            points = data.get(col, {})
            if d in points:
                values[col] = round(points[d], digits[col])
                continue
            earlier = [pd for pd in points if pd < d]
            if earlier:
                values[col] = round(points[max(earlier)], digits[col])
                carried = True
            else:
                values[col] = None
        rows.append({"date": d.isoformat(), "values": values, "carried_forward": carried})

    rows.reverse()  # 시트와 같은 최신순
    return rows


def main() -> int:
    now = datetime.now(KST)
    target = prev_business_day(now.date())
    cal_dates = weekdays_back(target, BACKFILL_BUSINESS_DAYS)

    log(f"=== 수집 시작 {now.isoformat()} ===")
    log(f"목표일(직전 평일): {target.isoformat()}")
    log(f"게시 범위: {cal_dates[0]} ~ {cal_dates[-1]} (평일 {len(cal_dates)}개)")

    for name, fn in JOBS:
        log(f"[{name}]")
        try:
            series[name] = fn()
            log(f"  OK {({c: len(v) for c, v in series[name].items()})}")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            log(f"  FAIL {name}: {exc}")
            traceback.print_exc()

    tables: dict[str, dict] = {}
    for name, _ in JOBS:
        if name not in series:
            continue
        rows = build_rows(name, cal_dates)
        tables[name] = {"columns": TABLE_COLUMNS[name], "rows": rows}
        newest = rows[0]
        flag = " (carry-forward)" if newest["carried_forward"] else ""
        log(f"  {name}: {len(rows)}행, 최신 {newest['date']}{flag}")

    payload = {
        "generated_at": now.isoformat(),
        "target_date": target.isoformat(),
        "backfill_business_days": BACKFILL_BUSINESS_DAYS,
        "tables": tables,
        "errors": errors,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"=== 완료: 표 {len(tables)}/{len(JOBS)}, 오류 {len(errors)}건 ===")
    for e in errors:
        log(f"  - {e}")

    return 1 if not tables else 0


if __name__ == "__main__":
    sys.exit(main())
