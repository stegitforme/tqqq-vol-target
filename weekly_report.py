#!/usr/bin/env python3
from __future__ import annotations

import math
import os
import sys
import subprocess
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd

# ============================================================
# Repo paths (weekly_report.py is in repo root)
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
TARGET_VOL_ANNUAL = 0.20      # 20% target
TRADING_DAYS = 252
ROUND_STEP = 0.05             # 5% steps
MAX_ALLOC = 1.0
MIN_ALLOC = 0.0

# ============================================================
# History retention (for trend chart)
# ============================================================
HISTORY_DAYS = 365
HISTORY_CSV = LOG_DIR / "history.csv"

# ============================================================
# Files (repo-relative)
# ============================================================
FETCH_SCRIPT = BASE / "fetch_tqqq.py"
PRICE_FILE = DATA_DIR / "TQQQ.csv"
HTML_FILE = OUT_DIR / "weekly_report.html"
LOG_FILE = LOG_DIR / "friday_run.log"
SUBJECT_FILE = OUT_DIR / "subject.txt"


def log(msg: str) -> None:
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


def append_history_row(run_date, close, vol20, target_vol, alloc_tqqq) -> pd.DataFrame:
    """
    Append one row per run to logs/history.csv and trim to last HISTORY_DAYS.
    Uses tz-naive dates consistently to avoid tz compare errors in pandas.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Force date-only string (tz-naive)
    run_date_str = pd.to_datetime(run_date).date().isoformat()

    row = {
        "RunDate": run_date_str,
        "Close": float(close),
        "RealizedVol20d": float(vol20),
        "TargetVol": float(target_vol),
        "AllocTQQQ": float(alloc_tqqq),
        "AllocCash": float(1.0 - float(alloc_tqqq)),
    }

    if HISTORY_CSV.exists():
        hist = pd.read_csv(HISTORY_CSV)
    else:
        hist = pd.DataFrame(columns=row.keys())

    hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)

    # Parse as tz-naive datetime64[ns]
    hist["RunDate"] = pd.to_datetime(hist["RunDate"], errors="coerce").dt.tz_localize(None)
    hist = hist.dropna(subset=["RunDate"]).sort_values("RunDate")

    # Dedup if re-run same date
    hist = hist.drop_duplicates(subset=["RunDate"], keep="last")

    # Cutoff is tz-naive too
    cutoff = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None) - pd.Timedelta(days=HISTORY_DAYS)
    hist = hist.loc[hist["RunDate"] >= cutoff].copy()

    # Write back as YYYY-MM-DD strings
    hist_out = hist.copy()
    hist_out["RunDate"] = hist_out["RunDate"].dt.date.astype(str)
    hist_out.to_csv(HISTORY_CSV, index=False)

    return hist_out


def make_subject(curr_alloc: float, cash_alloc: float, vol20: float) -> str:
    return f"TQQQ Vol Target | {curr_alloc:.0%} TQQQ / {cash_alloc:.0%} Cash | Vol20={vol20:.1%}"


def main() -> None:
    log("Starting weekly TQQQ volatility report")

    # --------------------------------------------------------
    # Fetch latest prices using the SAME python interpreter
    # --------------------------------------------------------
    log("Fetching TQQQ price data")
    subprocess.run([sys.executable, str(FETCH_SCRIPT)], check=True)

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
    # Append history (for 365d trend chart)
    # --------------------------------------------------------
    run_date = df.index[-1].date().isoformat()
    last_close = float(close.iloc[-1])

    hist = append_history_row(
        run_date=run_date,
        close=last_close,
        vol20=curr_vol,
        target_vol=TARGET_VOL_ANNUAL,
        alloc_tqqq=curr_alloc,
    )

    # --------------------------------------------------------
    # Subject line for email/push
    # --------------------------------------------------------
    subject = make_subject(curr_alloc, cash_alloc, curr_vol)
    SUBJECT_FILE.write_text(subject, encoding="utf-8")
    log(f"Wrote subject: {subject}")

    # Prepare chart arrays (last 365d in history.csv)
    hist2 = hist.copy()
    hist2["RunDate"] = pd.to_datetime(hist2["RunDate"]).dt.strftime("%Y-%m-%d")
    labels_js = hist2["RunDate"].tolist()
    alloc_js = (hist2["AllocTQQQ"] * 100.0).round(2).tolist()
    vol_js = (hist2["RealizedVol20d"] * 100.0).round(2).tolist()

    labels_js_str = "[" + ",".join([f"'{x}'" for x in labels_js]) + "]"
    alloc_js_str = "[" + ",".join([str(x) for x in alloc_js]) + "]"
    vol_js_str = "[" + ",".join([str(x) for x in vol_js]) + "]"

    # --------------------------------------------------------
    # HTML report (includes chart)
    # --------------------------------------------------------
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>TQQQ Volatility Target</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
body {{
  font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial;
  background: #f6f7f9;
  padding: 28px;
}}
.wrap {{
  max-width: 980px;
  margin: 0 auto;
}}
h1 {{
  margin: 0 0 6px 0;
  font-size: 34px;
}}
.sub {{
  color: #6b7280;
  font-size: 16px;
  margin-bottom: 14px;
}}
.pill {{
  display: inline-block;
  padding: 7px 12px;
  border-radius: 999px;
  background: #eef3ff;
  color: #1f2937;
  font-size: 13px;
}}
.grid {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-top: 16px;
}}
.card {{
  background: white;
  border-radius: 14px;
  box-shadow: 0 10px 30px rgba(0,0,0,.08);
  padding: 18px 18px;
}}
.card h2 {{
  margin: 0 0 12px 0;
  font-size: 18px;
}}
.kv {{
  display: flex;
  justify-content: space-between;
  padding: 10px 0;
  border-bottom: 1px solid #eef2f7;
  font-size: 16px;
}}
.kv:last-child {{ border-bottom: none; }}
.box {{
  background: #f1f4f8;
  border-radius: 12px;
  padding: 14px 16px;
  line-height: 1.5;
  color: #111827;
}}
.chart-card {{
  margin-top: 16px;
}}
.muted {{
  color: #6b7280;
  font-size: 13px;
  margin-top: 10px;
}}
@media (max-width: 840px) {{
  .grid {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>
  <div class="wrap">
    <h1>TQQQ Volatility Target</h1>
    <div class="sub">Weekly sizing based on realized volatility</div>
    <div class="pill">Target Vol = {TARGET_VOL_ANNUAL:.0%} • Lookback = {LOOKBACK_DAYS} days • Rounding = {int(ROUND_STEP*100)}%</div>

    <div class="grid">
      <div class="card">
        <h2>This week</h2>
        <div class="kv"><b>Date</b><span>{run_date}</span></div>
        <div class="kv"><b>TQQQ Close</b><span>${last_close:,.2f}</span></div>
        <div class="kv"><b>Realized Vol (20d)</b><span>{curr_vol:.1%}</span></div>
        <div style="height:8px"></div>
        <div class="kv"><b>Previous Allocation</b><span>{prev_alloc:.0%} TQQQ</span></div>
        <div class="kv"><b>Current Allocation</b><span>{curr_alloc:.0%} TQQQ</span></div>
        <div class="kv"><b>Cash / BIL</b><span>{cash_alloc:.0%}</span></div>
      </div>

      <div class="card">
        <h2>How it works</h2>
        <div class="box">
          • Compute 20-day realized volatility (annualized) from daily closes.<br/>
          • Allocation ≈ TargetVol ÷ RealizedVol (clamped 0–100%).<br/>
          • Round to {int(ROUND_STEP*100)}% steps to reduce churn.<br/>
          • Run after Friday close; execute Monday after the open.<br/>
          • “Cash” sleeve can be BIL/SGOV (or your preferred T-bill ETF).
        </div>
      </div>
    </div>

    <div class="card chart-card">
      <h2>Last {HISTORY_DAYS} days trend (allocation + vol)</h2>
      <canvas id="trend" height="110"></canvas>
      <div class="muted">
        Data comes from logs/history.csv (updated each run). Allocation is rounded to {int(ROUND_STEP*100)}% steps.
      </div>
    </div>

    <div class="muted" style="margin-top:12px;">
      Generated by automation. Data source: yfinance.
    </div>
  </div>

<script>
const labels = {labels_js_str};
const alloc = {alloc_js_str};
const vol = {vol_js_str};

const ctx = document.getElementById('trend').getContext('2d');
new Chart(ctx, {{
  type: 'line',
  data: {{
    labels,
    datasets: [
      {{
        label: 'TQQQ Allocation (%)',
        data: alloc,
        borderWidth: 2,
        tension: 0.25,
        yAxisID: 'y'
      }},
      {{
        label: 'Realized Vol 20d (%)',
        data: vol,
        borderWidth: 2,
        tension: 0.25,
        yAxisID: 'y1'
      }}
    ]
  }},
  options: {{
    responsive: true,
    interaction: {{
      mode: 'index',
      intersect: false
    }},
    plugins: {{
      legend: {{
        position: 'top'
      }}
    }},
    scales: {{
      y: {{
        title: {{ display: true, text: 'Allocation (%)' }},
        min: 0,
        max: 100
      }},
      y1: {{
        position: 'right',
        title: {{ display: true, text: 'Realized Vol (%)' }},
        grid: {{ drawOnChartArea: false }}
      }}
    }}
  }}
}});
</script>
</body>
</html>
"""
    HTML_FILE.write_text(html, encoding="utf-8")
    log(f"Wrote report: {HTML_FILE}")
    log("Weekly report completed successfully")


if __name__ == "__main__":
    main()
