# weekly_report.py
# =========================
# TQQQ Volatility Target
# =========================

from __future__ import annotations

import os
import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path

# -------------------------
# Config
# -------------------------
TARGET_VOL = 0.20
LOOKBACK_DAYS = 20
ROUND_STEP = 0.05

HISTORY_PATH = "logs/history.csv"
OUTPUT_DIR = Path("output")
REPORTS_DIR = OUTPUT_DIR / "reports"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# -------------------------
# Helpers
# -------------------------
def round_to_step(x: float, step: float) -> float:
    return round(x / step) * step


def load_history_clean(path: str) -> pd.DataFrame:
    """
    Load history.csv safely:
      - normalize RunDate
      - sort
      - keep LAST row per date (fixes duplicate runs)
    """
    hist = pd.read_csv(path, parse_dates=["RunDate"])
    hist["RunDate"] = pd.to_datetime(hist["RunDate"]).dt.normalize()
    hist = hist.sort_values("RunDate")
    hist = hist.drop_duplicates(subset=["RunDate"], keep="last").reset_index(drop=True)
    return hist


def compute_prev_curr_alloc(history_path: str):
    """
    Returns:
      prev_alloc (float 0–1)
      curr_alloc (float 0–1)
      curr_cash (float 0–1)
      history_clean (DataFrame)
    """
    hist = load_history_clean(history_path)

    if len(hist) == 0:
        raise RuntimeError("history.csv is empty")

    curr = hist.iloc[-1]
    curr_date = curr["RunDate"]

    curr_alloc = float(curr["AllocTQQQ"])
    curr_cash = float(curr["AllocCash"])

    prev_rows = hist[hist["RunDate"] < curr_date]
    if len(prev_rows) > 0:
        prev_alloc = float(prev_rows.iloc[-1]["AllocTQQQ"])
    else:
        prev_alloc = curr_alloc  # first run fallback

    return prev_alloc, curr_alloc, curr_cash, hist


# -------------------------
# Load history + allocations
# -------------------------
prev_alloc, curr_alloc, cash_alloc, history = compute_prev_curr_alloc(HISTORY_PATH)

prev_alloc_pct = int(round(prev_alloc * 100))
curr_alloc_pct = int(round(curr_alloc * 100))
cash_alloc_pct = int(round(cash_alloc * 100))

latest = history.iloc[-1]

run_date = latest["RunDate"].date()
close_price = float(latest["Close"])
realized_vol = float(latest["RealizedVol20d"]) * 100

# -------------------------
# Chart data (last 365 days)
# -------------------------
chart_df = history.copy()
chart_df = chart_df.tail(365)

chart_dates = chart_df["RunDate"].dt.strftime("%Y-%m-%d").tolist()
chart_alloc = (chart_df["AllocTQQQ"] * 100).tolist()
chart_vol = (chart_df["RealizedVol20d"] * 100).tolist()

# -------------------------
# HTML Report
# -------------------------
html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>TQQQ Volatility Target</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #f7f7f8;
  padding: 24px;
}}
.card {{
  background: white;
  border-radius: 14px;
  padding: 20px;
  margin-bottom: 20px;
}}
h1 {{ margin-bottom: 4px; }}
small {{ color: #666; }}
table {{
  width: 100%;
  border-collapse: collapse;
}}
td {{
  padding: 6px 0;
}}
</style>
</head>
<body>

<h1>TQQQ Volatility Target</h1>
<small>Target Vol = 20% • Lookback = 20 days • Rounding = 5%</small>

<div class="card">
<h2>This week</h2>
<table>
<tr><td>Date</td><td>{run_date}</td></tr>
<tr><td>TQQQ Close</td><td>${close_price:.2f}</td></tr>
<tr><td>Realized Vol (20d)</td><td>{realized_vol:.1f}%</td></tr>
<tr><td><b>Previous Allocation</b></td><td><b>{prev_alloc_pct}% TQQQ</b></td></tr>
<tr><td><b>Current Allocation</b></td><td><b>{curr_alloc_pct}% TQQQ</b></td></tr>
<tr><td>Cash / BIL</td><td>{cash_alloc_pct}%</td></tr>
</table>
</div>

<div class="card">
<h2>Last 365 days trend</h2>
<canvas id="chart"></canvas>
</div>

<script>
const ctx = document.getElementById("chart");
new Chart(ctx, {{
  type: "line",
  data: {{
    labels: {chart_dates},
    datasets: [
      {{
        label: "TQQQ Allocation (%)",
        data: {chart_alloc},
        borderColor: "#4f83ff",
        yAxisID: "y",
        tension: 0.3
      }},
      {{
        label: "Realized Vol 20d (%)",
        data: {chart_vol},
        borderColor: "#ff6b81",
        yAxisID: "y1",
        tension: 0.3
      }}
    ]
  }},
  options: {{
    responsive: true,
    scales: {{
      y: {{
        position: "left",
        min: 0,
        max: 100
      }},
      y1: {{
        position: "right",
        min: 0,
        max: 60,
        grid: {{ drawOnChartArea: false }}
      }}
    }}
  }}
});
</script>

</body>
</html>
"""

# -------------------------
# Write files
# -------------------------
dated_report = REPORTS_DIR / f"{run_date}.html"
latest_report = OUTPUT_DIR / "weekly_report.html"

dated_report.write_text(html, encoding="utf-8")
latest_report.write_text(html, encoding="utf-8")

print(f"Report written: {dated_report}")
