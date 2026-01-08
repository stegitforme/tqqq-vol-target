#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pandas as pd

# ============================================================
# Base paths (repo root)
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
# History tracking
# ============================================================
HISTORY_DAYS = 365
HISTORY_CSV = LOG_DIR / "history.csv"

# ============================================================
# Files
# ============================================================
FETCH_SCRIPT = BASE / "fetch_tqqq.py"
PRICE_FILE = DATA_DIR / "TQQQ.csv"
HTML_FILE = OUT_DIR / "weekly_report.html"
LOG_FILE = LOG_DIR / "friday_run.log"
SUBJECT_FILE = OUT_DIR / "subject.txt"


# ============================================================
# Helpers
# ============================================================
def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
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


def append_history_row(run_date, close, vol20, alloc):
    row = {
        "RunDate": pd.to_datetime(run_date),
        "Close": float(close),
        "RealizedVol20d": float(vol20),
        "AllocTQQQ": float(alloc),
        "AllocCash": float(1.0 - alloc),
    }

    if HISTORY_CSV.exists():
        hist = pd.read_csv(HISTORY_CSV, parse_dates=["RunDate"])
    else:
        hist = pd.DataFrame(columns=row.keys())

    hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
    hist = hist.sort_values("RunDate").drop_duplicates("RunDate", keep="last")

    cutoff = pd.Timestamp.now(tz=timezone.utc) - pd.Timedelta(days=HISTORY_DAYS)
    hist = hist[hist["RunDate"] >= cutoff]

    hist.to_csv(HISTORY_CSV, index=False)
    return hist


# ============================================================
# Main
# ============================================================
def main():
    log("Starting weekly TQQQ volatility report")

    # --------------------------------------------------------
    # Fetch data
    # --------------------------------------------------------
    log("Fetching TQQQ price data")
    subprocess.run([sys.executable, str(FETCH_SCRIPT)], check=True)

    if not PRICE_FILE.exists():
        raise FileNotFoundError(f"Missing price file: {PRICE_FILE}")

    df = pd.read_csv(PRICE_FILE, parse_dates=["Date"])
    df = df.sort_values("Date").set_index("Date")

    close = df["Close"]

    if len(close) < LOOKBACK_DAYS + 5:
        raise RuntimeError("Not enough data to compute volatility")

    # --------------------------------------------------------
    # Compute volatility + allocations
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
    # Subject line (EMAIL + PUSHOVER)
    # --------------------------------------------------------
    subject = (
        f"TQQQ Vol Target | "
        f"{curr_alloc:.0%} TQQQ / {cash_alloc:.0%} Cash | "
        f"Vol20={curr_vol:.1%}"
    )
    SUBJECT_FILE.write_text(subject, encoding="utf-8")
    log(f"Wrote subject: {subject}")

    # --------------------------------------------------------
    # Append history (365 days)
    # --------------------------------------------------------
    run_date = df.index[-1]
    last_close = float(close.iloc[-1])

    hist = append_history_row(
        run_date=run_date,
        close=last_close,
        vol20=curr_vol,
        alloc=curr_alloc,
    )

    # --------------------------------------------------------
    # HTML report
    # --------------------------------------------------------
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>TQQQ Volatility Target</title>
<style>
body {{
  font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial;
  background: #f6f7f9;
  padding: 40px;
}}
.card {{
  background: white;
  padding: 28px;
  border-radius: 14px;
  max-width: 900px;
  margin: auto;
  box-shadow: 0 15px 35px rgba(0,0,0,.08);
}}
h1 {{ margin-top: 0; }}
.kv {{
  display: flex;
  justify-content: space-between;
  padding: 8px 0;
  border-bottom: 1px solid #eee;
}}
.rule {{
  margin-top: 20px;
  background: #f1f4f8;
  padding: 16px;
  border-radius: 10px;
}}
</style>
</head>
<body>
<div class="card">
<h1>TQQQ Volatility Target</h1>
<p>Target Vol = 20% • Lookback = 20 days • Rounding = 5%</p>

<div class="kv"><b>Date</b><span>{run_date.date()}</span></div>
<div class="kv"><b>TQQQ Close</b><span>${last_close:,.2f}</span></div>
<div class="kv"><b>Realized Vol (20d)</b><span>{curr_vol:.1%}</span></div>
<div class="kv"><b>Previous Allocation</b><span>{prev_alloc:.0%} TQQQ</span></div>
<div class="kv"><b>Current Allocation</b><span>{curr_alloc:.0%} TQQQ</span></div>
<div class="kv"><b>Cash / BIL</b><span>{cash_alloc:.0%}</span></div>

<div class="rule">
<b>How it works</b><br>
• Compute 20-day realized volatility (annualized).<br>
• Allocation ≈ TargetVol ÷ RealizedVol.<br>
• Rounded to 5% steps.<br>
• Run after Friday close; trade Monday.
</div>

<p style="color:#666;margin-top:16px;">Generated automatically.</p>
</div>
</body>
</html>
"""

    HTML_FILE.write_text(html, encoding="utf-8")
    log(f"Wrote HTML report: {HTML_FILE}")
    log("Weekly report completed successfully")


if __name__ == "__main__":
    main()
