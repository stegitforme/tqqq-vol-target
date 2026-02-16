# weekly_report.py
# =========================
# TQQQ Volatility Target
# =========================

from __future__ import annotations

import json
import os
import math
import pandas as pd
from pathlib import Path
from string import Template

# -------------------------
# Config
# -------------------------
TARGET_VOL = 0.20
LOOKBACK_DAYS = 20
ROUND_STEP = 0.05
TRADING_DAYS = 252

TQQQ_CSV_PATH = "data/TQQQ.csv"
HISTORY_PATH = "logs/history.csv"
HISTORY_OFFICIAL_PATH = "logs/history_official.csv"

OUTPUT_DIR = Path("output")
REPORTS_DIR = OUTPUT_DIR / "reports"

PAGES_BASE_URL = "https://stegitforme.github.io/tqqq-vol-target"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
Path("logs").mkdir(parents=True, exist_ok=True)

# -------------------------
# Helpers
# -------------------------
def parse_asof_date() -> pd.Timestamp | None:
    raw = (os.environ.get("ASOF_DATE") or "").strip()
    if not raw:
        return None
    try:
        return pd.to_datetime(raw).normalize()
    except Exception as e:
        raise RuntimeError(f"Invalid ASOF_DATE='{raw}'. Must be YYYY-MM-DD. Error: {e}")

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def round_to_step(x: float, step: float) -> float:
    # nearest step (e.g., 0.3566 -> 0.35 when step=0.05)
    return round(x / step) * step

def load_tqqq_prices(path: str) -> pd.DataFrame:
    if not Path(path).exists():
        raise RuntimeError(f"Missing {path}. Run fetch_tqqq.py first.")
    df = pd.read_csv(path, parse_dates=["Date"])
    df = df.sort_values("Date").reset_index(drop=True)
    return df

def realized_vol_lookback(prices: pd.Series) -> float:
    # daily log returns
    rets = (prices / prices.shift(1)).apply(lambda x: math.log(x) if pd.notna(x) else x)
    rets = rets.dropna()
    if len(rets) < LOOKBACK_DAYS:
        raise RuntimeError(f"Not enough return observations for {LOOKBACK_DAYS}-day vol. Have {len(rets)}.")
    window = rets.tail(LOOKBACK_DAYS)
    vol_daily = float(window.std(ddof=1))
    vol_ann = vol_daily * math.sqrt(TRADING_DAYS)
    return vol_ann

def upsert_history_row(history_path: str, row: dict) -> pd.DataFrame:
    # Load existing history (if any), then upsert by RunDate
    if Path(history_path).exists():
        hist = pd.read_csv(history_path, parse_dates=["RunDate"])
        hist["RunDate"] = pd.to_datetime(hist["RunDate"]).dt.normalize()
    else:
        hist = pd.DataFrame(columns=["RunDate","Close","RealizedVol20d","TargetVol","AllocTQQQ","AllocCash"])

    new_row = pd.DataFrame([row])
    new_row["RunDate"] = pd.to_datetime(new_row["RunDate"]).dt.normalize()

    # remove existing same date, append new, sort
    hist = hist[hist["RunDate"] != new_row.iloc[0]["RunDate"]]
    hist = pd.concat([hist, new_row], ignore_index=True)
    hist = hist.sort_values("RunDate").reset_index(drop=True)
    hist.to_csv(history_path, index=False)
    return hist

def prev_official_alloc(curr_date: pd.Timestamp) -> float | None:
    p = Path(HISTORY_OFFICIAL_PATH)
    if not p.exists():
        return None
    off = pd.read_csv(p, parse_dates=["RunDate"])
    off["RunDate"] = pd.to_datetime(off["RunDate"]).dt.normalize()
    off = off.sort_values("RunDate").reset_index(drop=True)
    prev = off[off["RunDate"] < curr_date]
    if prev.empty:
        return None
    return float(prev.iloc[-1]["AllocTQQQ"])

# -------------------------
# Env inputs
# -------------------------
MODE = (os.environ.get("MODE") or "debug").strip().lower()   # debug / official
ASOF_DATE = parse_asof_date()

# -------------------------
# Load prices + choose ASOF row
# -------------------------
tqqq = load_tqqq_prices(TQQQ_CSV_PATH)

if ASOF_DATE is None:
    asof_dt = pd.to_datetime(tqqq["Date"].iloc[-1]).normalize()
else:
    asof_dt = ASOF_DATE

