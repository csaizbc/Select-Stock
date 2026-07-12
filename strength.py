from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


HISTORY_DAYS = 120
MIN_HISTORY_DAYS = 60
TOP_LIMIT = 200


def _is_limit_up(pct_chg: float, board: str) -> bool:
    """Conservative tradability filter; buffers absorb quote rounding."""
    threshold = 29.5 if board == "北交所" else 19.5 if board in {"创业板", "科创板"} else 9.5
    return pct_chg >= threshold


def fetch_price_history(pro, trade_dates: list[str], cache_dir: Path, call_with_retry) -> pd.DataFrame:
    """Load daily bars incrementally. One small cache file is kept per trading day."""
    daily_dir = cache_dir / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    frames: list[pd.DataFrame] = []
    fields = "ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount"
    for trade_date in trade_dates[-HISTORY_DAYS:]:
        path = daily_dir / f"{trade_date}.csv"
        if path.exists():
            frame = pd.read_csv(path, dtype={"ts_code": str, "trade_date": str})
        else:
            frame = call_with_retry(pro.daily, trade_date=trade_date, fields=fields)
            if frame.empty:
                continue
            frame.to_csv(path, index=False, encoding="utf-8-sig")
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    history = pd.concat(frames, ignore_index=True)
    for col in ["open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"]:
        history[col] = pd.to_numeric(history.get(col), errors="coerce")
    history["trade_date"] = history["trade_date"].astype(str).str.replace(r"\D", "", regex=True).str[:8]
    return history.drop_duplicates(["ts_code", "trade_date"], keep="last").sort_values(["ts_code", "trade_date"])


def _score_group(group: pd.DataFrame) -> dict | None:
    g = group.sort_values("trade_date").tail(HISTORY_DAYS).reset_index(drop=True)
    if len(g) < MIN_HISTORY_DAYS or g["close"].isna().any():
        return None
    close, high, vol = g["close"], g["high"], g["vol"]
    latest = g.iloc[-1]
    ma5, ma10, ma20 = (float(close.tail(n).mean()) for n in (5, 10, 20))
    vol5 = float(vol.tail(5).mean())
    ret5 = (float(close.iloc[-1] / close.iloc[-6]) - 1) * 100
    ret10 = (float(close.iloc[-1] / close.iloc[-11]) - 1) * 100
    ret20 = (float(close.iloc[-1] / close.iloc[-21]) - 1) * 100
    high20_prev = float(high.iloc[-21:-1].max())
    high60_prev = float(high.iloc[-61:-1].max())
    distance_high20 = (float(latest.close) / high20_prev - 1) * 100
    volume_ratio = float(latest.vol / vol5) if vol5 > 0 else 0
    day_range = float(latest.high - latest.low)
    close_position = float((latest.close - latest.low) / day_range) if day_range > 0 else 0.5
    distance_ma5 = (float(latest.close) / ma5 - 1) * 100
    distance_ma10 = (float(latest.close) / ma10 - 1) * 100

    trend = latest.close > ma5 > ma10 > ma20
    breakout = latest.close >= high20_prev * 0.995 and volume_ratio >= 1.2
    prior_breakout = float(high.iloc[-11:-1].max()) >= float(high.iloc[-31:-11].max()) * 0.995
    pullback = (
        prior_breakout
        and (abs(distance_ma5) <= 1.5 or abs(distance_ma10) <= 1.5)
        and volume_ratio <= 0.85
        and latest.close >= ma20
        and latest.close >= latest.open
    )

    trend_score = 25 if trend else (15 if latest.close > ma10 > ma20 else 0)
    momentum_score = min(20, max(0, ret5 * 0.8 + ret10 * 0.4 + ret20 * 0.2))
    breakout_score = 15 if breakout else (10 if distance_high20 >= -3 else 0)
    volume_score = 15 if 1.2 <= volume_ratio <= 2.5 else (8 if 0.8 <= volume_ratio < 1.2 else 0)
    liquidity_score = 10 if float(latest.amount) >= 100_000 else (5 if float(latest.amount) >= 50_000 else 0)
    price = float(latest.close)
    price_score = 10 if price <= 8 else 8 if price <= 12 else 6 if price <= 15 else 3 if price <= 20 else 0
    close_score = round(max(0, min(5, close_position * 5)), 1)
    risk_penalty = 0
    risks: list[str] = []
    upper_shadow = (float(latest.high - latest.close) / day_range) if day_range > 0 else 0
    if upper_shadow > 0.45 and volume_ratio > 1.5:
        risk_penalty += 12; risks.append("放量长上影")
    if ret5 > 25 or ret20 > 45:
        risk_penalty += 10; risks.append("短期涨幅过大")
    if volume_ratio > 3:
        risk_penalty += 8; risks.append("成交量异常放大")
    score = round(trend_score + momentum_score + breakout_score + volume_score + liquidity_score + price_score + close_score - risk_penalty, 1)
    patterns = [name for ok, name in [(trend, "趋势强势"), (breakout, "平台突破"), (pullback, "缩量回踩")] if ok]
    if not patterns or score < 55 or not (-5 <= ret5 <= 25) or ret20 > 45:
        return None
    return {
        "ts_code": latest.ts_code, "score": score, "patterns": patterns, "risk_flags": risks,
        "close": round(float(latest.close), 2), "pct_chg": round(float(latest.pct_chg), 2),
        "ret5": round(ret5, 2), "ret10": round(ret10, 2), "ret20": round(ret20, 2),
        "ma5": round(ma5, 2), "ma10": round(ma10, 2), "ma20": round(ma20, 2),
        "distance_high20": round(distance_high20, 2), "volume_ratio": round(volume_ratio, 2),
        "close_position": round(close_position * 100, 1), "amount_yi": round(float(latest.amount) / 100_000, 2),
        "high60": round(high60_prev, 2),
        "score_detail": {"趋势": trend_score, "动量": round(momentum_score, 1), "突破": breakout_score,
                         "量价": volume_score, "流动性": liquidity_score, "低价": price_score,
                         "收盘质量": close_score, "风险扣分": risk_penalty},
    }


def build_strength_rows(history: pd.DataFrame, stock_rows: list[dict]) -> list[dict]:
    if history.empty:
        return []
    metadata = {row["ts_code"]: row for row in stock_rows}
    results: list[dict] = []
    for _, group in history.groupby("ts_code", sort=False):
        item = _score_group(group)
        meta = metadata.get(item["ts_code"]) if item else None
        if (not item or not meta or meta.get("is_st") or not meta.get("has_latest_quote")
                or meta.get("list_age_days", 0) < 120 or _is_limit_up(item["pct_chg"], meta.get("board", ""))):
            continue
        item.update({"name": meta["name"], "board": meta["board"], "industry": meta["sw_l2_display"]})
        results.append(item)
    return sorted(results, key=lambda row: (-row["score"], row["ts_code"]))[:TOP_LIMIT]


def write_strength_json(rows: list[dict], meta: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"meta": meta, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
