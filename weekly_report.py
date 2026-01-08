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
# History logging
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


def log(msg: str) -> None:
    # Keep log lines simple & consistent across GitHub Actions + Mac
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


def append_history_row(run_date_iso: str, close: float, vol20: float, target_vol: float, alloc_tqqq: float) -> pd.DataFrame:
    """
    Appends one row per run to logs/history.csv and trims to last HISTORY_DAYS days.
    Returns the updated history DataFrame.
    """
    row = {
        "RunDate": pd.to_datetime(run_date_iso).date().isoformat(),
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

    # Normalize RunDate to tz-naive datetime64[ns] (fixes GitHub Actions tz issues)
    hist["RunDate"] = pd.to_datetime(hist["RunDate"], errors="coerce").dt.tz_localize(None)
    hist = hist.dropna(subset=["RunDate"]).sort_values("RunDate")

    # If rerun same day, keep last
    hist = hist.drop_duplicates(subset=["RunDate"], keep="last")

    # Trim to last HISTORY_DAYS using tz-naive cutoff
    cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=HISTORY_DAYS)
    cutoff = cutoff.tz_localize(None)
    hist = hist.loc[hist["RunDate"] >= cutoff].copy()

    # Write as ISO date strings
    hist_out = hist.copy()
    hist_out["RunDate"] = hist_out["RunDate"].dt.date.astype(str)
    hist_out.to_csv(HISTORY_CSV, index=False)

    return hist_out


def build_history_chart_js(hist: pd.DataFrame) -> str:
    """
    Returns a simple Chart.js snippet using history data.
    No external build tools; loads Chart.js CDN in HTML.
    """
    if hist.empty:
        return "/* no history */"

    # Ensure sorted
    hist = hist.copy()
    hist["RunDate"] = pd.to_datetime(hist["RunDate"])
    hist = hist.sort_values("RunDate")

    labels = hist["RunDate"].dt.strftime("%Y-%m-%d").tolist()
    alloc = (hist["AllocTQQQ"].astype(float) * 100.0).round(1).tolist()
    vol = (hist["RealizedVol20d"].astype(float) * 100.0).round(1).tolist()

    # Embed as JS arrays
    return f"""
const labels = {labels};
const alloc = {alloc};
const vol = {vol};

const ctx = document.getElementById('histChart').getContext('2d');
new Chart(ctx, {{
  type: 'line',
  data: {{
    labels,
    datasets: [
      {{
        label: 'TQQQ Allocation (%)',
        data: alloc,
        yAxisID: 'y',
        tension: 0.25,
      }},
      {{
        label: 'Realized Vol 20d (%)',
        data: vol,
        yAxisID: 'y1',
        tension: 0.25,
      }}
    ]
  }},
  options: {{
    responsive: true,
    interaction: {{ mode: 'index', intersect: false }},
    scales: {{
      y: {{
        title: {{ display: true, text: 'Allocation (%)' }},
        suggestedMin: 0,
        suggestedMax: 100
      }},
      y1: {{
        position: 'right',
        grid: {{ drawOnChartArea: false }},
        title: {{ display: true, text: 'Realized Vol (%)' }},
        suggestedMin: 0,
        suggestedMax: 150
      }}
    }}
  }}
}});
""".strip()


def main() -> None:
    log("Starting weekly TQQQ volatility report")

    # --------------------------------------------------------
    # Fetch latest prices using SAME interpreter
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

    close = df["Close"].astype(float)

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
    # History row (keep 365 days)
    # --------------------------------------------------------
    run_date = df.index[-1].date().isoformat()
    last_close = float(close.iloc[-1])

    hist = append_history_row(
        run_date_iso=run_date,
        close=last_close,
        vol20=curr_vol,
        target_vol=TARGET_VOL_ANNUAL,
        alloc_tqqq=curr_alloc,
    )

    # --------------------------------------------------------
    # HTML report
    # --------------------------------------------------------
    how_it_works = f"""
• Compute {LOOKBACK_DAYS}-day realized volatility (annualized) from daily closes.
• Allocation ≈ TargetVol ÷ RealizedVol (clamped {MIN_ALLOC:.0%}–{MAX_ALLOC:.0%}).
• Round to {int(ROUND_STEP*100)}% steps to reduce churn.
• Run after Friday close; execute Monday after the open.
• “Cash” sleeve can be BIL/SGOV (or your preferred short-term Treasury ETF).
""".strip()

    chart_js = build_history_chart_js(hist)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>TQQQ Volatility Target Report</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