# Find exact close for ASOF_DATE
asof_rows = tqqq[pd.to_datetime(tqqq["Date"]).dt.normalize() == asof_dt]
if asof_rows.empty:
    last_dt = pd.to_datetime(tqqq["Date"].iloc[-1]).normalize()
    raise RuntimeError(f"ASOF_DATE={asof_dt.date()} not present in data/TQQQ.csv. Latest available is {last_dt.date()}.")

close_price = float(asof_rows.iloc[-1]["Close"])

# Need enough history up to ASOF for vol calc
tqqq_upto = tqqq[pd.to_datetime(tqqq["Date"]).dt.normalize() <= asof_dt].copy()
tqqq_upto = tqqq_upto.sort_values("Date").reset_index(drop=True)

vol_ann = realized_vol_lookback(tqqq_upto["Close"])
alloc_raw = TARGET_VOL / vol_ann if vol_ann > 0 else 0.0
alloc_raw = clamp(alloc_raw, 0.0, 1.0)
alloc = round_to_step(alloc_raw, ROUND_STEP)
alloc = clamp(alloc, 0.0, 1.0)
cash = 1.0 - alloc

row = {
    "RunDate": asof_dt.strftime("%Y-%m-%d"),
    "Close": close_price,
    "RealizedVol20d": vol_ann,
    "TargetVol": TARGET_VOL,
    "AllocTQQQ": alloc,
    "AllocCash": cash,
}

# -------------------------
# Update logs/history.csv and (if official) logs/history_official.csv
# -------------------------
history = upsert_history_row(HISTORY_PATH, row)

if MODE == "official":
    _ = upsert_history_row(HISTORY_OFFICIAL_PATH, row)

# -------------------------
# Compute “Previous Allocation (official)” for display
# -------------------------
prev_alloc = None
if MODE == "official":
    prev_alloc = prev_official_alloc(asof_dt)

if prev_alloc is None:
    prev_rows = history[pd.to_datetime(history["RunDate"]).dt.normalize() < asof_dt]
    prev_alloc = float(prev_rows.iloc[-1]["AllocTQQQ"]) if not prev_rows.empty else alloc

prev_alloc_pct = int(round(prev_alloc * 100))
curr_alloc_pct = int(round(alloc * 100))
curr_cash_pct  = int(round(cash * 100))
realized_vol_pct = vol_ann * 100

run_date_str = asof_dt.strftime("%Y-%m-%d")

# -------------------------
# Chart data (last 365 points from history)
# -------------------------
hist_dt = history.copy()
hist_dt["RunDate"] = pd.to_datetime(hist_dt["RunDate"]).dt.normalize()

chart_df = hist_dt.tail(365).copy()
chart_dates = chart_df["RunDate"].dt.strftime("%Y-%m-%d").tolist()
chart_alloc = (chart_df["AllocTQQQ"] * 100).round(2).tolist()
chart_vol   = (chart_df["RealizedVol20d"] * 100).round(2).tolist()

chart_dates_js = json.dumps(chart_dates)
chart_alloc_js = json.dumps(chart_alloc)
chart_vol_js   = json.dumps(chart_vol)

# -------------------------
# Report paths + URLs
# -------------------------
report_rel_path = f"reports/{run_date_str}.html"
report_file = REPORTS_DIR / f"{run_date_str}.html"
latest_file = OUTPUT_DIR / "weekly_report.html"

report_url = f"{PAGES_BASE_URL}/{report_rel_path}"

(OUTPUT_DIR / "latest_report_path.txt").write_text(report_rel_path, encoding="utf-8")
(OUTPUT_DIR / "latest_report_url.txt").write_text(report_url, encoding="utf-8")

subject = f"TQQQ Vol Target | {curr_alloc_pct}% TQQQ / {curr_cash_pct}% Cash | Vol20={realized_vol_pct:.1f}%"
message = (
    f"{subject}\n"
    f"Date={run_date_str}\n"
    f"Mode={MODE}\n"
    f"Report generated.\n"
)

(OUTPUT_DIR / "subject.txt").write_text(subject, encoding="utf-8")
(OUTPUT_DIR / "message.txt").write_text(message, encoding="utf-8")

