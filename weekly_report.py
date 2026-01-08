#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
import subprocess
from pathlib import Path
from datetime import datetime
import pandas as pd

# ============================================================
# Base paths (repo root)
# weekly_report.py lives in repo root, so BASE is repo root.
# ============================================================
BASE = Path(__file__).resolve().parent

DATA_DIR = BASE / "data"
OUT_DIR = BASE / "output"
LOG_DIR = BASE / "logs"

DATA_DIR.mkdir(exist_ok=True)
OUT_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ============================================================
# Strategy parameters
# ============================================================
LOOKBACK_DAYS = 20
TARGET_VOL_ANNUAL = 0.20        # 20% target
TRADING_DAYS = 252
ROUND_STEP = 0.05               # 5% steps
MAX_ALLOC = 1.0
MIN_ALLOC = 0.0

# ============================================================
# Files (ALL repo-relative)
# ============================================================
FETCH_SCRIPT = BASE / "fetch_tqqq.py"
PRICE_FILE = DATA_DIR / "TQQQ.csv"
HTML_FILE = OUT_DIR / "weekly_report.html"
LOG_FILE = LOG_DIR / "friday_run.log"


def log(msg: str):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def realized_vol(close: pd.Series, window: int) -> float:
    rets = close.pct_change().dropna()
    vol_daily = rets.rolling(window).std().iloc[-1]
    return float(vol_daily) * math.sqrt(TRADING_DAYS)


def round_step(x: float, step: float) -> float:
    return round(x / step) * step


def main():
    log("Starting weekly TQQQ volatility report")

    # --------------------------------------------------------
    # Fetch latest prices using the SAME python interpreter
    # --------------------------------------------------------
    log("Fetching TQQQ price data")
    subprocess.run([sys.executable, str(FETCH_SCRIPT)], check=True)

    # Sanity check: did the fetch script create the file where we expect?
    if not PRICE_FILE.exists():
        raise FileNotFoundError(
            f"Expected price file not found: {PRICE_FILE}\n"
            f"Repo root BASE={BASE}\n"
            f"Tip: ensure fetch_tqqq.py writes to data/TQQQ.csv in the repo."
        )

    # --------------------------------------------------------
    # Load price data
    # --------------------------------------------------------
    df = pd.read_csv(PRICE_FILE, parse_dates=["Date"])
    df = df.sort_values("Date").set_index("Date")

    if len(df) < LOOKBACK_DAYS + 5:
        raise RuntimeError(f"Not enough rows in {PRICE_FILE} to compute volatility.")

    close = df["Close"]

    # --------------------------------------------------------
    # Compute vol + allocations
    # --------------------------------------------------------
    curr_vol = realized_vol(close, LOOKBACK_DAYS)
    prev_vol = realized_vol(close.iloc[:-1], LOOKBACK_DAYS)

    curr_alloc = TARGET_VOL_ANNUAL / curr_vol if curr_vol > 0 else 1.0
    prev_alloc = TARGET_VOL_ANNUAL / prev_vol if prev_vol > 0 else 1.0

    curr_alloc = max(MIN_ALLOC, min(MAX_ALLOC, curr_alloc))
    prev_alloc = max(MIN_ALLOC, min(MAX_ALLOC, prev_alloc))

    curr_alloc = round_step(curr_alloc, ROUND_STEP)
    prev_alloc = round_step(prev_alloc, ROUND_STEP)

    cash_alloc = 1.0 - curr_alloc

    log(f"Prev: {prev_alloc:.0%}  Curr: {curr_alloc:.0%}  Cash: {cash_alloc:.0%}")

    # --------------------------------------------------------
    # HTML report
    # --------------------------------------------------------
    run_date = df.index[-1].date().isoformat()
    last_close = float(close.iloc[-1])

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>TQQQ Volatility Target Report</title>
<style>
body {{
  font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial;
  background: #f6f7f9;
  padding: 40px;
}}
.card {{
  background: white;
  padding: 24px;
  border-radius: 12px;
  max-width: 760px;
  margin: auto;
  box-shadow: 0 10px 30px rgba(0,0,0,.08);
}}
h1 {{ margin: 0 0 6px 0; }}
.muted {{ color: #666; font-size: 14px; }}
.kv {{
  display: flex;
  justify-content: space-between;
  padding: 8px 0;
  border-bottom: 1px solid #eee;
  font-size: 16px;
}}
.kv:last-child {{ border-bottom: none; }}
.badge {{
  display: inline-block;
  padding: 4px 10px;
  border-radius: 999px;
  background: #eef3ff;
  color: #234;
  font-size: 13px;
  margin-top: 10px;
}}
.rule {{
  margin-top: 16px;
  padding: 14px 16px;
  background: #f1f4f8;
  border-radius: 10px;
  font-size: 14px;
  line-height: 1.45;
}}
</style>
</head>
<body>
  <div class="card">
    <h1>TQQQ Volatility Target</h1>
    <div class="muted">Weekly sizing based on 20-day realized volatility</div>
    <div class="badge">Target Vol = {TARGET_VOL_ANNUAL:.0%} • Lookback = {LOOKBACK_DAYS} days • Rounding = {int(ROUND_STEP*100)}%</div>

    <div style="height:16px"></div>

    <div class="kv"><b>Date</b><span>{run_date}</span></div>
    <div class="kv"><b>TQQQ Close</b><span>${last_close:,.2f}</span></div>
    <div class="kv"><b>Realized Vol (20d)</b><span>{curr_vol:.1%}</span></div>

    <div style="height:10px"></div>

    <div class="kv"><b>Previous Allocation</b><span>{prev_alloc:.0%} TQQQ</span></div>
    <div class="kv"><b>Current Allocation</b><span>{curr_alloc:.0%} TQQQ</span></div>
    <div class="kv"><b>Cash / BIL</b><span>{cash_alloc:.0%}</span></div>

    <div class="rule">
      <b>How it works</b><br>
      • Compute 20-day realized volatility (annualized) from daily closes.<br>
      • Allocation ≈ TargetVol ÷ RealizedVol (clamped 0–100%).<br>
      • Rounded to {int(ROUND_STEP*100)}% steps to reduce churn.<br>
      • Run after Friday close; execute Monday after open.
    </div>

    <div class="muted" style="margin-top:14px;">
      Generated by automation. Data source: yfinance.
    </div>
  </div>
</body>
</html>
"""
    HTML_FILE.write_text(html, encoding="utf-8")
    log(f"Wrote report: {HTML_FILE}")

    log("Weekly report completed successfully")


if __name__ == "__main__":
    main()
