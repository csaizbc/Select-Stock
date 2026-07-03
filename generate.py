from __future__ import annotations

import argparse
from email.utils import parsedate_to_datetime
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd
import tushare as ts


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "dist" / "tomorrow_stock_list.html"
DEFAULT_INDEX_OUTPUT = ROOT / "dist" / "index.html"
DEFAULT_DATA_OUTPUT_DIR = ROOT / "output"
DEFAULT_CACHE_DIR = ROOT / "cache"
LOCAL_TIMEZONE = "Asia/Shanghai"
LOCAL_TIMEZONE_LABEL = "北京时间"
TIME_SYNC_URLS = (
    "https://api.github.com",
    "https://www.baidu.com",
    "https://www.qq.com",
)
MAX_SUSPEND_WINDOW = 90
SUSPEND_WINDOWS = (20, 40, 60, 90)
CHIP_LOOKBACK_DAYS = 5
DEFAULT_AGE_DAYS = 730
DEFAULT_SUSPEND_WINDOW = 60
INDUSTRY_SRC = "SW2021"
ST_NAME_PATTERN = re.compile(r"(?:S\*ST|SST|\*ST|ST)", re.IGNORECASE)
SCRIPT_DATA_PATTERN = re.compile(r"</(script)", re.IGNORECASE)


@dataclass(frozen=True)
class TradingContext:
    as_of_date: str
    data_date: str
    target_trade_date: str
    recent_trade_dates: list[str]