# -------------------------
# HTML
# -------------------------
html_tpl = Template(r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>TQQQ Volatility Target</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:#f7f7f8; margin:0; padding:24px; }
  h1 { margin:0 0 6px 0; font-size: 40px; }
  .sub { color:#666; margin-bottom:18px; }
  .pill { display:inline-block; background:#eef2ff; color:#111; padding:10px 14px; border-radius:999px; font-size:14px; margin:10px 0 18px 0; }
  .grid { display:grid; grid-template-columns: 1fr 1fr; gap:18px; }
  .card { background:white; border-radius:18px; padding:20px; box-shadow: 0 8px 22px rgba(0,0,0,0.05); }
  .card h2 { margin:0 0 10px 0; font-size:28px; }
  table { width:100%; border-collapse:collapse; }
  td { padding:10px 0; border-bottom:1px solid #eee; font-size:18px; }
  td:last-child { text-align:right; font-weight:600; }
  .muted { color:#777; font-weight:500; }
  .big { font-weight:800; }
  .wide { grid-column: 1 / -1; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>

  <h1>TQQQ Volatility Target</h1>
  <div class="sub">Weekly sizing based on realized volatility</div>
  <div class="pill">Target Vol = 20% • Lookback = 20 days • Rounding = 5% • Mode = $MODE</div>

  <div class="grid">
    <div class="card">
      <h2>This week</h2>
      <table>
        <tr><td class="muted">Date</td><td>$RUN_DATE</td></tr>
        <tr><td class="muted">TQQQ Close</td><td>$$CLOSE</td></tr>
        <tr><td class="muted">Realized Vol (20d)</td><td>$VOL%</td></tr>
        <tr><td class="big">Previous Allocation (official)</td><td class="big">$PREV% TQQQ</td></tr>
        <tr><td class="big">Current Allocation</td><td class="big">$CURR% TQQQ</td></tr>
        <tr><td class="muted">Cash / BIL</td><td>$CASH%</td></tr>
      </table>
    </div>

    <div class="card">
      <h2>How it works</h2>
      <div style="font-size:18px; line-height:1.5;">
        <ul>
          <li>Compute 20-day realized volatility (annualized) from daily closes.</li>
          <li>Allocation ≈ TargetVol ÷ RealizedVol (clamped 0–100%).</li>
          <li>Round to 5% steps to reduce churn.</li>
          <li>Run after Friday close; execute Monday after the open.</li>
          <li>"Cash" sleeve can be BIL/SGOV (or your preferred T-bill ETF).</li>
        </ul>
      </div>
    </div>

    <div class="card wide">
      <h2>Last 365 days trend (allocation + vol)</h2>
      <canvas id="chart"></canvas>
      <div style="margin-top:10px; color:#777;">
        Data comes from logs/history.csv (full log). “Previous Allocation (official)” comes from logs/history_official.csv.
      </div>
    </div>
  </div>

<script>
  const labels = $CHART_DATES;
  const alloc = $CHART_ALLOC;
  const vol   = $CHART_VOL;

  const ctx = document.getElementById("chart");
  new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "TQQQ Allocation (%)", data: alloc, tension: 0.25, yAxisID: "y" },
        { label: "Realized Vol 20d (%)", data: vol, tension: 0.25, yAxisID: "y1" }
      ]
    },
    options: {
      responsive: true,
      interaction: { mode: "index", intersect: false },
      scales: {
        y:  { position: "left",  min: 0, max: 100, title: { display: true, text: "Allocation (%)" } },
        y1: { position: "right", min: 0, max:  60, grid: { drawOnChartArea: false }, title: { display: true, text: "Realized Vol (%)" } }
      }
    }
  });
</script>

</body>
</html>
""")

html = html_tpl.substitute(
    MODE=MODE,
    RUN_DATE=run_date_str,
    CLOSE=f"{close_price:.2f}",
    VOL=f"{realized_vol_pct:.1f}",
    PREV=str(prev_alloc_pct),
    CURR=str(curr_alloc_pct),
    CASH=str(curr_cash_pct),
    CHART_DATES=chart_dates_js,
    CHART_ALLOC=chart_alloc_js,
    CHART_VOL=chart_vol_js,
)

report_file.write_text(html, encoding="utf-8")
latest_file.write_text(html, encoding="utf-8")

print(f"✅ ASOF_DATE used: {run_date_str}")
print(f"✅ Wrote dated report: {report_file}")
print(f"✅ Wrote latest report: {latest_file}")
print(f"✅ Latest report URL: {report_url}")
print(f"✅ Alloc={curr_alloc_pct}%  Cash={curr_cash_pct}%  Vol20={realized_vol_pct:.2f}%")
