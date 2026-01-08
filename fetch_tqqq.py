#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import pandas as pd
import yfinance as yf

BASE = Path("/Users/sggmpb13/Library/Mobile Documents/com~apple~CloudDocs/Trading")
DATA_DIR = BASE / "data"
OUT_CSV = DATA_DIR / "TQQQ.csv"

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    df = yf.download(
        tickers="TQQQ",
        period="max",
        interval="1d",
        auto_adjust=False,
        group_by="column",
        progress=False,
    )

    if df is None or df.empty:
        raise RuntimeError("No data returned for TQQQ (network/API issue).")

    # Handle MultiIndex columns if present (e.g., ('Close','TQQQ'))
    if isinstance(df.columns, pd.MultiIndex):
        close = df["Close"]["TQQQ"].rename("Close")
        out = close.to_frame()
    else:
        out = df[["Close"]].copy()

    out = out.reset_index()
    out["Date"] = pd.to_datetime(out["Date"]).dt.date.astype(str)
    out = out[["Date", "Close"]].dropna()

    out.to_csv(OUT_CSV, index=False)
    print(f"Wrote {OUT_CSV} ({len(out)} rows)")
    print(out.tail(3).to_string(index=False))

if __name__ == "__main__":
    main()
