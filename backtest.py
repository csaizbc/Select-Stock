from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


HORIZONS = (1, 3, 5)


def update_backtest(cache_dir: Path, candidates: list[dict], data_date: str, output_dir: Path) -> dict:
    """Append today's signals and incrementally settle close-to-close forward returns."""
    output_dir.mkdir(parents=True, exist_ok=True)
    files = sorted((cache_dir / "daily").glob("*.csv"))[-120:]
    history = pd.concat([pd.read_csv(path, dtype={"ts_code": str, "trade_date": str}) for path in files], ignore_index=True) if files else pd.DataFrame()
    ledger_path = output_dir / "strength_backtest.csv"
    columns = ["signal_date", "ts_code", "name", "score", "patterns", "entry_close", "ret_1d", "ret_3d", "ret_5d"]
    if ledger_path.exists():
        ledger = pd.read_csv(ledger_path, dtype={"signal_date": str, "ts_code": str})
    else:
        ledger = pd.DataFrame(columns=columns)

    existing = set(zip(ledger.get("signal_date", []), ledger.get("ts_code", [])))
    additions = [{
        "signal_date": data_date, "ts_code": row["ts_code"], "name": row["name"], "score": row["score"],
        "patterns": "|".join(row["patterns"]), "entry_close": row["close"],
        "ret_1d": pd.NA, "ret_3d": pd.NA, "ret_5d": pd.NA,
    } for row in candidates if (data_date, row["ts_code"]) not in existing]
    if additions:
        ledger = pd.concat([ledger, pd.DataFrame(additions)], ignore_index=True)

    price_maps = {code: group.sort_values("trade_date").set_index("trade_date")["close"]
                  for code, group in history.groupby("ts_code", sort=False)} if not history.empty else {}
    for idx, row in ledger.iterrows():
        prices = price_maps.get(row["ts_code"])
        if prices is None or row["signal_date"] not in prices.index:
            continue
        dates = list(prices.index)
        pos = dates.index(row["signal_date"])
        entry = float(row["entry_close"])
        for horizon in HORIZONS:
            col = f"ret_{horizon}d"
            if pd.isna(row.get(col)) and pos + horizon < len(dates):
                ledger.at[idx, col] = round((float(prices.iloc[pos + horizon]) / entry - 1) * 100, 3)

    ledger = ledger[columns].sort_values(["signal_date", "score"], ascending=[False, False])
    ledger.to_csv(ledger_path, index=False, encoding="utf-8-sig")
    summary: dict = {"method": "信号日收盘买入，持有至第1/3/5个交易日收盘；未计手续费、滑点及涨跌停成交限制", "samples": {}}
    for horizon in HORIZONS:
        values = pd.to_numeric(ledger[f"ret_{horizon}d"], errors="coerce").dropna()
        summary["samples"][f"{horizon}d"] = {
            "count": int(len(values)), "avg_return": round(float(values.mean()), 3) if len(values) else None,
            "win_rate": round(float(values.gt(0).mean() * 100), 2) if len(values) else None,
            "median_return": round(float(values.median()), 3) if len(values) else None,
        }
    (output_dir / "strength_backtest_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
