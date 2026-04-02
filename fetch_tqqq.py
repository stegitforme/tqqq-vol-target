#!/usr/bin/env python3
"""
Fetch TQQQ and QQQ daily price history.
Writes:
  data/TQQQ.csv
  data/QQQ.csv
"""
from pathlib import Path
import pandas as pd
import yfinance as yf

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"

def fetch_ticker(ticker: str) -> None:
    out_file = DATA_DIR / f"{ticker}.csv"
    print(f"[fetch] Downloading {ticker}...")
    df = yf.download(
        tickers=ticker,
        period="max",
        interval="1d",
        auto_adjust=False,
        progress=False,
    )
    if df is None or df.empty:
        raise RuntimeError(f"No data returned for {ticker}")

    if isinstance(df.columns, pd.MultiIndex):
        close = df["Close"][ticker]
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
    out.to_csv(out_file, index=False)
    print(f"[fetch] Wrote {out_file} ({len(out)} rows), latest: {out['Date'].iloc[-1]}")

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fetch_ticker("TQQQ")
    fetch_ticker("QQQ")

if __name__ == "__main__":
    main()
