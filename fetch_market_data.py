"""
CMRC IB Daily News Run - 전일 종가 수집기

하루 여러 번(장 마감 직후) 실행되어 시세를 data/store.json 에 누적하고,
누적분에서 data/latest.json 을 조립해 게시한다. Office Script 가 latest.json 을
읽어 Data 시트 표 7개에 행을 삽입한다.

왜 누적 저장인가
  Yahoo 는 "가장 최근에 끝난 세션"의 확정 일봉(EOD)을 한동안 내려주지 않는다.
  실측: 09:38 KST 에 직전 영업일 바가 미국 외 7개 지수 전부 비어 있었고,
  같은 시각 S&P 는 멀쩡했으며, 하루 뒤에는 그 바가 채워져 있었다. 즉 미국 외
  시장은 항상 직전 세션 하나가 미확정으로 남는다. 반면 실시간 경로는 빨라서,
  장중/마감 직후에 조회하면 그 세션 값이 바로 잡힌다.

  그래서 "아침에 한 번 긁어서 전일 종가를 얻는다"는 전제 자체가 성립하지 않는다.
  각 시장이 닫힌 직후에 값을 잡아 store.json 에 적립해 두고, 아침 실행은 적립분을
  조립만 한다. store 는 병합 전용이며 기존 값을 지우지 않는다.

세션 확정 판정 (MARKETS)
  예전에는 "target 보다 최신인 데이터는 전부 폐기"했는데, 이러면 17:30 실행에서
  당일 아시아 종가를 잡아 두는 것 자체가 불가능하다. 지금은 시장별 마감 시각을
  두고, 그 시각을 지난 세션의 바만 받아들인다. 마감 전 값은 장중 시세이므로 버린다.

휴장 vs 미수신 판정
  거래소 캘린더 없이 구분한다. 컬럼 c 의 날짜 d 에 값이 없을 때,
    - store 에 d 보다 나중 날짜의 값이 있으면 -> 소스가 d 를 지나갔다는 뜻이므로
      진짜 휴장. 직전 거래일 값을 이어 적고 carried_columns 에 기록한다.
    - d 이후 값이 하나도 없으면 -> 아직 안 들어온 것. 이 경우 그 날짜 행 자체를
      게시하지 않는다.
  이월값이 시트에 한 번 박히면 Office Script 의 "시트 최신 날짜보다 새 행만 삽입"
  규칙 때문에 나중에 진짜 값이 와도 영영 못 들어간다. 그래서 미수신은 이월이 아니라
  보류가 맞다.

이름 매칭 정책 (2026-09 개정)
  예전에는 ECOS 항목명과 KRX 지수명을 "완전일치 실패 시 부분일치"로 찾았다.
  이 폴백이 사고를 냈다. CP(91일) 를 못 찾으면 이름에 'CP' 가 들어간 아무 항목이나
  집어서 조용히 성공한 것처럼 지나갔고, CD(91일) 와는 9bp 차이라 값만 봐서는
  틀린 걸 알 수 없었다. 국고채/회사채도 접두어가 같아 동일한 위험이 있었다.

  지금은 모든 이름 매칭이 다음 규칙을 따른다.
    - 완전일치(공백 무시)만 채택한다.
    - 일치 항목이 0개 또는 2개 이상이면 그 자리에서 실패시킨다.
    - 후보 목록 전체를 실행 로그에 찍어, 후보 이름만 고치면 바로 복구되게 한다.
  틀린 값보다 빈칸이 낫다. 빈칸은 보이지만 그럴듯한 틀린 값은 아무도 못 본다.

실패 범위 (컬럼 단위)
  실패한 컬럼만 죽이고 같은 표의 나머지 컬럼은 정상 게시한다. 죽은 컬럼은
  failed_columns 에 기록되어 값 자리에 null 로 나가고, Office Script 가 그 칸에
  N/A 를 찍는다. 빈칸은 눈에 안 띄지만 N/A 는 띈다.

  예외는 앵커 컬럼(TABLE_ANCHOR)이다. 앵커는 그 표의 행 날짜와 개장 여부를
  판정하는 기준이라, 죽으면 행을 만들 수 없어 표 전체를 보류한다.

  죽은 컬럼은 store 에 남은 과거 값을 이월하지 않는다. 이월하면 오늘 수집이
  실패했다는 사실이 옛날 숫자로 가려진다.

  "아직 안 들어온 값"(MISSING)은 죽은 것이 아니다. 기다리면 오므로 행 자체를
  보류한다. 여기에 N/A 를 박으면 나중에 진짜 값이 와도 Office Script 의
  "시트 최신 날짜보다 새 행만 삽입" 규칙 때문에 영영 못 들어간다.

행 구성
  - 단일 시장 표(미국증시/국내증시/미국채/국내채권)는 기준 컬럼 하나로 개장 여부를
    판정해 행 전체가 같은 날짜에서 온 값이 되게 한다. 노동절에 VIX 만 유령 값이
    들어오는 식의 혼합 행을 막는다.
  - 복수 시장 표(해외주요국증시/환율/원자재)는 컬럼별로 판정한다. 영국만 쉬고
    나머지는 여는 날을 처리하려면 이쪽이 맞다.
  - 행 날짜는 모든 표가 한국 평일(월~금) 기준이며, 매 실행마다 최근 평일
    BACKFILL_BUSINESS_DAYS 개분 중 게시 가능한 것만 내보낸다. 시트에 이미 있는
    날짜를 거르는 일은 Office Script 쪽에서 한다.

경제지표 5개 표(CPI/PPI/PCE/PMI/NFP)는 월간 발표라 수기 유지한다.

필요한 시크릿
  ECOS_API_KEY   한국은행 ECOS 오픈API 인증키
  KRX_ID/KRX_PW  KRX 데이터 마켓플레이스 계정. 2025-12-27 회원제 전환 이후
                 pykrx 가 이 환경변수를 직접 읽어 로그인한다.

선택 환경변수 (항목명이 바뀌었을 때의 비상 고정용)
  ECOS_ITEM_<KEY>   해당 컬럼의 ECOS ITEM_CODE 를 직접 고정한다.
                    예: ECOS_ITEM_CP91=010502001
                    고정해도 이름 검증은 그대로 수행한다.
  KRX_INDEX_<KEY>   해당 컬럼의 KRX 지수 코드를 직접 고정한다.
                    예: KRX_INDEX_KVALUEUP=1Q01

종료 코드
  0  정상
  1  표를 하나도 만들지 못함
  2  목표일 행을 게시하지 못한 표가 있음 (데이터는 유효하나 아직 불완전)
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import traceback
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import requests

KST = timezone(timedelta(hours=9))
DATA_DIR = Path(__file__).parent / "data"
OUT_PATH = DATA_DIR / "latest.json"
STORE_PATH = DATA_DIR / "store.json"

BACKFILL_BUSINESS_DAYS = 10   # 게시할 평일 개수 (연휴 대비 2주치)
FETCH_CALENDAR_DAYS = 40      # 소스에서 끌어올 달력일 범위
STORE_KEEP_DAYS = 400         # store 보관 기간 (파일 비대화 방지)
CUTOFF = time(8, 0)           # D일을 목표일로 인정하는 D+1 시각 (KST)

# 시장별 "그 세션이 확정되는" 시각 (KST). (날짜 오프셋, 시각).
# 실제 마감 + 여유 30분~1시간. 이 시각을 지나야 해당 날짜 바를 store 에 적립한다.
MARKETS: dict[str, tuple[int, time]] = {
    "EU":     (1, time(2, 0)),    # 유로존 17:30 CET/CEST -> 익일 새벽 KST
    "UK":     (1, time(2, 0)),    # 런던 16:30 GMT/BST
    "CN":     (0, time(16, 30)),  # 상해 15:00 CST = 16:00 KST
    "HK":     (0, time(17, 30)),  # 홍콩 16:00 HKT = 17:00 KST
    "JP":     (0, time(16, 30)),  # 도쿄 15:30 JST = 15:30 KST
    "TW":     (0, time(15, 0)),   # 타이베이 13:30 = 14:30 KST
    "KR":     (0, time(16, 0)),   # 한국 15:30
    "US":     (1, time(6, 0)),    # 뉴욕 16:00 ET = 익일 05:00/06:00 KST
    "USBOND": (1, time(7, 30)),   # 재무부 수익률곡선 게시
    "FX":     (1, time(6, 0)),    # 뉴욕 마감 기준
    "COMMO":  (1, time(6, 0)),    # NYMEX/COMEX 정산
}

# 표별 컬럼 순서, 소수점 자릿수, 소속 시장.
# 엑셀 표 헤더와 컬럼 문자열이 정확히 일치해야 한다.
TABLE_SPEC: dict[str, list[tuple[str, int, str]]] = {
    "미국증시": [
        ("Dow Jones", 2, "US"), ("Nasdaq", 2, "US"), ("S&P 500", 2, "US"),
        ("Phili 반도체", 2, "US"), ("Russell 2000", 2, "US"), ("VIX", 2, "US"),
    ],
    "해외주요국증시": [
        ("유로스톡스50", 2, "EU"), ("영국", 2, "UK"), ("상해종합", 2, "CN"),
        ("항셍", 2, "HK"), ("홍콩 H", 2, "HK"), ("Nikkei 225", 2, "JP"),
        ("대만", 2, "TW"),
    ],
    "국내증시": [
        ("KOSPI", 2, "KR"), ("KOSPI 200", 2, "KR"), ("KOSDAQ", 2, "KR"),
        ("KOSDAQ 150", 2, "KR"), ("K밸류업", 2, "KR"),
    ],
    "국내채권": [
        ("KR 2Y", 3, "KR"), ("KR 3Y", 3, "KR"), ("KR 10Y", 3, "KR"),
        ("SB 3Y(AA-)", 3, "KR"), ("CP91", 2, "KR"),
    ],
    "미국채": [
        ("US 2Y", 3, "USBOND"), ("US 5Y", 3, "USBOND"),
        ("US 10Y", 3, "USBOND"), ("US 30Y", 3, "USBOND"),
    ],
    "환율": [
        ("달러/원", 2, "FX"), ("유로/원", 2, "FX"), ("엔/원", 4, "FX"),
        ("유로/달러", 4, "FX"), ("달러/엔", 2, "FX"),
    ],
    "원자재": [
        ("WTI", 2, "COMMO"), ("Brent", 2, "COMMO"), ("천연가스", 3, "COMMO"),
        ("Gold", 2, "COMMO"), ("구리", 4, "COMMO"),
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

TABLE_COLUMNS = {t: [c for c, _, _ in spec] for t, spec in TABLE_SPEC.items()}
TABLE_DIGITS = {t: {c: d for c, d, _ in spec} for t, spec in TABLE_SPEC.items()}
TABLE_MARKET = {t: {c: m for c, _, m in spec} for t, spec in TABLE_SPEC.items()}

errors: list[str] = []
warnings: list[str] = []
notes: dict[str, str] = {}      # 컬럼별 실제 사용 소스 기록
admitted: dict[str, list[str]] = {}   # 이번 실행에 새로 적립된 (표/컬럼) -> 날짜들
held: dict[str, list[str]] = {}       # 장중이라 보류한 (표/컬럼) -> 날짜들

# 구조적으로 죽은 컬럼: {표: {컬럼: 사유}}.
# 항목 확정 실패처럼 다음 실행에서도 저절로 낫지 않는 것만 넣는다. 이 컬럼은
# 행을 막지 않고 값 자리에 null 로 나가며, Office Script 가 N/A 로 찍는다.
# "아직 안 들어온 값"(MISSING)은 여기 넣으면 안 된다 — 그건 기다리면 오므로
# 행 자체를 보류해야 하고, N/A 를 박아 두면 영구히 굳는다.
failed_columns: dict[str, dict[str, str]] = {}


def mark_failed(table: str, column: str, reason: str) -> None:
    failed_columns.setdefault(table, {})[column] = reason
    errors.append(f"{table}/{column}: {reason}")


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
    """뉴스런이 채워야 할 날짜. D일 인정 조건은 now >= D+1 CUTOFF KST."""
    d = prev_business_day(now.date())
    for _ in range(10):
        cutoff = datetime.combine(d + timedelta(days=1), CUTOFF, tzinfo=KST)
        if now >= cutoff:
            return d
        d = prev_business_day(d)
    return d


def session_final(market: str, d: date) -> datetime:
    """market 의 d 세션이 확정되는 시각(KST)."""
    off, t = MARKETS[market]
    return datetime.combine(d + timedelta(days=off), t, tzinfo=KST)


# ---------------------------------------------------------------------------
# store.json - 병합 전용 시세 적립소
# ---------------------------------------------------------------------------

def load_store() -> dict[str, dict[str, dict[date, float]]]:
    if not STORE_PATH.exists():
        return {}
    try:
        raw = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # store 가 깨졌다고 실행을 멈추면 안 된다. 경고만 남기고 새로 쌓는다.
        warnings.append(f"store.json 읽기 실패, 새로 시작함: {type(exc).__name__}: {exc}")
        return {}
    out: dict[str, dict[str, dict[date, float]]] = {}
    for table, cols in (raw.get("points") or {}).items():
        out[table] = {}
        for col, pts in cols.items():
            parsed: dict[date, float] = {}
            for k, v in pts.items():
                try:
                    parsed[date.fromisoformat(k)] = float(v)
                except (ValueError, TypeError):
                    continue
            out[table][col] = parsed
    return out


def save_store(store: dict[str, dict[str, dict[date, float]]], now: datetime) -> None:
    horizon = now.date() - timedelta(days=STORE_KEEP_DAYS)
    payload = {
        "updated_at": now.isoformat(),
        "points": {
            table: {
                col: {d.isoformat(): v for d, v in sorted(pts.items()) if d >= horizon}
                for col, pts in cols.items()
            }
            for table, cols in store.items()
        },
    }
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def merge_into_store(
    store: dict[str, dict[str, dict[date, float]]],
    table: str,
    fetched: dict[str, dict[date, float]],
    now: datetime,
) -> None:
    """확정된 세션의 바만 적립한다. 장중 값은 버리고, 기존 값은 갱신한다.

    같은 날짜를 다시 받으면 덮어쓴다. 마감 직후 잡은 값이 나중에 EOD 확정값으로
    교체되게 하려는 것이다(클로징 옥션 확정 전후로 소수점이 미세하게 다를 수 있다).
    """
    markets = TABLE_MARKET[table]
    bucket = store.setdefault(table, {})
    for col, pts in fetched.items():
        market = markets.get(col)
        if market is None:
            continue
        col_store = bucket.setdefault(col, {})
        for d, v in pts.items():
            key = f"{table}/{col}"
            if now < session_final(market, d):
                held.setdefault(key, []).append(d.isoformat())
                continue
            if col_store.get(d) != v:
                if d not in col_store:
                    admitted.setdefault(key, []).append(d.isoformat())
                col_store[d] = v


# ---------------------------------------------------------------------------
# 이름 매칭 공통 - 완전일치 + 유일성. 실패하면 예외.
# ---------------------------------------------------------------------------

class ItemResolutionError(RuntimeError):
    """소스 항목을 확정하지 못했다. 다른 항목으로 대체하지 않고 실패시킨다."""


def _flat(s: str) -> str:
    """비교용 정규화. 공백/전각공백 제거 후 대문자."""
    return re.sub(r"\s+", "", s).upper()


def resolve_exact(
    catalog: dict[str, str],
    column: str,
    candidates: list[str],
    *,
    pin: str | None = None,
    verify: tuple[list[str], list[str]] | None = None,
) -> tuple[str, str]:
    """catalog(이름 -> 코드)에서 column 에 해당하는 항목을 확정한다.

    완전일치(공백 무시)만 인정한다. 0개면 못 찾은 것, 2개 이상이면 후보가 모호한
    것이므로 둘 다 예외다. 부분일치 폴백은 의도적으로 없다 — 그게 CP91 에
    CD 가 들어간 원인이었다.

    pin      ITEM_CODE 를 직접 지정해 이름 매칭을 건너뛴다. 이름 검증은 유지된다.
    verify   (필수 키워드, 금지 키워드). 확정된 이름에 필수 키워드가 하나도 없거나
             금지 키워드가 하나라도 있으면 예외.
    """
    if pin:
        by_code = {c: n for n, c in catalog.items()}
        if pin not in by_code:
            raise ItemResolutionError(
                f"{column}: 고정 코드 '{pin}' 가 소스 목록에 없다"
            )
        name = by_code[pin]
        _verify_name(column, name, verify)
        return name, pin

    wanted = {_flat(c) for c in candidates}
    hits = [(n, c) for n, c in catalog.items() if _flat(n) in wanted]

    if len(hits) == 1:
        name, code = hits[0]
        _verify_name(column, name, verify)
        return name, code

    if not hits:
        raise ItemResolutionError(
            f"{column}: 완전일치 항목 없음. 후보={candidates} "
            f"— 위 목록에서 정확한 이름을 찾아 후보에 추가하거나 코드로 고정할 것"
        )
    raise ItemResolutionError(
        f"{column}: 완전일치가 {len(hits)}개로 모호함 "
        f"({', '.join(f'{n}({c})' for n, c in hits)}) — 후보를 하나로 좁힐 것"
    )


def _verify_name(column: str, name: str, verify: tuple[list[str], list[str]] | None) -> None:
    if not verify:
        return
    required, forbidden = verify
    flat = _flat(name)
    bad = [k for k in forbidden if _flat(k) in flat]
    if bad:
        raise ItemResolutionError(
            f"{column}: 확정된 항목 '{name}' 에 금지 키워드 {bad} 가 있다"
        )
    if required and not any(_flat(k) in flat for k in required):
        raise ItemResolutionError(
            f"{column}: 확정된 항목 '{name}' 에 필수 키워드 {required} 가 없다"
        )


def _env_pin(prefix: str, key: str) -> str | None:
    v = os.environ.get(f"{prefix}{key}", "").strip()
    return v or None


# ---------------------------------------------------------------------------
# Yahoo Finance
# ---------------------------------------------------------------------------

def yahoo_series(table: str, mapping: dict[str, str]) -> dict[str, dict[date, float]]:
    import yfinance as yf

    tickers = list(mapping.values())
    end = datetime.now(KST).date() + timedelta(days=2)
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
            mark_failed(table, col, f"티커 {ticker} 응답 없음")
            out[col] = {}
            continue
        s = s.dropna()
        out[col] = {idx.date(): float(v) for idx, v in s.items()}
        notes[f"{table}/{col}"] = f"Yahoo Finance {ticker}"
        if not out[col]:
            # 하루 이틀 지연이면 과거분은 남는다. 40일이 통째로 비면 티커가 죽은 것.
            mark_failed(table, col, f"최근 {FETCH_CALENDAR_DAYS}일 데이터 없음 ({ticker})")
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
# 환율 (Yahoo Finance)
# ---------------------------------------------------------------------------
#
# ECOS 매매기준율(서울외국환중개 원출처)을 한 번 붙여 봤으나 되돌렸다.
# 매매기준율 D일자 고시는 D-1 거래를 반영하므로, 그대로 D일 행에 넣으면
# 환율만 하루 밀린다. 야후 종가와 날짜별로 대조했을 때 같은 날 기준으로는
# 평균 8원 이상 벌어지고 ECOS 를 하루 당기면 2원 안으로 붙었다.
# 다른 6개 표가 모두 "그날 시장 종가"이므로, 한 행 안의 날짜 일관성을
# 우선해 야후 종가로 통일한다.

FX_TICKERS = {
    "달러/원": "KRW=X",
    "유로/원": "EURKRW=X",
    "엔/원": "JPYKRW=X",      # 1엔 기준 (원본 파일이 9.38 형태로 기록 중)
    "유로/달러": "EURUSD=X",
    "달러/엔": "JPY=X",
}


def fetch_fx():
    return yahoo_series("환율", FX_TICKERS)


# ---------------------------------------------------------------------------
# 국내증시 (pykrx - KRX 로그인 필요)
# ---------------------------------------------------------------------------

# 컬럼 -> (환경변수 키, 완전일치 후보, (필수 키워드, 금지 키워드))
KR_INDEX_SPEC: dict[str, tuple[str, list[str], tuple[list[str], list[str]]]] = {
    "KOSPI":      ("KOSPI",      ["코스피"],              (["코스피"], ["200", "150", "코스닥"])),
    "KOSPI 200":  ("KOSPI200",   ["코스피 200"],          (["코스피", "200"], ["코스닥"])),
    "KOSDAQ":     ("KOSDAQ",     ["코스닥"],              (["코스닥"], ["150", "코스피"])),
    "KOSDAQ 150": ("KOSDAQ150",  ["코스닥 150"],          (["코스닥", "150"], ["코스피"])),
    "K밸류업":     ("KVALUEUP",   ["코리아 밸류업 지수"],   (["밸류업"], [])),
}


def fetch_kr_equity():
    if not os.environ.get("KRX_ID") or not os.environ.get("KRX_PW"):
        raise RuntimeError(
            "KRX_ID / KRX_PW 시크릿이 설정되지 않음. "
            "KRX 정보데이터시스템이 2025-12-27 회원제로 바뀌어 로그인이 필수다."
        )

    from pykrx import stock

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

    # 컬럼 단위로 확정한다. 앵커(KOSPI)가 죽을 때만 표 전체를 보류한다.
    anchor = TABLE_ANCHOR["국내증시"]
    resolved: dict[str, tuple[str, str]] = {}
    for col, (env_key, candidates, verify) in KR_INDEX_SPEC.items():
        try:
            resolved[col] = resolve_exact(
                name_to_code, f"국내증시/{col}", candidates,
                pin=_env_pin("KRX_INDEX_", env_key), verify=verify,
            )
        except ItemResolutionError as exc:
            # 진단용으로 비슷한 이름만 추려 보여준다. 전체 목록은 너무 길다.
            hint = sorted(
                n for n in name_to_code
                if any(_flat(k) in _flat(n) for k in verify[0])
            )[:15]
            log(f"  ! {exc}")
            if hint:
                log(f"    비슷한 이름: {hint}")
            if col == anchor:
                raise ItemResolutionError(f"앵커 컬럼 확정 실패 — {exc}") from exc
            mark_failed("국내증시", col, f"지수 확정 실패: {exc}")

    start = (today - timedelta(days=FETCH_CALENDAR_DAYS)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    out: dict[str, dict[date, float]] = {}
    for col, (matched, code) in resolved.items():
        log(f"  {col} <- '{matched}' ({code})")
        notes[f"국내증시/{col}"] = f"KRX {matched} ({code})"
        df = stock.get_index_ohlcv(start, end, code)
        if df is None or df.empty:
            mark_failed("국내증시", col, f"OHLCV 비어 있음 (code={code})")
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


# ---------------------------------------------------------------------------
# 국내채권 (ECOS 시장금리 일별)
# ---------------------------------------------------------------------------

ECOS_RATE_STAT = "817Y002"

# 컬럼 -> (환경변수 키, 완전일치 후보, (필수 키워드, 금지 키워드))
#
# CP91 은 CP(기업어음) 91일물이다. CD(양도성예금증서)가 아니다. 둘은 9bp 차이라
# 값만 봐서는 구분이 안 되므로 금지 키워드로 막는다.
# 국고채 2Y/3Y/10Y 는 접두어가 같아 예전 부분일치에서 서로 섞일 수 있었다.
ECOS_RATE_SPEC: dict[str, tuple[str, list[str], tuple[list[str], list[str]]]] = {
    "KR 2Y": ("KR2Y", ["국고채(2년)"], (["국고채"], ["3년", "5년", "10년", "20년", "30년", "50년"])),
    "KR 3Y": ("KR3Y", ["국고채(3년)"], (["국고채"], ["2년", "5년", "10년", "20년", "30년", "50년"])),
    "KR 10Y": ("KR10Y", ["국고채(10년)"], (["국고채"], ["2년", "3년", "5년", "20년", "30년", "50년"])),
    "SB 3Y(AA-)": (
        "SB3Y",
        ["회사채(3년, AA-)", "회사채(3년,AA-)", "회사채(AA-,3년)"],
        (["회사채", "AA-"], ["BBB", "A+", "A0", "AAA"]),
    ),
    "CP91": (
        "CP91",
        ["CP(91일)", "CP(91일물)", "기업어음(91일)", "기업어음(CP)(91일)"],
        (["CP", "기업어음"], ["CD", "양도성"]),
    ),
}


def fetch_kr_rates():
    api_key = _ecos_key()
    items = _ecos_items(api_key, ECOS_RATE_STAT)

    # 항목 전체를 로그에 남긴다. 후보 이름이 틀렸을 때 인증키를 어디로도 옮기지
    # 않고 Actions 로그만 보고 고칠 수 있게 하려는 것이다.
    log(f"  ECOS {ECOS_RATE_STAT} 항목 {len(items)}개")
    for n, c in sorted(items.items(), key=lambda kv: kv[1]):
        log(f"    {c}  {n}")

    # 컬럼 단위로 확정한다. 실패한 컬럼만 죽이고 나머지는 그대로 수집한다.
    # 단 앵커 컬럼(KR 3Y)이 죽으면 행 날짜를 판정할 기준이 없으므로 표 전체를 보류한다.
    anchor = TABLE_ANCHOR["국내채권"]
    resolved: dict[str, tuple[str, str]] = {}
    for col, (env_key, candidates, verify) in ECOS_RATE_SPEC.items():
        try:
            resolved[col] = resolve_exact(
                items, f"국내채권/{col}", candidates,
                pin=_env_pin("ECOS_ITEM_", env_key), verify=verify,
            )
        except ItemResolutionError as exc:
            log(f"  ! {exc}")
            if col == anchor:
                raise ItemResolutionError(f"앵커 컬럼 확정 실패 — {exc}") from exc
            mark_failed("국내채권", col, f"항목 확정 실패: {exc}")

    today = datetime.now(KST).date()
    start = (today - timedelta(days=FETCH_CALENDAR_DAYS)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    out: dict[str, dict[date, float]] = {}
    for col, (matched, code) in resolved.items():
        log(f"  {col} <- '{matched}' ({code})")
        notes[f"국내채권/{col}"] = f"ECOS {ECOS_RATE_STAT} {matched}"
        out[col] = _ecos_daily(api_key, ECOS_RATE_STAT, code, start, end)
        if not out[col]:
            # 40일치가 통째로 비었으면 일시적 지연이 아니라 구조적 문제다.
            mark_failed("국내채권", col, f"조회 결과 없음 ({matched})")

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

    # 헤더가 바뀌면 조용히 빈 컬럼이 되므로 먼저 확인한다.
    header = set(rows[0].keys())
    missing = [src for src in mapping.values() if src not in header]
    if missing:
        raise RuntimeError(
            f"Treasury CSV 헤더에 {missing} 가 없다. 실제 헤더: {sorted(header)}"
        )

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
        notes[f"미국채/{col}"] = "U.S. Treasury daily yield curve CSV"
        if not out[col]:
            mark_failed("미국채", col, "Treasury CSV 에 값 없음")

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

EXACT, CARRIED, MISSING, EMPTY = "exact", "carried", "missing", "empty"


def resolve_cell(points: dict[date, float], d: date):
    """(값, 상태, 실제 일자). 휴장과 미수신을 store 내용만으로 구분한다."""
    if not points:
        return None, EMPTY, None          # 컬럼 자체가 비어 있음 (수집 실패)
    if d in points:
        return points[d], EXACT, d
    if any(p > d for p in points):
        earlier = [p for p in points if p < d]
        if earlier:
            e = max(earlier)
            return points[e], CARRIED, e   # 소스가 d 를 지나감 -> 진짜 휴장
        return None, EMPTY, None
    return None, MISSING, None             # d 이후가 없음 -> 아직 안 들어옴


def build_rows(table: str, store_table: dict[str, dict[date, float]],
               cal_dates: list[date]) -> tuple[list[dict], list[str]]:
    digits = TABLE_DIGITS[table]
    anchor = TABLE_ANCHOR.get(table)
    dead = set(failed_columns.get(table, {}))
    rows: list[dict] = []
    skipped: list[str] = []

    # 죽은 컬럼은 store 에 남은 과거 값을 절대 이월하지 않는다. 이월하면 오늘
    # 수집이 실패했다는 사실이 옛날 숫자로 가려진다. null 로 내보내 N/A 가 찍히게 한다.
    store_table = {
        col: ({} if col in dead else pts) for col, pts in store_table.items()
    }

    for d in cal_dates:
        values: dict[str, float | None] = {}
        carried_cols: list[str] = []

        if anchor:
            _, status, eff = resolve_cell(store_table.get(anchor, {}), d)
            if status == MISSING:
                skipped.append(d.isoformat())
                continue
            if status == EMPTY:
                # 앵커가 통째로 비었으면 이 표는 판정 기준이 없다.
                skipped.append(d.isoformat())
                continue
            for col in TABLE_COLUMNS[table]:
                v = store_table.get(col, {}).get(eff)
                values[col] = round(v, digits[col]) if v is not None else None
            if status == CARRIED:
                carried_cols = [c for c in TABLE_COLUMNS[table] if values[c] is not None]
        else:
            incomplete = False
            for col in TABLE_COLUMNS[table]:
                v, status, _ = resolve_cell(store_table.get(col, {}), d)
                if status == MISSING:
                    incomplete = True
                    break
                values[col] = round(v, digits[col]) if v is not None else None
                if status == CARRIED:
                    carried_cols.append(col)
            if incomplete:
                skipped.append(d.isoformat())
                continue

        rows.append({
            "date": d.isoformat(),
            "values": values,
            "carried_forward": bool(carried_cols),
            "carried_columns": carried_cols,
        })

    rows.reverse()  # 시트와 같은 최신순
    return rows, skipped


def main() -> int:
    now = datetime.now(KST)
    target = resolve_target(now)
    cal_dates = weekdays_back(target, BACKFILL_BUSINESS_DAYS)
    store = load_store()

    log(f"=== 수집 시작 {now.isoformat()} ===")
    log(f"목표일: {target.isoformat()}")
    log(f"게시 범위: {cal_dates[0]} ~ {cal_dates[-1]} (평일 {len(cal_dates)}개)")

    fetched_tables: set[str] = set()
    for name, fn in JOBS:
        log(f"[{name}]")
        try:
            raw = fn()
            merge_into_store(store, name, raw, now)
            fetched_tables.add(name)
            detail = ", ".join(
                f"{c}={len(v)}건/{max(v).isoformat() if v else '없음'}"
                for c, v in store.get(name, {}).items()
            )
            log(f"  OK store: {detail}")
        except ItemResolutionError as exc:
            # 항목 확정 실패는 수집 실패와 다르다. 다른 값으로 대체하지 않고
            # 이 표를 통째로 보류시킨다. store 에 아무것도 넣지 않으므로
            # 목표일 행이 MISSING 으로 남아 게시되지 않는다.
            errors.append(f"{name}: 항목 확정 실패 - {exc}")
            log(f"  FAIL {name}: {exc}")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            log(f"  FAIL {name}: {exc}")
            traceback.print_exc()

    save_store(store, now)

    tables: dict[str, dict] = {}
    skipped_all: dict[str, list[str]] = {}
    target_iso = target.isoformat()
    incomplete_tables: list[str] = []

    for name, _ in JOBS:
        store_table = store.get(name)
        if not store_table:
            continue
        rows, skipped = build_rows(name, store_table, cal_dates)
        if skipped:
            skipped_all[name] = skipped
        if not rows:
            warnings.append(f"{name}: 게시 가능한 행이 없음 (보류 {len(skipped)}일)")
            continue
        tables[name] = {"columns": TABLE_COLUMNS[name], "rows": rows}
        newest = rows[0]
        flag = ""
        if newest["carried_forward"]:
            flag = f" (휴장 이월: {', '.join(newest['carried_columns'])})"
        log(f"  {name}: {len(rows)}행, 최신 {newest['date']}{flag}")
        if target_iso in skipped:
            incomplete_tables.append(name)
            warnings.append(
                f"{name}: 목표일 {target_iso} 아직 미수신 — 행 보류. "
                f"다음 실행에서 채워지면 그때 게시된다."
            )

    payload = {
        "generated_at": now.isoformat(),
        "target_date": target_iso,
        "backfill_business_days": BACKFILL_BUSINESS_DAYS,
        "sources": notes,
        "tables": tables,
        "skipped_dates": skipped_all,
        "failed_columns": failed_columns,
        "admitted_this_run": admitted,
        "held_intraday": held,
        "warnings": warnings,
        "errors": errors,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if admitted:
        log("신규 적립:")
        for k, v in admitted.items():
            log(f"  + {k}: {', '.join(sorted(v))}")
    if held:
        log("장중이라 보류:")
        for k, v in held.items():
            log(f"  ~ {k}: {', '.join(sorted(v))}")

    if failed_columns:
        log("죽은 컬럼 (N/A 로 게시):")
        for t, cols in failed_columns.items():
            for c, reason in cols.items():
                log(f"  x {t}/{c}: {reason}")

    log(f"=== 완료: 표 {len(tables)}/{len(JOBS)}, "
        f"죽은 컬럼 {sum(len(v) for v in failed_columns.values())}개, "
        f"오류 {len(errors)}건, 경고 {len(warnings)}건 ===")
    for w in warnings:
        log(f"  ! {w}")
    for e in errors:
        log(f"  - {e}")

    if not tables:
        return 1
    if incomplete_tables or failed_columns:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