def normalize_ymd(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return re.sub(r"\D", "", text)[:8]


def format_ymd(value: str) -> str:
    value = normalize_ymd(value)
    if len(value) != 8:
        return value
    return f"{value[:4]}-{value[4:6]}-{value[6:]}"


def today_ymd() -> str:
    return pd.Timestamp.now(tz=LOCAL_TIMEZONE).strftime("%Y%m%d")


def current_beijing_timestamp() -> str:
    for url in TIME_SYNC_URLS:
        request = Request(url, method="HEAD", headers={"User-Agent": "select-stock-time-sync/1.0"})
        try:
            with urlopen(request, timeout=6) as response:
                date_header = response.headers.get("Date", "")
        except HTTPError as exc:
            date_header = exc.headers.get("Date", "")
        except (OSError, URLError):
            continue
        if not date_header:
            continue
        try:
            server_time = parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            continue
        beijing_time = server_time.astimezone(ZoneInfo(LOCAL_TIMEZONE))
        return f"{beijing_time.strftime('%Y-%m-%d %H:%M:%S')} {LOCAL_TIMEZONE_LABEL}"

    fallback_time = pd.Timestamp.now(tz=LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    return f"{fallback_time} {LOCAL_TIMEZONE_LABEL}（本机时间）"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def load_tushare_token() -> str:
    for env_path in [ROOT / ".env", Path.cwd() / ".env"]:
        load_env_file(env_path)
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TUSHARE_TOKEN is missing. Put it in .env or environment variables.")
    return token


def get_tushare_pro():
    ts.set_token(load_tushare_token())
    return ts.pro_api(timeout=90)


def call_with_retry(func: Callable, *args, retries: int = 5, sleep_seconds: float = 0.6, **kwargs) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            result = func(*args, **kwargs)
            if result is None:
                return pd.DataFrame()
            return result
        except Exception as exc:  # pragma: no cover - network branch
            last_error = exc
            if attempt == retries:
                break
            time.sleep(sleep_seconds * attempt)
    raise RuntimeError(f"Tushare call failed after {retries} attempts: {last_error}") from last_error


def get_trade_calendar(pro, as_of_date: str) -> pd.DataFrame:
    as_of_dt = datetime.strptime(as_of_date, "%Y%m%d")
    start_date = (as_of_dt - timedelta(days=260)).strftime("%Y%m%d")
    end_date = (as_of_dt + timedelta(days=45)).strftime("%Y%m%d")
    calendar = call_with_retry(
        pro.trade_cal,
        exchange="SSE",
        start_date=start_date,
        end_date=end_date,
        fields="cal_date,is_open,pretrade_date",
    )
    if calendar.empty:
        raise RuntimeError("Tushare trade_cal returned no rows.")
    calendar["cal_date"] = calendar["cal_date"].map(normalize_ymd)
    calendar["pretrade_date"] = calendar["pretrade_date"].map(normalize_ymd)
    calendar["is_open"] = pd.to_numeric(calendar["is_open"], errors="coerce").fillna(0).astype(int)
    return calendar.sort_values("cal_date").reset_index(drop=True)


def fetch_daily_for_latest_available(pro, open_dates: list[str]) -> tuple[str, pd.DataFrame]:
    for trade_date in reversed(open_dates):
        daily = call_with_retry(
            pro.daily,
            trade_date=trade_date,
            fields="ts_code,trade_date,close,pct_chg",
        )
        if not daily.empty:
            daily["trade_date"] = daily["trade_date"].map(normalize_ymd)
            daily = daily.drop_duplicates("ts_code", keep="last")
            return trade_date, daily
    raise RuntimeError("No latest daily quote data found in recent open trading days.")


def resolve_trading_context(pro, as_of_date: str | None) -> tuple[TradingContext, pd.DataFrame]:
    as_of = normalize_ymd(as_of_date) if as_of_date else today_ymd()
    if len(as_of) != 8:
        raise ValueError("--as-of must be YYYYMMDD.")

    calendar = get_trade_calendar(pro, as_of)
    open_dates = calendar.loc[
        (calendar["is_open"].eq(1)) & (calendar["cal_date"] <= as_of),
        "cal_date",
    ].tolist()
    if not open_dates:
        raise RuntimeError(f"No open trading day found on or before {as_of}.")

    data_date, daily = fetch_daily_for_latest_available(pro, open_dates)
    future_dates = calendar.loc[
        (calendar["is_open"].eq(1)) & (calendar["cal_date"] > data_date),
        "cal_date",
    ].tolist()
    if not future_dates:
        raise RuntimeError(f"No next trading day found after {data_date}.")

    historical_open_dates = calendar.loc[
        (calendar["is_open"].eq(1)) & (calendar["cal_date"] <= data_date),
        "cal_date",
    ].tolist()
    recent_trade_dates = historical_open_dates[-MAX_SUSPEND_WINDOW:]
    if len(recent_trade_dates) < MAX_SUSPEND_WINDOW:
        raise RuntimeError("Trade calendar did not return enough recent trading days.")

    context = TradingContext(
        as_of_date=as_of,
        data_date=data_date,
        target_trade_date=future_dates[0],
        recent_trade_dates=recent_trade_dates,
    )
    return context, daily


def classify_exchange(ts_code: str, exchange_value: object = "") -> str:
    code = str(ts_code)
    declared = str(exchange_value or "").upper()
    if code.endswith(".SH") or declared in {"SSE", "SH"}:
        return "沪市"
    if code.endswith(".SZ") or declared in {"SZSE", "SZ"}:
        return "深市"
    if code.endswith(".BJ") or declared in {"BSE", "BJ"}:
        return "北交所"
    return "其他"


def classify_board(ts_code: str, market_value: object = "") -> str:
    code = str(ts_code).split(".")[0]
    suffix = str(ts_code).split(".")[-1] if "." in str(ts_code) else ""
    market = str(market_value or "").strip()
    if suffix == "BJ":
        return "北交所"
    if suffix == "SH" and code.startswith("688"):
        return "科创板"
    if suffix == "SH" and code.startswith("60"):
        return "主板"
    if suffix == "SZ" and code.startswith(("300", "301")):
        return "创业板"
    if suffix == "SZ" and code.startswith(("000", "001", "002", "003")):
        return "主板"
    if market:
        if "创业" in market:
            return "创业板"
        if "科创" in market:
            return "科创板"
        if "北交" in market:
            return "北交所"
        if "主板" in market or market in {"中小板"}:
            return "主板"
    return market or "其他"


def is_st_name(name: object) -> bool:
    if name is None or pd.isna(name):
        return False
    return bool(ST_NAME_PATTERN.search(str(name).upper().replace(" ", "")))


def fetch_stock_basic(pro) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    fields = "ts_code,symbol,name,area,industry,market,exchange,list_status,list_date"
    for status in ["L"]:
        frame = call_with_retry(pro.stock_basic, exchange="", list_status=status, fields=fields)
        if not frame.empty:
            frames.append(frame)
    if not frames:
        raise RuntimeError("Tushare stock_basic returned no listed stocks.")
    stock = pd.concat(frames, ignore_index=True).drop_duplicates("ts_code", keep="last")
    stock["list_date"] = stock["list_date"].map(normalize_ymd)
    stock["exchange_name"] = stock.apply(lambda row: classify_exchange(row["ts_code"], row.get("exchange", "")), axis=1)
    stock["board"] = stock.apply(lambda row: classify_board(row["ts_code"], row.get("market", "")), axis=1)
    stock["is_st_by_name"] = stock["name"].map(is_st_name)
    return stock.sort_values("ts_code").reset_index(drop=True)


def fetch_suspend_counts(pro, context: TradingContext) -> pd.DataFrame:
    start_date = context.recent_trade_dates[0]
    end_date = context.data_date
    suspend = call_with_retry(
        pro.suspend_d,
        start_date=start_date,
        end_date=end_date,
        fields="ts_code,trade_date,suspend_type",
    )
    if suspend.empty:
        return pd.DataFrame(columns=["ts_code", *[f"suspend_days_{window}" for window in SUSPEND_WINDOWS]])
    suspend["trade_date"] = suspend["trade_date"].map(normalize_ymd)
    suspend = suspend[suspend["trade_date"].isin(set(context.recent_trade_dates))].drop_duplicates(["ts_code", "trade_date"])

    output = pd.DataFrame({"ts_code": sorted(suspend["ts_code"].dropna().unique().tolist())})
    for window in SUSPEND_WINDOWS:
        dates = set(context.recent_trade_dates[-window:])
        counts = (
            suspend[suspend["trade_date"].isin(dates)]
            .groupby("ts_code")["trade_date"]
            .nunique()
            .rename(f"suspend_days_{window}")
            .reset_index()
        )
        output = output.merge(counts, on="ts_code", how="left")
    for window in SUSPEND_WINDOWS:
        col = f"suspend_days_{window}"
        output[col] = output[col].fillna(0).astype(int)
    return output


def fetch_recent_namechange_st(pro, context: TradingContext) -> set[str]:
    end_dt = datetime.strptime(context.data_date, "%Y%m%d")
    start_date = (end_dt - timedelta(days=370)).strftime("%Y%m%d")
    namechange = call_with_retry(
        pro.namechange,
        start_date=start_date,
        end_date=context.data_date,
        fields="ts_code,name,start_date,end_date,ann_date,change_reason",
    )
    if namechange.empty:
        return set()

    namechange["start_date"] = namechange["start_date"].map(normalize_ymd)
    namechange["end_date"] = namechange["end_date"].map(normalize_ymd)
    st_rows = namechange[namechange["name"].map(is_st_name)].copy()
    if st_rows.empty:
        return set()
    active = st_rows[
        (st_rows["start_date"] <= context.data_date)
        & ((st_rows["end_date"].eq("")) | (st_rows["end_date"] >= context.data_date))
    ]
    return set(active["ts_code"].dropna().astype(str))


def fetch_chip_perf(pro, context: TradingContext) -> tuple[str, pd.DataFrame]:
    chip = pd.DataFrame()
    chip_date = ""
    for trade_date in reversed(context.recent_trade_dates[-CHIP_LOOKBACK_DAYS:]):
        candidate = call_with_retry(
            pro.cyq_perf,
            trade_date=trade_date,
            fields="ts_code,trade_date,cost_15pct,cost_50pct,cost_85pct",
        )
        if not candidate.empty:
            chip = candidate
            chip_date = trade_date
            break
    if chip.empty:
        raise RuntimeError(f"No cyq_perf data found in the last {CHIP_LOOKBACK_DAYS} trading days.")
    chip["trade_date"] = chip["trade_date"].map(normalize_ymd)
    chip = chip[chip["trade_date"].eq(chip_date)].drop_duplicates("ts_code", keep="last")
    for col in ["cost_15pct", "cost_50pct", "cost_85pct"]:
        chip[col] = pd.to_numeric(chip[col], errors="coerce")
    valid = chip["cost_50pct"].gt(0) & chip["cost_15pct"].notna() & chip["cost_85pct"].notna()
    chip["chip_concentration_70"] = pd.NA
    chip.loc[valid, "chip_concentration_70"] = (
        (chip.loc[valid, "cost_85pct"] - chip.loc[valid, "cost_15pct"]) / chip.loc[valid, "cost_50pct"] * 100
    )
    return chip_date, chip[["ts_code", "chip_concentration_70"]].reset_index(drop=True)


def read_cached_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_cached_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_sw_l2_classify(pro, cache_dir: Path, refresh: bool = False) -> pd.DataFrame:
    cache_path = cache_dir / "sw_l2_classify.json"
    if not refresh:
        cached = read_cached_json(cache_path)
        if cached is not None:
            return pd.DataFrame(cached)

    classify = call_with_retry(
        pro.index_classify,
        level="L2",
        src=INDUSTRY_SRC,
        fields="index_code,industry_name,level,industry_code,is_pub,parent_code,src",
    )
    if classify.empty:
        raise RuntimeError("Tushare index_classify returned no SW L2 rows.")
    classify = classify[["index_code", "industry_name", "level", "industry_code", "is_pub", "parent_code", "src"]]
    classify = classify.drop_duplicates("index_code").sort_values("index_code").reset_index(drop=True)
    write_cached_json(cache_path, classify.to_dict(orient="records"))
    return classify


def fetch_sw_member_events(pro, classify: pd.DataFrame, cache_dir: Path, refresh: bool = False) -> pd.DataFrame:
    cache_path = cache_dir / "sw_member_events.json"
    if not refresh:
        cached = read_cached_json(cache_path)
        if cached is not None:
            return pd.DataFrame(cached)

    frames: list[pd.DataFrame] = []
    l2_codes = classify["index_code"].dropna().astype(str).sort_values().tolist()
    total = len(l2_codes)
    for idx, l2_code in enumerate(l2_codes, start=1):
        for is_new in ["Y", "N"]:
            member = call_with_retry(
                pro.index_member_all,
                l2_code=l2_code,
                is_new=is_new,
                fields="ts_code,name,l1_code,l1_name,l2_code,l2_name,l3_code,l3_name,in_date,out_date,is_new",
            )
            if not member.empty:
                frames.append(member)
        if idx % 10 == 0 or idx == total:
            print(f"industry membership sync checkpoint: {idx}/{total}")
        time.sleep(0.08)

    if frames:
        events = pd.concat(frames, ignore_index=True).drop_duplicates()
    else:
        events = pd.DataFrame(
            columns=["ts_code", "name", "l1_code", "l1_name", "l2_code", "l2_name", "l3_code", "l3_name", "in_date", "out_date", "is_new"]
        )
    for col in ["in_date", "out_date"]:
        events[col] = events[col].map(normalize_ymd)
    events = events.sort_values(["ts_code", "in_date", "l2_code"]).reset_index(drop=True)
    write_cached_json(cache_path, events.to_dict(orient="records"))
    return events


def build_sw_l2_membership(pro, context: TradingContext, cache_dir: Path, refresh: bool = False) -> pd.DataFrame:
    classify = fetch_sw_l2_classify(pro, cache_dir, refresh=refresh)
    events = fetch_sw_member_events(pro, classify, cache_dir, refresh=refresh)
    if events.empty:
        return pd.DataFrame(columns=["ts_code", "sw_l1_code", "sw_l1_name", "sw_l2_code", "sw_l2_name"])

    events = events.copy()
    events["in_date"] = events["in_date"].map(normalize_ymd)
    events["out_date"] = events["out_date"].map(normalize_ymd)
    active = events[
        (events["in_date"] <= context.data_date)
        & ((events["out_date"].eq("")) | (events["out_date"] >= context.data_date))
    ].copy()
    if active.empty:
        return pd.DataFrame(columns=["ts_code", "sw_l1_code", "sw_l1_name", "sw_l2_code", "sw_l2_name"])

    active = active.sort_values(["ts_code", "in_date", "is_new"], ascending=[True, False, False])
    active = active.drop_duplicates("ts_code", keep="first")
    active = active.rename(
        columns={
            "l1_code": "sw_l1_code",
            "l1_name": "sw_l1_name",
            "l2_code": "sw_l2_code",
            "l2_name": "sw_l2_name",
        }
    )
    return active[["ts_code", "sw_l1_code", "sw_l1_name", "sw_l2_code", "sw_l2_name"]].reset_index(drop=True)


def build_stock_rows(pro, context: TradingContext, daily: pd.DataFrame, cache_dir: Path, refresh_industry: bool = False) -> tuple[list[dict], str]:
    stock = fetch_stock_basic(pro)
    suspend_counts = fetch_suspend_counts(pro, context)
    recent_st_codes = fetch_recent_namechange_st(pro, context)
    industry = build_sw_l2_membership(pro, context, cache_dir, refresh=refresh_industry)
    chip_data_date, chip_perf = fetch_chip_perf(pro, context)

    daily = daily[["ts_code", "close", "pct_chg"]].copy()
    daily["close"] = pd.to_numeric(daily["close"], errors="coerce")
    daily["pct_chg"] = pd.to_numeric(daily["pct_chg"], errors="coerce")

    frame = stock.merge(daily, on="ts_code", how="left")
    frame = frame.merge(suspend_counts, on="ts_code", how="left")
    frame = frame.merge(industry, on="ts_code", how="left")
    frame = frame.merge(chip_perf, on="ts_code", how="left")
    for window in SUSPEND_WINDOWS:
        col = f"suspend_days_{window}"
        frame[col] = frame[col].fillna(0).astype(int)
    for col in ["sw_l1_code", "sw_l1_name", "sw_l2_code", "sw_l2_name"]:
        frame[col] = frame[col].fillna("")
    frame["sw_l2_display"] = frame["sw_l2_name"].where(frame["sw_l2_name"].ne(""), "未分类")

    data_dt = datetime.strptime(context.data_date, "%Y%m%d")
    list_dates = pd.to_datetime(frame["list_date"], format="%Y%m%d", errors="coerce")
    frame["list_age_days"] = (data_dt - list_dates).dt.days
    frame["list_age_years"] = frame["list_age_days"] / 365.25
    frame["has_latest_quote"] = frame["close"].notna()
    frame["is_st"] = frame["is_st_by_name"] | frame["ts_code"].isin(recent_st_codes)
    frame["is_normal_listed"] = frame["list_status"].eq("L")

    rows: list[dict] = []
    for row in frame.itertuples(index=False):
        rows.append(
            {
                "ts_code": row.ts_code,
                "symbol": row.symbol,
                "display_code": str(row.ts_code).split(".")[0],
                "name": row.name,
                "board": row.board,
                "exchange": row.exchange_name,
                "market_raw": row.market if not pd.isna(row.market) else "",
                "sw_l1_code": row.sw_l1_code,
                "sw_l1_name": row.sw_l1_name,
                "sw_l2_code": row.sw_l2_code,
                "sw_l2_name": row.sw_l2_name,
                "sw_l2_display": row.sw_l2_display,
                "list_status": row.list_status,
                "list_date": row.list_date,
                "list_age_days": None if pd.isna(row.list_age_days) else int(row.list_age_days),
                "list_age_years": None if pd.isna(row.list_age_years) else round(float(row.list_age_years), 2),
                "close": None if pd.isna(row.close) else round(float(row.close), 3),
                "pct_chg": None if pd.isna(row.pct_chg) else round(float(row.pct_chg), 3),
                "chip_concentration_70": None if pd.isna(row.chip_concentration_70) else round(float(row.chip_concentration_70), 3),
                "is_st": bool(row.is_st),
                "is_normal_listed": bool(row.is_normal_listed),
                "has_latest_quote": bool(row.has_latest_quote),
                "suspend_days_20": int(row.suspend_days_20),
                "suspend_days_40": int(row.suspend_days_40),
                "suspend_days_60": int(row.suspend_days_60),
                "suspend_days_90": int(row.suspend_days_90),
            }
        )
    return rows, chip_data_date


def default_reject_reasons(row: dict) -> list[str]:
    reasons: list[str] = []
    if not row["is_normal_listed"]:
        reasons.append("非正常上市")
    if row["list_age_days"] is None or row["list_age_days"] < DEFAULT_AGE_DAYS:
        reasons.append("上市时间不足")
    if row["is_st"]:
        reasons.append("ST")
    if row[f"suspend_days_{DEFAULT_SUSPEND_WINDOW}"] > 0:
        reasons.append(f"近{DEFAULT_SUSPEND_WINDOW}日停牌")
    if not row["has_latest_quote"]:
        reasons.append("无最新行情")
    return reasons


def rows_to_dataframe(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame

    frame["default_reject_reasons"] = frame.apply(lambda row: "、".join(default_reject_reasons(row.to_dict())), axis=1)
    frame["default_passed"] = frame["default_reject_reasons"].eq("")
    ordered_columns = [
        "ts_code",
        "symbol",
        "display_code",
        "name",
        "board",
        "exchange",
        "market_raw",
        "sw_l1_code",
        "sw_l1_name",
        "sw_l2_code",
        "sw_l2_name",
        "sw_l2_display",
        "list_status",
        "list_date",
        "list_age_days",
        "list_age_years",
        "close",
        "pct_chg",
        "chip_concentration_70",
        "is_st",
        "is_normal_listed",
        "has_latest_quote",
        "suspend_days_20",
        "suspend_days_40",
        "suspend_days_60",
        "suspend_days_90",
        "default_passed",
        "default_reject_reasons",
    ]
    return frame[ordered_columns].sort_values("ts_code").reset_index(drop=True)


def build_summary(payload: dict, frame: pd.DataFrame) -> dict:
    meta = payload["meta"]
    passed = frame[frame["default_passed"]].copy()
    excluded = frame[~frame["default_passed"]].copy()

    reason_counts: dict[str, int] = {}
    for reasons in excluded["default_reject_reasons"].dropna():
        for reason in str(reasons).split("、"):
            if reason:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1

    return {
        "as_of_date": meta["as_of_date"],
        "data_date": meta["data_date"],
        "chip_data_date": meta.get("chip_data_date", ""),
        "target_trade_date": meta["target_trade_date"],
        "generated_at": meta["generated_at"],
        "total_count": int(len(frame)),
        "default_passed_count": int(len(passed)),
        "default_excluded_count": int(len(excluded)),
        "board_counts": {str(k): int(v) for k, v in frame["board"].value_counts().sort_index().items()},
        "exchange_counts": {str(k): int(v) for k, v in frame["exchange"].value_counts().sort_index().items()},
        "default_passed_by_board": {str(k): int(v) for k, v in passed["board"].value_counts().sort_index().items()},
        "default_excluded_by_board": {str(k): int(v) for k, v in excluded["board"].value_counts().sort_index().items()},
        "sw_l2_counts": {str(k): int(v) for k, v in frame["sw_l2_display"].value_counts().sort_index().items()},
        "default_passed_by_sw_l2": {str(k): int(v) for k, v in passed["sw_l2_display"].value_counts().sort_index().items()},
        "unclassified_industry_count": int(frame["sw_l2_display"].eq("未分类").sum()),
        "reject_reason_counts": reason_counts,
        "default_age_days": DEFAULT_AGE_DAYS,
        "default_suspend_window": DEFAULT_SUSPEND_WINDOW,
    }


def write_data_outputs(payload: dict, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    frame = rows_to_dataframe(payload["rows"])
    summary = build_summary(payload, frame)

    passed = frame[frame["default_passed"]].copy()
    excluded = frame[~frame["default_passed"]].copy()
    top_gain = passed.dropna(subset=["pct_chg"]).sort_values(["pct_chg", "ts_code"], ascending=[False, True]).head(100)
    top_loss = passed.dropna(subset=["pct_chg"]).sort_values(["pct_chg", "ts_code"], ascending=[True, True]).head(100)

    frame.to_csv(output_dir / "all_stocks.csv", index=False, encoding="utf-8-sig")
    passed.to_csv(output_dir / "default_passed.csv", index=False, encoding="utf-8-sig")
    excluded.to_csv(output_dir / "default_excluded.csv", index=False, encoding="utf-8-sig")
    top_gain.to_csv(output_dir / "default_passed_top_gain.csv", index=False, encoding="utf-8-sig")
    top_loss.to_csv(output_dir / "default_passed_top_loss.csv", index=False, encoding="utf-8-sig")

    pools = {
        "all": frame,
        "main_board": frame[frame["board"].eq("主板")],
        "gem": frame[frame["board"].eq("创业板")],
        "star_market": frame[frame["board"].eq("科创板")],
        "beijing": frame[frame["board"].eq("北交所")],
        "shanghai": frame[frame["exchange"].eq("沪市")],
        "shenzhen": frame[frame["exchange"].eq("深市")],
    }
    pool_dir = output_dir / "pools"
    pool_dir.mkdir(parents=True, exist_ok=True)
    for pool_name, pool_frame in pools.items():
        pool_frame.to_csv(pool_dir / f"{pool_name}_all.csv", index=False, encoding="utf-8-sig")
        pool_frame[pool_frame["default_passed"]].to_csv(
            pool_dir / f"{pool_name}_passed.csv",
            index=False,
            encoding="utf-8-sig",
        )
        pool_frame[~pool_frame["default_passed"]].to_csv(
            pool_dir / f"{pool_name}_excluded.csv",
            index=False,
            encoding="utf-8-sig",
        )

    industry_dir = output_dir / "industries"
    industry_dir.mkdir(parents=True, exist_ok=True)
    for industry_name, industry_frame in passed.groupby("sw_l2_display"):
        safe_name = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", str(industry_name)).strip("_") or "未分类"
        industry_frame.to_csv(industry_dir / f"{safe_name}.csv", index=False, encoding="utf-8-sig")

    (output_dir / "payload.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    summary_rows = [{"metric": key, "value": json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value} for key, value in summary.items()]
    pd.DataFrame(summary_rows).to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    industry_summary = pd.DataFrame(
        [
            {
                "sw_l2_name": name,
                "total_count": summary["sw_l2_counts"].get(name, 0),
                "default_passed_count": summary["default_passed_by_sw_l2"].get(name, 0),
            }
            for name in sorted(summary["sw_l2_counts"].keys())
        ]
    ).sort_values(["default_passed_count", "sw_l2_name"], ascending=[False, True])
    industry_summary.to_csv(output_dir / "industry_summary.csv", index=False, encoding="utf-8-sig")

    industry_return_summary = (
        passed.dropna(subset=["pct_chg"])
        .groupby("sw_l2_display", dropna=False)
        .agg(default_passed_quote_count=("ts_code", "count"), avg_pct_chg=("pct_chg", "mean"))
        .reset_index()
        .rename(columns={"sw_l2_display": "sw_l2_name"})
    )
    if not industry_return_summary.empty:
        industry_return_summary["avg_pct_chg"] = industry_return_summary["avg_pct_chg"].round(3)
        industry_return_summary = industry_return_summary.sort_values(
            ["avg_pct_chg", "default_passed_quote_count", "sw_l2_name"],
            ascending=[False, False, True],
        )
    industry_return_summary.to_csv(output_dir / "industry_return_summary.csv", index=False, encoding="utf-8-sig")
    write_run_report(summary, output_dir / "RUN_REPORT.md")
    return summary


def write_run_report(summary: dict, path: Path) -> None:
    lines = [
        "# 明日股票列表本地运行报告",
        "",
        f"- 数据日期：{format_ymd(summary['data_date'])}",
        f"- 筹码日期：{format_ymd(summary['chip_data_date'])}",
        f"- 目标交易日：{format_ymd(summary['target_trade_date'])}",
        f"- 全量股票数：{summary['total_count']}",
        f"- 默认通过数：{summary['default_passed_count']}",
        f"- 默认剔除数：{summary['default_excluded_count']}",
        f"- 默认新股阈值：上市满 {summary['default_age_days']} 天",
        f"- 默认停牌窗口：近 {summary['default_suspend_window']} 个交易日",
        "",
        "## 板块数量",
        "",
        "| 板块 | 全量 | 默认通过 | 默认剔除 |",
        "| --- | ---: | ---: | ---: |",
    ]
    boards = sorted(summary["board_counts"].keys())
    for board in boards:
        lines.append(
            f"| {board} | {summary['board_counts'].get(board, 0)} | "
            f"{summary['default_passed_by_board'].get(board, 0)} | "
            f"{summary['default_excluded_by_board'].get(board, 0)} |"
        )

    lines.extend(["", "## 申万二级行业", "", f"- 未分类股票数：{summary['unclassified_industry_count']}", "", "| 行业 | 默认通过数 |", "| --- | ---: |"])
    for industry_name, count in sorted(summary["default_passed_by_sw_l2"].items(), key=lambda item: (-item[1], item[0]))[:40]:
        lines.append(f"| {industry_name} | {count} |")

    lines.extend(["", "## 剔除原因", "", "| 原因 | 股票数 |", "| --- | ---: |"])
    for reason, count in summary["reject_reason_counts"].items():
        lines.append(f"| {reason} | {count} |")

    lines.extend(
        [
            "",
            "## 主要产物",
            "",
            "- `all_stocks.csv`：全量股票清单",
            "- `default_passed.csv`：默认条件通过清单",
            "- `default_excluded.csv`：默认条件剔除清单",
            "- `default_passed_top_gain.csv`：默认通过股票涨幅前 100",
            "- `default_passed_top_loss.csv`：默认通过股票跌幅前 100",
            "- `industry_return_summary.csv`：按申万二级行业统计的默认通过股票平均涨跌幅",
            "- `industries/`：按申万二级行业拆分的默认通过清单",
            "- `pools/`：按股票池拆分的全量、通过、剔除清单",
            "- `payload.json`：HTML 内嵌的完整数据",
            "- `summary.json`：本次运行汇总",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def json_for_script(data: dict) -> str:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return SCRIPT_DATA_PATTERN.sub(r"<\/\1", text)


def build_html(payload: dict) -> str:
    data_json = json_for_script(payload)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>明日股票列表</title>
  <style>
    :root {{
      --bg: #f7f8fa;
      --panel: #ffffff;
      --text: #1f2937;
      --muted: #687385;
      --line: #dfe4ea;
      --accent: #0f766e;
      --accent-soft: #e6f4f1;
      --bad: #b42318;
      --good: #067647;
      --warn: #a15c07;
      --table-head: #eef2f6;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      letter-spacing: 0;
    }}
    header {{
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      padding: 18px 24px 14px;
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: 24px;
      font-weight: 700;
    }}
    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px 18px;
      color: var(--muted);
      font-size: 14px;
    }}
    .meta strong {{ color: var(--text); }}
    main {{ padding: 16px 24px 28px; }}
    .toolbar {{
      display: grid;
      grid-template-columns: repeat(7, minmax(130px, 1fr));
      gap: 12px;
      align-items: end;
      background: var(--panel);
      border: 1px solid var(--line);
      padding: 14px;
      border-radius: 8px;
    }}
    label {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 13px;
    }}
    select, input[type="search"] {{
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      padding: 7px 9px;
      font-size: 14px;
    }}
    .check {{
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 38px;
      color: var(--text);
      font-size: 14px;
    }}
    input[type="checkbox"] {{
      width: 17px;
      height: 17px;
      accent-color: var(--accent);
    }}
    .actions {{
      display: flex;
      gap: 8px;
      justify-content: flex-end;
    }}
    button {{
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      padding: 7px 11px;
      font-size: 14px;
      cursor: pointer;
      white-space: nowrap;
    }}
    button.primary {{
      background: var(--accent);
      border-color: var(--accent);
      color: white;
    }}
    .summary {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px 18px;
      margin: 14px 2px;
      color: var(--muted);
      font-size: 14px;
    }}
    .summary strong {{ color: var(--text); }}
    .industry-chart {{
      margin: 0 0 14px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }}
    .chart-head {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }}
    .chart-head h2 {{
      margin: 0;
      font-size: 16px;
      line-height: 1.35;
    }}
    .chart-note {{
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }}
    .chart-scroll {{
      overflow-x: auto;
      overflow-y: hidden;
      padding-bottom: 2px;
    }}
    .chart-bars {{
      display: flex;
      align-items: flex-end;
      gap: 8px;
      min-height: 282px;
      min-width: 100%;
      padding: 8px 0 0;
    }}
    .chart-item {{
      display: grid;
      grid-template-rows: 34px 170px 22px 56px;
      justify-items: center;
      min-width: 46px;
      max-width: 56px;
      flex: 1 0 46px;
    }}
    .chart-value {{
      align-self: end;
      font-size: 12px;
      font-weight: 650;
      font-variant-numeric: tabular-nums;
      color: var(--muted);
    }}
    .chart-plot {{
      position: relative;
      width: 100%;
      height: 170px;
      display: flex;
      align-items: flex-end;
      justify-content: center;
      border-bottom: 1px solid var(--line);
    }}
    .chart-zero {{
      position: absolute;
      left: 0;
      right: 0;
      height: 1px;
      background: #cbd5e1;
    }}
    .chart-bar {{
      position: absolute;
      left: 50%;
      width: 24px;
      transform: translateX(-50%);
      border-radius: 5px 5px 0 0;
      background: #c2410c;
    }}
    .chart-bar.negative {{
      border-radius: 0 0 5px 5px;
      background: #047857;
    }}
    .chart-count {{
      align-self: center;
      color: #667085;
      font-size: 11px;
      font-weight: 650;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}
    .chart-label {{
      writing-mode: vertical-rl;
      text-orientation: mixed;
      max-height: 54px;
      overflow: hidden;
      color: #475467;
      font-size: 12px;
      line-height: 1.1;
      padding-top: 6px;
    }}
    .table-wrap {{
      overflow: auto;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      max-height: calc(100vh - 260px);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 1200px;
      font-size: 13px;
    }}
    thead th {{
      position: sticky;
      top: 0;
      z-index: 1;
      background: var(--table-head);
      color: #344054;
      text-align: left;
      padding: 10px 9px;
      border-bottom: 1px solid var(--line);
      white-space: nowrap;
      font-weight: 650;
    }}
    tbody td {{
      padding: 9px;
      border-bottom: 1px solid #eef1f4;
      white-space: nowrap;
    }}
    tbody tr:hover {{ background: #f9fbfc; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .tag {{
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      padding: 2px 7px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 12px;
      font-weight: 650;
    }}
    .tag.bad {{ background: #fee4e2; color: var(--bad); }}
    .tag.warn {{ background: #fff3d6; color: var(--warn); }}
    .up {{ color: var(--bad); font-weight: 650; }}
    .down {{ color: var(--good); font-weight: 650; }}
    .muted {{ color: var(--muted); }}
    .empty {{
      padding: 36px 12px;
      text-align: center;
      color: var(--muted);
      font-size: 15px;
    }}
    @media (max-width: 1100px) {{
      header, main {{ padding-left: 14px; padding-right: 14px; }}
      .toolbar {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .actions {{ justify-content: flex-start; }}
      h1 {{ font-size: 21px; }}
    }}
    @media (max-width: 640px) {{
      .toolbar {{ grid-template-columns: 1fr; }}
      .table-wrap {{ max-height: none; }}
      .chart-head {{ display: block; }}
      .chart-note {{ display: block; margin-top: 4px; white-space: normal; }}
      .chart-item {{ min-width: 42px; }}
      .actions {{ flex-wrap: wrap; }}
      button {{ flex: 1; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>明日股票列表</h1>
    <div class="meta">
      <span>数据日期：<strong id="dataDate"></strong></span>
      <span>筹码日期：<strong id="chipDate"></strong></span>
      <span>用于：<strong id="targetDate"></strong> 盘前参考</span>
      <span>生成时间（北京时间）：<strong id="generatedAt"></strong></span>
    </div>
  </header>
  <main>
    <section class="toolbar" aria-label="筛选条件">
      <label>股票池
        <select id="poolFilter">
          <option value="all">全部 A 股</option>
          <option value="main">主板</option>
          <option value="gem">创业板</option>
          <option value="star">科创板</option>
          <option value="bj">北交所</option>
          <option value="sh">沪市</option>
          <option value="sz">深市</option>
        </select>
      </label>
      <label>申万二级行业
        <select id="industryFilter">
          <option value="all">全部行业</option>
        </select>
      </label>
      <label>新股过滤
        <select id="ageFilter">
          <option value="0">不过滤</option>
          <option value="183">上市满 6 个月</option>
          <option value="365">上市满 1 年</option>
          <option value="730" selected>上市满 2 年</option>
          <option value="1095">上市满 3 年</option>
        </select>
      </label>
      <label>停牌窗口
        <select id="suspendWindow">
          <option value="20">20 个交易日</option>
          <option value="40">40 个交易日</option>
          <option value="60" selected>60 个交易日</option>
          <option value="90">90 个交易日</option>
        </select>
      </label>
      <label>排序方式
        <select id="pctSort">
          <option value="code">默认排序</option>
          <option value="desc">涨幅从高到低</option>
          <option value="asc">跌幅从高到低</option>
          <option value="price_desc">股价从高到低</option>
          <option value="price_asc">股价从低到高</option>
          <option value="chip_asc">筹码从集中到发散</option>
          <option value="chip_desc">筹码从发散到集中</option>
        </select>
      </label>
      <label>显示范围
        <select id="statusFilter">
          <option value="passed" selected>只看通过</option>
          <option value="excluded">只看剔除</option>
          <option value="all">全部股票</option>
        </select>
      </label>
      <label>搜索
        <input id="searchBox" type="search" placeholder="代码或名称">
      </label>
      <label class="check"><input id="stFilter" type="checkbox" checked>剔除 ST</label>
      <label class="check"><input id="suspendFilter" type="checkbox" checked>剔除停牌</label>
      <label class="check"><input id="quoteFilter" type="checkbox" checked>剔除无行情</label>
      <div class="actions">
        <button id="resetBtn" type="button">恢复默认</button>
        <button id="exportBtn" type="button" class="primary">导出 CSV</button>
      </div>
    </section>
    <section class="summary" aria-live="polite">
      <span>当前通过：<strong id="passedCount">0</strong></span>
      <span>当前剔除：<strong id="excludedCount">0</strong></span>
      <span>显示：<strong id="shownCount">0</strong></span>
      <span>总数：<strong id="totalCount">0</strong></span>
    </section>
    <section class="industry-chart" aria-label="行业涨跌">
      <div class="chart-head">
        <h2>行业涨跌</h2>
        <span class="chart-note">按当前显示列表的最新交易日平均涨跌幅从高到低排列，柱下为当前只数</span>
      </div>
      <div class="chart-scroll">
        <div id="industryChart" class="chart-bars"></div>
      </div>
    </section>
    <section class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>股票代码</th>
            <th>股票名称</th>
            <th>市场板块</th>
            <th>交易所</th>
            <th>申万二级行业</th>
            <th>上市日期</th>
            <th class="num">上市年限</th>
            <th class="num">最新收盘价</th>
            <th class="num">当日涨跌幅</th>
            <th class="num">筹码集中度</th>
            <th>是否 ST</th>
            <th class="num">停牌天数</th>
            <th>最新行情</th>
            <th>状态</th>
            <th>剔除原因</th>
          </tr>
        </thead>
        <tbody id="tableBody"></tbody>
      </table>
      <div id="emptyState" class="empty" hidden>没有符合当前条件的股票</div>
    </section>
  </main>
  <script id="payload" type="application/json">{data_json}</script>
  <script>
    const payload = JSON.parse(document.getElementById('payload').textContent);
    const rows = payload.rows || [];
    const state = {{
      pool: 'all',
      industry: 'all',
      ageDays: 730,
      suspendWindow: 60,
      pctSort: 'code',
      status: 'passed',
      search: '',
      filterSt: true,
      filterSuspend: true,
      filterQuote: true,
    }};
    let shownRows = [];

    const els = {{
      dataDate: document.getElementById('dataDate'),
      chipDate: document.getElementById('chipDate'),
      targetDate: document.getElementById('targetDate'),
      generatedAt: document.getElementById('generatedAt'),
      poolFilter: document.getElementById('poolFilter'),
      industryFilter: document.getElementById('industryFilter'),
      ageFilter: document.getElementById('ageFilter'),
      suspendWindow: document.getElementById('suspendWindow'),
      pctSort: document.getElementById('pctSort'),
      statusFilter: document.getElementById('statusFilter'),
      searchBox: document.getElementById('searchBox'),
      stFilter: document.getElementById('stFilter'),
      suspendFilter: document.getElementById('suspendFilter'),
      quoteFilter: document.getElementById('quoteFilter'),
      resetBtn: document.getElementById('resetBtn'),
      exportBtn: document.getElementById('exportBtn'),
      passedCount: document.getElementById('passedCount'),
      excludedCount: document.getElementById('excludedCount'),
      shownCount: document.getElementById('shownCount'),
      totalCount: document.getElementById('totalCount'),
      industryChart: document.getElementById('industryChart'),
      tableBody: document.getElementById('tableBody'),
      emptyState: document.getElementById('emptyState'),
    }};

    function formatDate(value) {{
      if (!value || value.length !== 8) return value || '';
      return `${{value.slice(0, 4)}}-${{value.slice(4, 6)}}-${{value.slice(6, 8)}}`;
    }}

    function formatNumber(value, digits = 2) {{
      if (value === null || value === undefined || Number.isNaN(Number(value))) return '';
      return Number(value).toFixed(digits);
    }}

    function formatPct(value) {{
      if (value === null || value === undefined || Number.isNaN(Number(value))) return '';
      return `${{Number(value).toFixed(2)}}%`;
    }}

    function pctClass(value) {{
      if (value === null || value === undefined || Number.isNaN(Number(value))) return '';
      if (Number(value) > 0) return 'up';
      if (Number(value) < 0) return 'down';
      return '';
    }}

    function poolMatched(row) {{
      if (state.pool === 'all') return true;
      if (state.pool === 'main') return row.board === '主板';
      if (state.pool === 'gem') return row.board === '创业板';
      if (state.pool === 'star') return row.board === '科创板';
      if (state.pool === 'bj') return row.board === '北交所';
      if (state.pool === 'sh') return row.exchange === '沪市';
      if (state.pool === 'sz') return row.exchange === '深市';
      return true;
    }}

    function industryMatched(row) {{
      if (state.industry === 'all') return true;
      return String(row.sw_l2_display || '未分类') === state.industry;
    }}

    function searchMatched(row) {{
      const q = state.search.trim().toLowerCase();
      if (!q) return true;
      return String(row.ts_code || '').toLowerCase().includes(q)
        || String(row.symbol || '').toLowerCase().includes(q)
        || String(row.name || '').toLowerCase().includes(q);
    }}

    function evaluateRow(row) {{
      const reasons = [];
      if (!row.is_normal_listed) reasons.push('非正常上市');
      if (state.ageDays > 0 && (row.list_age_days === null || row.list_age_days < state.ageDays)) {{
        reasons.push('上市时间不足');
      }}
      if (state.filterSt && row.is_st) reasons.push('ST');
      const suspendDays = Number(row[`suspend_days_${{state.suspendWindow}}`] || 0);
      if (state.filterSuspend && suspendDays > 0) reasons.push(`近${{state.suspendWindow}}日停牌`);
      if (state.filterQuote && !row.has_latest_quote) reasons.push('无最新行情');
      return {{ passed: reasons.length === 0, reasons, suspendDays }};
    }}

    function sortRows(list) {{
      const withNullLast = (getter, direction) => (a, b) => {{
        const av = getter(a);
        const bv = getter(b);
        const an = av === null || av === undefined || Number.isNaN(Number(av));
        const bn = bv === null || bv === undefined || Number.isNaN(Number(bv));
        if (an && bn) return String(a.ts_code).localeCompare(String(b.ts_code));
        if (an) return 1;
        if (bn) return -1;
        return direction * (Number(av) - Number(bv)) || String(a.ts_code).localeCompare(String(b.ts_code));
      }};
      if (state.pctSort === 'desc') return list.sort(withNullLast((row) => row.pct_chg, -1));
      if (state.pctSort === 'asc') return list.sort(withNullLast((row) => row.pct_chg, 1));
      if (state.pctSort === 'price_desc') return list.sort(withNullLast((row) => row.close, -1));
      if (state.pctSort === 'price_asc') return list.sort(withNullLast((row) => row.close, 1));
      if (state.pctSort === 'chip_desc') return list.sort(withNullLast((row) => row.chip_concentration_70, -1));
      if (state.pctSort === 'chip_asc') return list.sort(withNullLast((row) => row.chip_concentration_70, 1));
      return list.sort((a, b) => String(a.ts_code).localeCompare(String(b.ts_code)));
    }}

    function tag(text, cls = '') {{
      return `<span class="tag ${{cls}}">${{text}}</span>`;
    }}

    function escapeHtml(value) {{
      return String(value ?? '').replace(/[&<>"']/g, (char) => ({{
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
      }}[char]));
    }}

    function buildIndustryReturns(list) {{
      const groups = new Map();
      for (const row of list) {{
        const name = row.sw_l2_display || '未分类';
        const current = groups.get(name) || {{ name, sum: 0, count: 0, quoteCount: 0 }};
        current.count += 1;
        const hasPct = row.pct_chg !== null && row.pct_chg !== undefined && !Number.isNaN(Number(row.pct_chg));
        if (hasPct) {{
          current.sum += Number(row.pct_chg);
          current.quoteCount += 1;
        }}
        groups.set(name, current);
      }}
      return [...groups.values()]
        .filter((item) => item.quoteCount > 0)
        .map((item) => ({{ ...item, avg: item.sum / item.quoteCount }}))
        .sort((a, b) => b.avg - a.avg || b.count - a.count || a.name.localeCompare(b.name, 'zh-Hans-CN'));
    }}

    function renderIndustryChart(list) {{
      const data = buildIndustryReturns(list).slice(0, 40);
      if (!data.length) {{
        els.industryChart.innerHTML = '<div class="empty">没有可展示的行业涨跌数据</div>';
        return;
      }}
      const maxAbs = Math.max(...data.map((item) => Math.abs(item.avg)), 0.01);
      const plotHeight = 170;
      const zeroTop = plotHeight * (maxAbs / (maxAbs * 2));
      els.industryChart.innerHTML = data.map((item) => {{
        const magnitude = Math.max(2, Math.abs(item.avg) / (maxAbs * 2) * plotHeight);
        const top = item.avg >= 0 ? zeroTop - magnitude : zeroTop;
        return `
          <div class="chart-item" title="${{escapeHtml(item.name)}}：${{formatPct(item.avg)}}，当前 ${{item.count}} 只，行情样本 ${{item.quoteCount}} 只">
            <div class="chart-value ${{pctClass(item.avg)}}">${{formatPct(item.avg)}}</div>
            <div class="chart-plot">
              <span class="chart-zero" style="top: ${{zeroTop}}px"></span>
              <span class="chart-bar ${{item.avg < 0 ? 'negative' : ''}}" style="top: ${{top}}px; height: ${{magnitude}}px"></span>
            </div>
            <div class="chart-count">${{item.count}}只</div>
            <div class="chart-label">${{escapeHtml(item.name)}}</div>
          </div>
        `;
      }}).join('');
    }}

    function render() {{
      const selected = rows.filter((row) => poolMatched(row) && industryMatched(row) && searchMatched(row));
      let passedCount = 0;
      let excludedCount = 0;
      const evaluated = selected.map((row) => {{
        const result = evaluateRow(row);
        if (result.passed) passedCount += 1;
        else excludedCount += 1;
        return {{ ...row, ...result }};
      }});
      shownRows = sortRows(evaluated.filter((row) => {{
        if (state.status === 'passed') return row.passed;
        if (state.status === 'excluded') return !row.passed;
        return true;
      }}));

      els.passedCount.textContent = String(passedCount);
      els.excludedCount.textContent = String(excludedCount);
      els.shownCount.textContent = String(shownRows.length);
      els.totalCount.textContent = String(rows.length);
      els.emptyState.hidden = shownRows.length > 0;
      renderIndustryChart(shownRows);

      els.tableBody.innerHTML = shownRows.map((row) => `
        <tr>
          <td>${{escapeHtml(row.ts_code)}}</td>
          <td>${{escapeHtml(row.name)}}</td>
          <td>${{escapeHtml(row.board)}}</td>
          <td>${{escapeHtml(row.exchange)}}</td>
          <td>${{escapeHtml(row.sw_l2_display || '未分类')}}</td>
          <td>${{formatDate(row.list_date)}}</td>
          <td class="num">${{formatNumber(row.list_age_years, 2)}}</td>
          <td class="num">${{formatNumber(row.close, 2)}}</td>
          <td class="num ${{pctClass(row.pct_chg)}}">${{formatPct(row.pct_chg)}}</td>
          <td class="num">${{formatPct(row.chip_concentration_70)}}</td>
          <td>${{row.is_st ? tag('是', 'bad') : tag('否')}}</td>
          <td class="num">${{row.suspendDays}}</td>
          <td>${{row.has_latest_quote ? tag('有') : tag('无', 'warn')}}</td>
          <td>${{row.passed ? tag('通过') : tag('剔除', 'bad')}}</td>
          <td class="muted">${{escapeHtml(row.reasons.join('、'))}}</td>
        </tr>
      `).join('');
    }}

    function syncStateFromControls() {{
      state.pool = els.poolFilter.value;
      state.industry = els.industryFilter.value;
      state.ageDays = Number(els.ageFilter.value);
      state.suspendWindow = Number(els.suspendWindow.value);
      state.pctSort = els.pctSort.value;
      state.status = els.statusFilter.value;
      state.search = els.searchBox.value;
      state.filterSt = els.stFilter.checked;
      state.filterSuspend = els.suspendFilter.checked;
      state.filterQuote = els.quoteFilter.checked;
      render();
    }}

    function resetDefaults() {{
      els.poolFilter.value = 'all';
      els.industryFilter.value = 'all';
      els.ageFilter.value = '730';
      els.suspendWindow.value = '60';
      els.pctSort.value = 'code';
      els.statusFilter.value = 'passed';
      els.searchBox.value = '';
      els.stFilter.checked = true;
      els.suspendFilter.checked = true;
      els.quoteFilter.checked = true;
      syncStateFromControls();
    }}

    function exportCsv() {{
      const headers = ['股票代码','六位代码','股票名称','市场板块','交易所','申万一级行业','申万二级行业','上市日期','上市年限','最新收盘价','当日涨跌幅','筹码集中度','是否ST','停牌天数','最新行情','状态','剔除原因'];
      const lines = [headers.join(',')];
      for (const row of shownRows) {{
        const values = [
          row.ts_code,
          row.display_code,
          row.name,
          row.board,
          row.exchange,
          row.sw_l1_name || '',
          row.sw_l2_display || '未分类',
          formatDate(row.list_date),
          formatNumber(row.list_age_years, 2),
          formatNumber(row.close, 2),
          formatPct(row.pct_chg),
          formatPct(row.chip_concentration_70),
          row.is_st ? '是' : '否',
          row.suspendDays,
          row.has_latest_quote ? '有' : '无',
          row.passed ? '通过' : '剔除',
          row.reasons.join('、'),
        ];
        lines.push(values.map((value) => `"${{String(value ?? '').replace(/"/g, '""')}}"`).join(','));
      }}
      const blob = new Blob(['\\ufeff' + lines.join('\\n')], {{ type: 'text/csv;charset=utf-8' }});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `tomorrow_stock_list_${{payload.meta.target_trade_date}}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    }}

    els.dataDate.textContent = formatDate(payload.meta.data_date);
    els.chipDate.textContent = formatDate(payload.meta.chip_data_date);
    els.targetDate.textContent = formatDate(payload.meta.target_trade_date);
    els.generatedAt.textContent = payload.meta.generated_at || '';
    const industryOptions = [...new Set(rows.map((row) => row.sw_l2_display || '未分类'))].sort((a, b) => a.localeCompare(b, 'zh-Hans-CN'));
    for (const name of industryOptions) {{
      const option = document.createElement('option');
      option.value = name;
      option.textContent = name;
      els.industryFilter.appendChild(option);
    }}
    for (const el of [els.poolFilter, els.industryFilter, els.ageFilter, els.suspendWindow, els.pctSort, els.statusFilter, els.stFilter, els.suspendFilter, els.quoteFilter]) {{
      el.addEventListener('change', syncStateFromControls);
    }}
    els.searchBox.addEventListener('input', syncStateFromControls);
    els.resetBtn.addEventListener('click', resetDefaults);
    els.exportBtn.addEventListener('click', exportCsv);
    resetDefaults();
  </script>
</body>
</html>
"""


def build_payload(as_of_date: str | None, cache_dir: Path, refresh_industry: bool = False) -> dict:
    pro = get_tushare_pro()
    context, daily = resolve_trading_context(pro, as_of_date)
    rows, chip_data_date = build_stock_rows(pro, context, daily, cache_dir=cache_dir, refresh_industry=refresh_industry)
    generated_at = current_beijing_timestamp()
    industry_names = sorted({row.get("sw_l2_display") or "未分类" for row in rows})
    return {
        "meta": {
            "as_of_date": context.as_of_date,
            "data_date": context.data_date,
            "chip_data_date": chip_data_date,
            "target_trade_date": context.target_trade_date,
            "generated_at": generated_at,
            "row_count": len(rows),
            "suspend_windows": list(SUSPEND_WINDOWS),
            "industry_src": INDUSTRY_SRC,
            "industry_count": len(industry_names),
            "industries": industry_names,
        },
        "rows": rows,
    }


def write_html(payload: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html = build_html(payload)
    output_path.write_text(html, encoding="utf-8")
    if output_path.resolve() == DEFAULT_OUTPUT.resolve():
        DEFAULT_INDEX_OUTPUT.write_text(html, encoding="utf-8")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an offline HTML stock list for the next A-share trading day.")
    parser.add_argument("--as-of", default=None, help="Calendar date in YYYYMMDD. Defaults to today in Asia/Shanghai.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output HTML path.")
    parser.add_argument("--data-output-dir", default=str(DEFAULT_DATA_OUTPUT_DIR), help="Directory for full local CSV/JSON outputs.")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help="Local cache directory for SW industry data.")
    parser.add_argument("--refresh-industry", action="store_true", help="Refresh SW L2 industry cache from Tushare.")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = ROOT / cache_dir
    payload = build_payload(args.as_of, cache_dir=cache_dir, refresh_industry=args.refresh_industry)
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = ROOT / output_path
    write_html(payload, output_path)
    data_output_dir = Path(args.data_output_dir)
    if not data_output_dir.is_absolute():
        data_output_dir = ROOT / data_output_dir
    summary = write_data_outputs(payload, data_output_dir)
    meta = payload["meta"]
    print(f"output: {output_path}")
    if output_path.resolve() == DEFAULT_OUTPUT.resolve():
        print(f"pages_index: {DEFAULT_INDEX_OUTPUT}")
    print(f"data_output_dir: {data_output_dir}")
    print(f"data_date: {meta['data_date']}")
    print(f"target_trade_date: {meta['target_trade_date']}")
    print(f"rows: {meta['row_count']}")
    print(f"default_passed: {summary['default_passed_count']}")
    print(f"default_excluded: {summary['default_excluded_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
