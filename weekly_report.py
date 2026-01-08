#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
import subprocess
from pathlib import Path
from datetime import datetime
import pandas as pd

# ============================================================
# Base paths (repo-relative, works everywhere)
# ============================================================
BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
OUT_DIR = BASE / "output"
LOG_DIR = BASE / "logs"

DATA_DIR.mkdir(exist_ok=True)
OUT_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ============================================================
# Strategy parameters (LOCKED)
# ============================================================
LOOKBACK_DAYS = 20
TARGET_VOL_ANNUAL = 0.20        # 20% volatility target
TRADING_DAYS = 252
ROUND_STEP = 0.05               # 5% allocation steps
MAX_ALLOC = 1.0
MIN_ALLOC = 0.0

# ============================================================
# Files
# ============================================================
FETCH_SCRIPT = BASE / "fetch_tqqq.py"
PRICE_FILE = DATA_DIR / "TQQQ.csv"
HTML_FILE = OUT_DIR / "weekly_report.html"
LOG_FILE = LOG_DIR / "friday_run.log"

# ============================================================
# Helpers
# ============================================================
def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def realized_vol(close: pd.Series, window: int) -> float:
    rets = close.pct_change().dropna()
    vol_daily = rets.rolling(window).std().iloc[-1]
    return vol_daily * math.sqrt(TRADING_DAYS)


def round_step(x: float, step: float) -> float:
    return round(x / step) * step


# ============================================================
# Main
# ============================================================
def main():
    log("Starting weekly TQQQ volatility report")

    # --------------------------------------------------------
    # Fetch latest prices (uses SAME python interpreter)
    # --------------------------------------------------------
    log("Fetching TQQQ price data")
    subprocess.run([sys.executable, str(FETCH_SCRIPT)], check=True)

    # --------------------------------------------------------
    # Load price data
    # --------------------------------------------------------
    df = pd.read_csv(PRICE_FILE, parse_dates=["Date"])
    df = df.sort_values("Date").set_index("Date")

    if len(df) < LOOKBACK_DAYS + 5:
        raise RuntimeError("Not enough data to compute volatility")

    close = df["Close"]

    # --------------------------------------------------------
    # Compute volatility + allocations
    # --------------------------------------------------------
    curr_vol = realized_vol(close, LOOKBACK_DAYS)
    prev_vol = realized_vol(close.iloc[:-1], LOOKBACK_DAYS)

    curr_alloc = TARGET_VOL_ANNUAL / curr_vol
    prev_alloc = TARGET_VOL_ANNUAL / prev_vol

    curr_alloc = max(MIN_ALLOC, min(MAX_ALLOC, curr_alloc))
    prev_alloc = max(MIN_ALLOC, min(MAX_ALLOC, prev_alloc))

    curr_alloc = round_step(curr_alloc, ROUND_STEP)
    prev_alloc = round_step(prev_alloc, ROUND_STEP)

    cash_alloc = 1.0 - curr_alloc

    log(f"Prev: {prev_alloc:.0%}  Curr: {curr_alloc:.0%}  Cash: {cash_alloc:.0%}")

    # --------------------------------------------------------
    # Build HTML report
    # --------------------------------------------------------
    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>TQQQ Volatility Target Report</title>
<style>
body {{
    font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica;
    background: #f6f7f9;
    padding: 40px;
}}
.card {{
    background: white;
    padding: 24px;
    border-radius: 10px;
    max-width: 720px;
    margin: auto;
    box-shadow: 0 8px 30px rgba(0,0,0,.06);
}}
h1 {{ margin-top: 0; }}
.kv {{
    display: flex;
    justify-content: space-between;
    margin: 10px 0;
    font-size: 18px;
}}
.small {{
    color: #666;
    font-size: 14px;
}}
.rule {{
    margin-top: 18px;
    padding: 14px;
    background: #f1f4f8;
    border-radius: 8px;
    font-size: 14px;
}}
</style>
</head>

<body>
<div class="card">
<h1>TQQQ Volatility Target</h1>

<div class="kv"><b>Date</b><span>{df.index[-1].date()}</span></div>
<div class="kv"><b>Target Volatility</b><span>20%</span></div>
<div class="kv"><b>20-Day Realized Vol</b><span>{curr_vol:.1%}</span></div>

<hr>

<div class="kv"><b>Previous Allocation</b><span>{prev_alloc:.0%} TQQQ</span></div>
<div class="kv"><b>Current Allocation</b><span>{curr_alloc:.0%} TQQQ</span></div>
<div class="kv"><b>Cash / BIL</b><span>{cash_alloc:.0%}</span></div>

<div class="rule">
<b>How this works</b><br><br>
Allocation = Target Vol ÷ Realized Vol<br>
Target Vol = 20%<br>
Lookback = 20 trading days<br>
Rounded to 5% steps<br>
Computed weekly after Friday close<br>
Executed Monday after open
</div>

<div class="small" style="margin-top:16px;">
Generated automatically by GitHub Actions
</div>

</div>
</body>
</html>
"""

    HTML_FILE.write_text(html)
    log(f"Wrote report: {HTML_FILE}")

    log("Weekly report completed successfully")


if __name__ == "__main__":
    main()
