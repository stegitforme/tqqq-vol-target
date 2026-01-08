#!/usr/bin/env python3
"""
Fetch TQQQ daily price history and write to data/TQQQ.csv

Works:
- locally
- on GitHub Actions
- without requiring a virtualenv path
"""

from pathlib import Path
import pandas as pd
import yfinance as yf


# -------------------------------------------------
# Paths (repo-root safe)
# -------------------------------------------------
BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
PRICE_FILE = DATA_DIR / "TQQQ.csv"


def main():
    print("[fetch_tqqq] BASE =", BASE)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("[fetch_tqqq] Downloading TQQQ data...")
    df = yf.download(
        tickers="TQQQ",
        period="max",
        interval="1d",
        auto_adjust=False,
        progress=False,
    )

    if df is None or df.empty:
        raise RuntimeError("No data returned from yfinance")

    # Handle MultiIndex columns safely
    if isinstance(df.columns, pd.MultiIndex):
        close = df["Close"]["TQQQ"]
    else:
        close = df["Close"]

    out = (
        close
        .dropna()
        .rename("Close")
        .to_frame()
        .reset_index()
    )

    out["Date"] = pd.to_datetime(out["Date"]).dt.date.astype(str)
    out = out[["Date", "Close"]]

    out.to_csv(PRICE_FILE, index=False)

    print(f"[fetch_tqqq] Wrote {PRICE_FILE} ({len(out)} rows)")
    print(out.tail(3))


if __name__ == "__main__":
    main()