:root {{
  --bg: #f6f7f9;
  --card: #ffffff;
  --text: #111827;
  --muted: #6b7280;
  --line: #e5e7eb;
  --badge: #eef3ff;
}}
body {{
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial;
  background: var(--bg);
  color: var(--text);
  padding: 28px;
}}
.card {{
  background: var(--card);
  padding: 22px;
  border-radius: 14px;
  max-width: 860px;
  margin: 0 auto;
  box-shadow: 0 10px 30px rgba(0,0,0,.08);
}}
h1 {{
  margin: 0 0 6px 0;
  font-size: 26px;
}}
.muted {{
  color: var(--muted);
  font-size: 14px;
}}
.badge {{
  display: inline-block;
  padding: 6px 10px;
  border-radius: 999px;
  background: var(--badge);
  color: #1f2937;
  font-size: 13px;
  margin-top: 10px;
}}
.kv {{
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid var(--line);
  font-size: 16px;
}}
.kv:last-child {{ border-bottom: none; }}
.kv b {{ font-weight: 650; }}
.rule {{
  margin-top: 16px;
  padding: 14px 16px;
  background: #f1f4f8;
  border: 1px solid #e9edf5;
  border-radius: 12px;
  font-size: 14px;
  line-height: 1.5;
  white-space: pre-line;
}}
.split {{
  display: grid;
  grid-template-columns: 1fr;
  gap: 16px;
}}
@media (min-width: 860px) {{
  .split {{ grid-template-columns: 1fr 1fr; }}
}}
.box {{
  background: #fff;
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 14px;
}}
.box h2 {{
  margin: 0 0 10px 0;
  font-size: 16px;
}}
.small {{
  font-size: 12px;
  color: var(--muted);
  margin-top: 12px;
}}
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
  <div class="card">
    <h1>TQQQ Volatility Target</h1>
    <div class="muted">Weekly sizing based on realized volatility</div>
    <div class="badge">Target Vol = {TARGET_VOL_ANNUAL:.0%} • Lookback = {LOOKBACK_DAYS} days • Rounding = {int(ROUND_STEP*100)}%</div>

    <div style="height:14px"></div>

    <div class="split">
      <div class="box">
        <h2>This week</h2>
        <div class="kv"><b>Date</b><span>{run_date}</span></div>
        <div class="kv"><b>TQQQ Close</b><span>${last_close:,.2f}</span></div>
        <div class="kv"><b>Realized Vol ({LOOKBACK_DAYS}d)</b><span>{curr_vol:.1%}</span></div>
        <div class="kv"><b>Previous Allocation</b><span>{prev_alloc:.0%} TQQQ</span></div>
        <div class="kv"><b>Current Allocation</b><span>{curr_alloc:.0%} TQQQ</span></div>
        <div class="kv"><b>Cash / BIL</b><span>{cash_alloc:.0%}</span></div>
      </div>

      <div class="box">
        <h2>How it works</h2>
        <div class="rule">{how_it_works}</div>
      </div>
    </div>

    <div style="height:18px"></div>

    <div class="box">
      <h2>Last {HISTORY_DAYS} days trend (allocation + vol)</h2>
      <canvas id="histChart" height="110"></canvas>
      <div class="small">Data comes from logs/history.csv (updated each run). Allocation is rounded to {int(ROUND_STEP*100)}% steps.</div>
    </div>

    <div class="small">
      Generated by automation. Data source: yfinance.
    </div>
  </div>

<script>
{chart_js}
</script>
</body>
</html>
"""
    HTML_FILE.write_text(html, encoding="utf-8")
    log(f"Wrote report: {HTML_FILE}")
    log("Weekly report completed successfully")


if __name__ == "__main__":
    main()
