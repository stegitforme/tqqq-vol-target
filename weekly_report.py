#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
import subprocess
from pathlib import Path
from datetime import datetime, timezone
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

HISTORY_CSV = LOG_DIR / "history.csv"
HISTORY_DAYS = 365  # keep last 365 days of run history


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


def append_history_row(run_date_iso: str, close: float, vol20: float, target_vol: float, alloc_tqqq: float) -> pd.DataFrame:
    """
    Appends one row per run to logs/history.csv and trims to last HISTORY_DAYS days.
    Uses tz-naive dates everywhere to avoid tz-aware comparison errors.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    row = {
        "RunDate": run_date_iso,   # ISO date string "YYYY-MM-DD"
        "Close": float(close),
        "RealizedVol20d": float(vol20),
        "TargetVol": float(target_vol),
        "AllocTQQQ": float(alloc_tqqq),
        "AllocCash": float(1.0 - alloc_tqqq),
    }

    if HISTORY_CSV.exists():
        hist = pd.read_csv(HISTORY_CSV)
    else:
        hist = pd.DataFrame(columns=row.keys())

    # Append row
    hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)

    # Normalize RunDate to datetime (tz-naive)
    hist["RunDate"] = pd.to_datetime(hist["RunDate"]).dt.normalize()

    # Drop duplicates if re-run same day
    hist = hist.sort_values("RunDate").drop_duplicates(subset=["RunDate"], keep="last")

    # Trim to last HISTORY_DAYS using tz-naive cutoff
    cutoff = (pd.Timestamp.now().normalize() - pd.Timedelta(days=HISTORY_DAYS))
    hist = hist[hist["RunDate"] >= cutoff].copy()

    # Write back as YYYY-MM-DD strings
    hist_out = hist.copy()
    hist_out["RunDate"] = hist_out["RunDate"].dt.date.astype(str)
    hist_out.to_csv(HISTORY_CSV, index=False)

    return hist_out


def build_subject_and_message(run_date: str, curr_alloc: float, cash_alloc: float, curr_vol: float) -> tuple[str, str]:
    """
    Creates a clean subject + message for email/push.
    """
    subject = f"TQQQ Vol Target | {curr_alloc:.0%} TQQQ / {cash_alloc:.0%} Cash | Vol20={curr_vol:.1%}"
    message = (
        f"{subject}\n"
        f"Date={run_date}\n"
        f"Report generated."
    )
    return subject, message


def build_chart_js_from_history(hist: pd.DataFrame) -> str:
    """
    Builds Chart.js data arrays from history.csv (last HISTORY_DAYS).
    """
    # Ensure sorted
    hist = hist.copy()
    hist["RunDate"] = pd.to_datetime(hist["RunDate"])
    hist = hist.sort_values("RunDate")

    labels = [d.strftime("%Y-%m-%d") for d in hist["RunDate"]]
    alloc = [float(x) * 100.0 for x in hist["AllocTQQQ"]]
    vol = [float(x) * 100.0 for x in hist["RealizedVol20d"]]

    # JS arrays
    labels_js = "[" + ",".join([f"'{x}'" for x in labels]) + "]"
    alloc_js = "[" + ",".join([f"{x:.2f}" for x in alloc]) + "]"
    vol_js = "[" + ",".join([f"{x:.2f}" for x in vol]) + "]"

    return labels_js, alloc_js, vol_js


def main():
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
    # Run date + last close
    # --------------------------------------------------------
    run_date = df.index[-1].date().isoformat()
    last_close = float(close.iloc[-1])

    # --------------------------------------------------------
    # Update history.csv (drives chart)
    # --------------------------------------------------------
    hist = append_history_row(
        run_date_iso=run_date,
        close=last_close,
        vol20=curr_vol,
        target_vol=TARGET_VOL_ANNUAL,
        alloc_tqqq=curr_alloc
    )

    # --------------------------------------------------------
    # Subject + message for notifications
    # --------------------------------------------------------
    subject, message = build_subject_and_message(run_date, curr_alloc, cash_alloc, curr_vol)
    (OUT_DIR / "subject.txt").write_text(subject, encoding="utf-8")
    (OUT_DIR / "message.txt").write_text(message, encoding="utf-8")
    log(f"Wrote subject: {subject}")

    # Build chart data (last 365 days)
    labels_js, alloc_js, vol_js = build_chart_js_from_history(hist)

    # --------------------------------------------------------
    # HTML report (includes chart)
    # --------------------------------------------------------
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TQQQ Volatility Target Report</title>
<style>
body {{
  font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial;
  background: #f6f7f9;
  padding: 24px;
}}
.container {{
  max-width: 980px;
  margin: auto;
}}
h1 {{
  margin: 0 0 6px 0;
  font-size: 40px;
}}
.muted {{ color: #666; font-size: 14px; }}
.badge {{
  display: inline-block;
  padding: 6px 12px;
  border-radius: 999px;
  background: #eef3ff;
  color: #234;
  font-size: 13px;
  margin: 14px 0 18px 0;
}}
.grid {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}}
.card {{
  background: white;
  padding: 18px 18px;
  border-radius: 14px;
  box-shadow: 0 10px 30px rgba(0,0,0,.06);
}}
.kv {{
  display: flex;
  justify-content: space-between;
  padding: 10px 0;
  border-bottom: 1px solid #eee;
  font-size: 16px;
}}
.kv:last-child {{ border-bottom: none; }}
.rule {{
  margin-top: 10px;
  padding: 14px 16px;
  background: #f1f4f8;
  border-radius: 12px;
  font-size: 14px;
  line-height: 1.55;
}}
.chart-wrap {{
  margin-top: 16px;
}}
@media (max-width: 860px) {{
  .grid {{ grid-template-columns: 1fr; }}
  h1 {{ font-size: 32px; }}
}}
</style>
</head>
<body>
  <div class="container">
    <h1>TQQQ Volatility Target</h1>
    <div class="muted">Weekly sizing based on realized volatility</div>
    <div class="badge">Target Vol = {TARGET_VOL_ANNUAL:.0%} • Lookback = {LOOKBACK_DAYS} days • Rounding = {int(ROUND_STEP*100)}%</div>

    <div class="grid">
      <div class="card">
        <h2 style="margin:0 0 8px 0;">This week</h2>
        <div class="kv"><b>Date</b><span>{run_date}</span></div>
        <div class="kv"><b>TQQQ Close</b><span>${last_close:,.2f}</span></div>
        <div class="kv"><b>Realized Vol (20d)</b><span>{curr_vol:.1%}</span></div>
        <div class="kv"><b>Previous Allocation</b><span>{prev_alloc:.0%} TQQQ</span></div>
        <div class="kv"><b>Current Allocation</b><span>{curr_alloc:.0%} TQQQ</span></div>
        <div class="kv"><b>Cash / BIL</b><span>{cash_alloc:.0%}</span></div>
      </div>

      <div class="card">
        <h2 style="margin:0 0 8px 0;">How it works</h2>
        <div class="rule">
          • Compute 20-day realized volatility (annualized) from daily closes.<br>
          • Allocation ≈ TargetVol ÷ RealizedVol (clamped 0–100%).<br>
          • Round to {int(ROUND_STEP*100)}% steps to reduce churn.<br>
          • Run after Friday close; execute Monday after the open.<br>
          • “Cash” sleeve can be BIL/SGOV (or your preferred T-bill ETF).
        </div>
      </div>
    </div>

    <div class="card chart-wrap">
      <h2 style="margin:0 0 10px 0;">Last {HISTORY_DAYS} days trend (allocation + vol)</h2>
      <canvas id="trendChart" height="110"></canvas>
      <div class="muted" style="margin-top:10px;">
        Data comes from logs/history.csv (updated each run). Allocation is rounded to {int(ROUND_STEP*100)}% steps.
      </div>
    </div>

    <div class="muted" style="margin-top:14px;">
      Generated by automation. Data source: yfinance.
    </div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <script>
    const labels = {labels_js};
    const alloc = {alloc_js};
    const vol = {vol_js};

    const ctx = document.getElementById('trendChart').getContext('2d');
    new Chart(ctx, {{
      type: 'line',
      data: {{
        labels: labels,
        datasets: [
          {{
            label: 'TQQQ Allocation (%)',
            data: alloc,
            yAxisID: 'y',
            tension: 0.2
          }},
          {{
            label: 'Realized Vol 20d (%)',
            data: vol,
            yAxisID: 'y1',
            tension: 0.2
          }}
        ]
      }},
      options: {{
        responsive: true,
        interaction: {{ mode: 'index', intersect: false }},
        plugins: {{
          legend: {{ position: 'top' }},
          tooltip: {{
            callbacks: {{
              label: function(context) {{
                return context.dataset.label + ': ' + context.parsed.y.toFixed(1);
              }}
            }}
          }}
        }},
        scales: {{
          y: {{
            position: 'left',
            title: {{ display: true, text: 'Allocation (%)' }},
            suggestedMin: 0,
            suggestedMax: 100
          }},
          y1: {{
            position: 'right',
            title: {{ display: true, text: 'Realized Vol (%)' }},
            grid: {{ drawOnChartArea: false }},
            suggestedMin: 0
          }}
        }}
      }}
    }});
  </script>
</body>
</html>
"""

    # --------------------------------------------------------
    # Write BOTH:
    # 1) Latest file: output/weekly_report.html (overwrites)
    # 2) Snapshot: output/reports/YYYY-MM-DD.html (kept forever)
    # Also write output/report_path.txt so workflow links to snapshot
    # --------------------------------------------------------
    reports_dir = OUT_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Latest (always overwritten)
    HTML_FILE.write_text(html, encoding="utf-8")

    # Snapshot
    snapshot_path = reports_dir / f"{run_date}.html"
    snapshot_path.write_text(html, encoding="utf-8")

    # Used by workflow to link to the right snapshot
    (OUT_DIR / "report_path.txt").write_text(f"reports/{run_date}.html", encoding="utf-8")

    log(f"Wrote report: {HTML_FILE}")
    log(f"Wrote snapshot: {snapshot_path}")
    log("Weekly report completed successfully")


if __name__ == "__main__":
    main()
