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
# History (rolling)
# ============================================================
HISTORY_CSV = LOG_DIR / "history.csv"
HISTORY_DAYS = 365  # keep last 365 days of run history

# ============================================================
# Files (repo-relative)
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


def append_history_row(run_date, close, vol20, target_vol, alloc_tqqq) -> pd.DataFrame:
    """
    Appends one row per run to logs/history.csv and trims to last HISTORY_DAYS days.
    Returns the updated history DataFrame.
    """
    row = {
        "RunDate": pd.to_datetime(run_date).date().isoformat(),
        "Close": float(close),
        "RealizedVol20d": float(vol20),
        "TargetVol": float(target_vol),
        "AllocTQQQ": float(alloc_tqqq),
        "AllocCash": float(1.0 - alloc_tqqq),
    }

    if HISTORY_CSV.exists():
        hist = pd.read_csv(HISTORY_CSV, parse_dates=["RunDate"])
    else:
        hist = pd.DataFrame(columns=row.keys())

    hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)

    # Sort + drop duplicates (if you re-run same day)
    hist["RunDate"] = pd.to_datetime(hist["RunDate"])
    hist = hist.sort_values("RunDate")
    hist = hist.drop_duplicates(subset=["RunDate"], keep="last")

    # Trim to last HISTORY_DAYS
    cutoff = pd.Timestamp.utcnow().normalize() - pd.Timedelta(days=HISTORY_DAYS)
    hist = hist[hist["RunDate"] >= cutoff].copy()

    # Write
    hist["RunDate"] = hist["RunDate"].dt.date.astype(str)
    hist.to_csv(HISTORY_CSV, index=False)

    return hist


def main():
    log("Starting weekly TQQQ volatility report")

    # --------------------------------------------------------
    # Fetch latest prices (same python interpreter)
    # --------------------------------------------------------
    if not FETCH_SCRIPT.exists():
        raise FileNotFoundError(f"Missing fetch script: {FETCH_SCRIPT}")

    log("Fetching TQQQ price data")
    subprocess.run([sys.executable, str(FETCH_SCRIPT)], check=True)

    # Sanity check
    if not PRICE_FILE.exists():
        raise FileNotFoundError(
            f"Expected price file not found: {PRICE_FILE}\n"
            f"BASE={BASE}\n"
            f"Tip: ensure fetch_tqqq.py writes to data/TQQQ.csv"
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
    # Append rolling history (365 days)
    # --------------------------------------------------------
    run_date = df.index[-1]
    last_close = float(close.iloc[-1])

    hist = append_history_row(
        run_date=run_date,
        close=last_close,
        vol20=curr_vol,
        target_vol=TARGET_VOL_ANNUAL,
        alloc_tqqq=curr_alloc,
    )
    log(f"Updated history: {HISTORY_CSV} (rows={len(hist)})")

    # Build last 12 table
    last12 = hist.tail(12).copy()
    if not last12.empty:
        last12["AllocTQQQ"] = (last12["AllocTQQQ"] * 100).round(0).astype(int).astype(str) + "%"
        last12["AllocCash"] = (last12["AllocCash"] * 100).round(0).astype(int).astype(str) + "%"
        last12["RealizedVol20d"] = (last12["RealizedVol20d"] * 100).round(1).astype(str) + "%"
        last12["Close"] = last12["Close"].round(2)

        history_table = last12[["RunDate", "Close", "RealizedVol20d", "AllocTQQQ", "AllocCash"]].to_html(
            index=False, escape=False
        )
    else:
        history_table = "<div class='muted'>No history yet.</div>"

    # --------------------------------------------------------
    # HTML report
    # --------------------------------------------------------
    run_date_str = pd.to_datetime(run_date).date().isoformat()

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
  max-width: 860px;
  margin: auto;
  box-shadow: 0 10px 30px rgba(0,0,0,.08);
}}
h1 {{ margin: 0 0 6px 0; }}
h2 {{ margin: 22px 0 8px 0; font-size: 18px; }}
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
table {{
  width: 100%;
  border-collapse: collapse;
  margin-top: 10px;
  font-size: 14px;
}}
th, td {{
  border-bottom: 1px solid #eee;
  padding: 8px 10px;
  text-align: left;
}}
th {{
  background: #fafbfc;
}}
</style>
</head>
<body>
  <div class="card">
    <h1>TQQQ Volatility Target</h1>
    <div class="muted">Weekly sizing based on 20-day realized volatility</div>
    <div class="badge">Target Vol = {TARGET_VOL_ANNUAL:.0%} • Lookback = {LOOKBACK_DAYS} days • Rounding = {int(ROUND_STEP*100)}%</div>

    <div style="height:16px"></div>

    <div class="kv"><b>Date</b><span>{run_date_str}</span></div>
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

    <h2>History (last 12 weekly runs)</h2>
    <div class="muted">Stored in <code>logs/history.csv</code> (rolling last {HISTORY_DAYS} days).</div>
    {history_table}

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
