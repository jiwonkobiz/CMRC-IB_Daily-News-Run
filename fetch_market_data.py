"""
CMRC IB Daily News Run - 전일 종가 수집기

한국시간 매 평일 09:00 에 실행되어, 직전 영업일까지의 종가를 data/latest.json 으로
게시한다. Office Script 가 이 파일을 읽어 Data 시트 표 7개에 행을 삽입한다.

목표일 결정 (미국장 마감 기준)
  미국 정규장 D일 종가는 한국시간 D+1 새벽 5시에 확정된다. 따라서 D일을 목표일로
  인정하는 조건은 "현재 시각 >= D+1 09:00 KST" 다. 조건을 못 채우면 한 평일 더
  거슬러 올라간다. 이렇게 하면 새벽에 수동 실행해도 미국 장중 시세가 섞이지 않고,
  한 행에는 항상 같은 날짜의 확정 종가만 들어간다. 목표일보다 최신인 데이터는
  수집 직후 전부 버린다.

행 구성
  - 단일 시장 표(미국증시/국내증시/미국채/국내채권)는 기준 컬럼 하나로 개장
    여부를 판정해 행 전체가 같은 날짜에서 온 값이 되게 한다. 노동절에 VIX 만
    유령 값이 들어오는 식의 혼합 행을 막는다.
  - 복수 시장 표(해외주요국증시/환율/원자재)는 컬럼별로 판정한다. 영국만 쉬고
    나머지는 여는 날을 제대로 처리하려면 이쪽이 맞다.
  - 휴장이면 직전 거래일 값을 이어 적고 carried_forward=true 로 표시한다.
    원본 파일이 5/25 메모리얼데이 행을 5/22 값으로 채워 둔 관행과 같다.
  - 행 날짜는 모든 표가 한국 평일(월~금) 기준이며, 매 실행마다 최근 평일
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
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import requests

KST = timezone(timedelta(hours=9))
OUT_PATH = Path(__file__).parent / "data" / "latest.json"

BACKFILL_BUSINESS_DAYS = 10   # 게시할 평일 개수 (연휴 대비 2주치)
FETCH_CALENDAR_DAYS = 40      # 소스에서 끌어올 달력일 범위
CUTOFF = time(9, 0)           # D일 종가를 인정하는 D+1 시각 (KST)

# 표별 컬럼 순서와 소수점 자릿수. 엑셀 표 헤더와 문자열이 정확히 일치해야 한다.
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
    "미국채": [("US 2Y", 3), ("US 5Y", 3), ("US 10Y", 3), ("US 30Y", 3)],
    "환율": [
        ("달러/원", 2), ("유로/원", 2), ("엔/원", 4),
        ("유로/달러", 4), ("달러/엔", 2),
    ],
    "원자재": [
        ("WTI", 2), ("Brent", 2), ("천연가스", 3), ("Gold", 2), ("구리", 4),
    ],
}

# 단일 시장 표 -> 개장 여부를 판정할 기준 컬럼.
# 여기 없는 표는 컬럼별로 따로 판정한다.
TABLE_ANCHOR = {
    "미국증시": "S&P 500",
    "국내증시": "KOSPI",
    "미국채": "US 10Y",
    "국내채권": "KR 3Y",
}

TABLE_COLUMNS = {t: [c for c, _ in spec] for t, spec in TABLE_SPEC.items()}
TABLE_DIGITS = {t: dict(spec) for t, spec in TABLE_SPEC.items()}

errors: list[str] = []
notes: dict[str, str] = {}   # 컬럼별 실제 사용 소스 기록

# series[표][컬럼] = {date: raw float}
series: dict[str, dict[str, dict[date, float]]] = {}


def log(msg: str) -> None:
    print(msg, flush=True)


def prev_business_day(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def weekdays_back(end: date, count: int) -> list[date]:
    out: list[date] = []
    d = end
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def resolve_target(now: datetime) -> date:
    """미국장이 확실히 닫힌 마지막 평일. D일 인정 조건은 now >= D+1 09:00 KST."""
    d = prev_business_day(now.date())
    for _ in range(10):
        cutoff = datetime.combine(d + timedelta(days=1), CUTOFF, tzinfo=KST)
        if now >= cutoff:
            return d
        d = prev_business_day(d)
    return d


# ---------------------------------------------------------------------------
# Yahoo Finance
# ---------------------------------------------------------------------------

def yahoo_series(table: str, mapping: dict[str, str]) -> dict[str, dict[date, float]]:
    import yfinance as yf

    tickers = list(mapping.values())
    end = datetime.now(KST).date() + timedelta(days=1)
    start = end - timedelta(days=FETCH_CALENDAR_DAYS)

    df = yf.download(
        tickers, start=start.isoformat(), end=end.isoformat(),
        progress=False, auto_adjust=False, group_by="column",
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
        "Dow Jones": "^DJI", "Nasdaq": "^IXIC", "S&P 500": "^GSPC",
        "Phili 반도체": "^SOX", "Russell 2000": "^RUT", "VIX": "^VIX",
    })


def fetch_global_equity():
    return yahoo_series("해외주요국증시", {
        "유로스톡스50": "^STOXX50E", "영국": "^FTSE", "상해종합": "000001.SS",
        "항셍": "^HSI", "홍콩 H": "^HSCE", "Nikkei 225": "^N225", "대만": "^TWII",
    })


def fetch_commodity():
    # 전부 선물 최근월물. 만기 롤오버 시점에 가격이 점프하는 것은 정상이다.
    return yahoo_series("원자재", {
        "WTI": "CL=F", "Brent": "BZ=F", "천연가스": "NG=F",
        "Gold": "GC=F", "구리": "HG=F",
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

    wanted = {
        "KOSPI": ["코스피"],
        "KOSPI 200": ["코스피 200"],
        "KOSDAQ": ["코스닥"],
        "KOSDAQ 150": ["코스닥 150"],
        "K밸류업": ["코리아 밸류업 지수", "코리아 밸류업", "밸류업"],
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
    for col, candidates in wanted.items():
        code = matched = None
        for cand in candidates:
            for name, c in name_to_code.items():
                if name.strip() == cand:
                    code, matched = c, name
                    break
            if code:
                break
        if code is None:
            for name, c in name_to_code.items():
                if any(k in name for k in candidates):
                    code, matched = c, name
                    break
        if code is None:
            errors.append(f"국내증시/{col}: 지수 코드를 찾지 못함")
            out[col] = {}
            continue

        log(f"  {col} <- '{matched}' ({code})")
        notes[f"국내증시/{col}"] = f"KRX {matched} ({code})"
        df = stock.get_index_ohlcv(start, end, code)
        if df is None or df.empty:
            errors.append(f"국내증시/{col}: OHLCV 비어 있음 (code={code})")
            out[col] = {}
            continue
        out[col] = {idx.date(): float(v) for idx, v in df["종가"].items()}

    return out


# ---------------------------------------------------------------------------
# ECOS 공통
# ---------------------------------------------------------------------------

def _ecos_key() -> str:
    key = os.environ.get("ECOS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ECOS_API_KEY 시크릿이 설정되지 않음")
    return key


def _ecos_items(api_key: str, stat_code: str) -> dict[str, str]:
    url = f"https://ecos.bok.or.kr/api/StatisticItemList/{api_key}/json/kr/1/500/{stat_code}"
    res = requests.get(url, timeout=30)
    res.raise_for_status()
    body = res.json()
    if "StatisticItemList" not in body:
        raise RuntimeError(
            f"ECOS 항목목록 응답 이상 ({stat_code}): "
            f"{json.dumps(body, ensure_ascii=False)[:300]}"
        )
    return {r["ITEM_NAME"].strip(): r["ITEM_CODE"] for r in body["StatisticItemList"]["row"]}


def _ecos_daily(api_key: str, stat: str, item: str, start: str, end: str) -> dict[date, float]:
    url = (
        f"https://ecos.bok.or.kr/api/StatisticSearch/{api_key}/json/kr/1/200/"
        f"{stat}/D/{start}/{end}/{item}"
    )
    res = requests.get(url, timeout=30)
    res.raise_for_status()
    body = res.json()
    if "StatisticSearch" not in body:
        return {}
    return {
        datetime.strptime(r["TIME"], "%Y%m%d").date(): float(r["DATA_VALUE"])
        for r in body["StatisticSearch"]["row"]
        if r.get("DATA_VALUE") not in (None, "")
    }


def _match(catalog: dict, candidates: list[str]):
    """완전일치 우선, 없으면 부분일치. (name, value) 반환."""
    for cand in candidates:
        for name, v in catalog.items():
            if name.replace(" ", "") == cand.replace(" ", ""):
                return name, v
    for cand in candidates:
        head = cand.split("(")[0].strip()
        for name, v in catalog.items():
            if head and head in name:
                return name, v
    return None, None


# ---------------------------------------------------------------------------
# 국내채권 (ECOS 시장금리 일별)
# ---------------------------------------------------------------------------

ECOS_RATE_STAT = "817Y002"


def fetch_kr_rates():
    api_key = _ecos_key()
    items = _ecos_items(api_key, ECOS_RATE_STAT)
    log(f"  ECOS {ECOS_RATE_STAT} 항목 {len(items)}개")
    for name in sorted(items):
        log(f"    · {name}")

    # CP91 은 CP(기업어음) 91일물이다. CD(양도성예금증서) 가 아니다.
    wanted = {
        "KR 2Y": ["국고채(2년)"],
        "KR 3Y": ["국고채(3년)"],
        "KR 10Y": ["국고채(10년)"],
        "SB 3Y(AA-)": ["회사채(3년, AA-)", "회사채(3년,AA-)"],
        "CP91": ["CP(91일)", "기업어음(91일)", "CP 91일"],
    }

    today = datetime.now(KST).date()
    start = (today - timedelta(days=FETCH_CALENDAR_DAYS)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    out: dict[str, dict[date, float]] = {}
    for col, candidates in wanted.items():
        matched, code = _match(items, candidates)
        if code is None:
            errors.append(f"국내채권/{col}: ECOS 항목을 찾지 못함 (후보: {candidates})")
            out[col] = {}
            continue
        if col == "CP91" and "CD" in matched:
            errors.append(f"국내채권/CP91: CP 항목이 없어 '{matched}' 가 잡힘 - 확인 필요")
        log(f"  {col} <- '{matched}' ({code})")
        notes[f"국내채권/{col}"] = f"ECOS {ECOS_RATE_STAT} {matched}"
        out[col] = _ecos_daily(api_key, ECOS_RATE_STAT, code, start, end)
        if not out[col]:
            errors.append(f"국내채권/{col}: 조회 결과 없음 ({matched})")

    return out


# ---------------------------------------------------------------------------
# 환율 (ECOS 매매기준율 우선, 실패 시 Yahoo 폴백)
# ---------------------------------------------------------------------------

ECOS_FX_STATS = ["731Y001", "731Y002"]

# 컬럼 -> (ECOS 항목 후보, 야후 폴백 티커, 타당 범위)
FX_SPEC = {
    "달러/원":   (["원/미국달러(매매기준율)", "원/미국달러"], "KRW=X",    (500, 3000)),
    "유로/원":   (["원/유로", "원/유로화"],                    "EURKRW=X", (500, 4000)),
    "엔/원":     (["원/일본엔(100엔)", "원/일본엔"],           "JPYKRW=X", (3, 30)),
    "유로/달러": (["미국달러/유로", "달러/유로"],              "EURUSD=X", (0.5, 2.0)),
    "달러/엔":   (["일본엔/미국달러", "엔/달러"],              "JPY=X",    (50, 300)),
}


def fetch_fx():
    today = datetime.now(KST).date()
    start = (today - timedelta(days=FETCH_CALENDAR_DAYS)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    catalog: dict[str, tuple[str, str]] = {}
    try:
        api_key = _ecos_key()
        for stat in ECOS_FX_STATS:
            try:
                for name, code in _ecos_items(api_key, stat).items():
                    catalog.setdefault(name, (stat, code))
            except Exception as exc:
                log(f"  ! ECOS {stat} 항목목록 실패: {exc}")
        log(f"  ECOS 환율 후보 항목 {len(catalog)}개")
        for name in sorted(catalog):
            log(f"    · {name}")
    except Exception as exc:
        log(f"  ! ECOS 환율 카탈로그 구성 실패: {exc}")
        api_key = None

    out: dict[str, dict[date, float]] = {}
    fallback_cols: dict[str, str] = {}

    for col, (candidates, ticker, (lo, hi)) in FX_SPEC.items():
        if not api_key or not catalog:
            fallback_cols[col] = ticker
            continue

        matched, ref = _match(catalog, candidates)
        if ref is None:
            log(f"  ~ {col}: ECOS 항목 없음 -> 야후 폴백")
            fallback_cols[col] = ticker
            continue

        stat, code = ref
        try:
            pts = _ecos_daily(api_key, stat, code, start, end)
        except Exception as exc:
            log(f"  ~ {col}: ECOS 조회 실패({exc}) -> 야후 폴백")
            fallback_cols[col] = ticker
            continue

        if not pts:
            log(f"  ~ {col}: ECOS 결과 비어 있음 -> 야후 폴백")
            fallback_cols[col] = ticker
            continue

        # 100엔 표기면 1엔 기준으로 환산
        scale = 0.01 if "100" in matched else 1.0
        pts = {d: v * scale for d, v in pts.items()}

        latest = pts[max(pts)]
        if not (lo <= latest <= hi):
            log(f"  ~ {col}: ECOS 값 {latest} 이 타당 범위 밖 -> 야후 폴백")
            errors.append(f"환율/{col}: ECOS 값 범위 이상({latest}) - 야후로 대체")
            fallback_cols[col] = ticker
            continue

        log(f"  {col} <- ECOS {stat} '{matched}' ({code}){' /100' if scale != 1 else ''}")
        notes[f"환율/{col}"] = f"ECOS {stat} {matched}"
        out[col] = pts

    if fallback_cols:
        log(f"  야후 폴백: {list(fallback_cols)}")
        try:
            yah = yahoo_series("환율", fallback_cols)
            for col, pts in yah.items():
                out[col] = pts
                notes[f"환율/{col}"] = f"Yahoo Finance {fallback_cols[col]}"
        except Exception as exc:
            for col in fallback_cols:
                out.setdefault(col, {})
                errors.append(f"환율/{col}: ECOS·야후 모두 실패 ({exc})")

    return out


# ---------------------------------------------------------------------------
# 미국채 (U.S. Treasury 일별 수익률곡선 CSV - 키 불필요)
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

    mapping = {"US 2Y": "2 Yr", "US 5Y": "5 Yr", "US 10Y": "10 Yr", "US 30Y": "30 Yr"}
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


def _effective(points: dict[date, float], d: date):
    """d 의 값, 없으면 직전 거래일 값. (value, carried) 반환."""
    if d in points:
        return points[d], False
    earlier = [pd for pd in points if pd < d]
    if earlier:
        return points[max(earlier)], True
    return None, False


def build_rows(table: str, cal_dates: list[date]) -> list[dict]:
    digits = TABLE_DIGITS[table]
    data = series.get(table, {})
    anchor = TABLE_ANCHOR.get(table)
    rows: list[dict] = []

    for d in cal_dates:
        values: dict[str, float | None] = {}
        carried = False

        if anchor and data.get(anchor):
            # 기준 컬럼으로 유효일자를 정하고 행 전체를 그 날짜에서 읽는다.
            anchor_pts = data[anchor]
            eff = d if d in anchor_pts else max(
                (pd for pd in anchor_pts if pd < d), default=None
            )
            carried = eff is not None and eff != d
            for col in TABLE_COLUMNS[table]:
                v = data.get(col, {}).get(eff) if eff else None
                values[col] = round(v, digits[col]) if v is not None else None
        else:
            for col in TABLE_COLUMNS[table]:
                v, c = _effective(data.get(col, {}), d)
                values[col] = round(v, digits[col]) if v is not None else None
                carried = carried or c

        rows.append({"date": d.isoformat(), "values": values, "carried_forward": carried})

    rows.reverse()  # 시트와 같은 최신순
    return rows


def main() -> int:
    now = datetime.now(KST)
    target = resolve_target(now)
    cal_dates = weekdays_back(target, BACKFILL_BUSINESS_DAYS)

    log(f"=== 수집 시작 {now.isoformat()} ===")
    log(f"목표일(미국장 마감 확정 기준): {target.isoformat()}")
    log(f"게시 범위: {cal_dates[0]} ~ {cal_dates[-1]} (평일 {len(cal_dates)}개)")

    for name, fn in JOBS:
        log(f"[{name}]")
        try:
            raw = fn()
            # 목표일보다 최신인 데이터는 장중 시세일 수 있으므로 버린다.
            series[name] = {
                col: {d: v for d, v in pts.items() if d <= target}
                for col, pts in raw.items()
            }
            dropped = sum(len(p) for p in raw.values()) - sum(
                len(p) for p in series[name].values()
            )
            log(f"  OK {({c: len(v) for c, v in series[name].items()})}"
                + (f"  (목표일 이후 {dropped}건 폐기)" if dropped else ""))
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
        "sources": notes,
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
