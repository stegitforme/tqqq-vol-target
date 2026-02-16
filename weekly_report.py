# weekly_report.py
# =========================
# TQQQ Volatility Target
# =========================

from __future__ import annotations

import json
import os
import pandas as pd
from pathlib import Path
from string import Template

# -------------------------
# Config
# -------------------------
TARGET_VOL = 0.20
LOOKBACK_DAYS = 20
ROUND_STEP = 0.05

HISTORY_PATH = "logs/history.csv"
HISTORY_OFFICIAL_PATH = "logs/history_official.csv"

OUTPUT_DIR = Path("output")
REPORTS_DIR = OUTPUT_DIR / "reports"

# Your GitHub Pages base (update if repo/user changes)
PAGES_BASE_URL = "https://stegitforme.github.io/tqqq-vol-target"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# -------------------------
# Helpers
# -------------------------
def load_history_clean(path: str) -> pd.DataFrame:
    """
    Load logs/history.csv:
      - normalize RunDate to date
      - sort
      - keep LAST row per date (fixes duplicate runs in same day)
    """
    hist = pd.read_csv(path, parse_dates=["RunDate"])
    hist["RunDate"] = pd.to_datetime(hist["RunDate"]).dt.normalize()
    hist = hist.sort_values("RunDate")
    hist = hist.drop_duplicates(subset=["RunDate"], keep="last").reset_index(drop=True)
    return hist


def parse_asof_date() -> pd.Timestamp | None:
    raw = (os.environ.get("ASOF_DATE") or "").strip()
    if not raw:
        return None
    try:
        # Normalize to midnight (date-only)
        return pd.to_datetime(raw).normalize()
    except Exception as e:
        raise RuntimeError(f"Invalid ASOF_DATE='{raw}'. Must be YYYY-MM-DD. Error: {e}")


def pick_row_asof(history: pd.DataFrame, asof: pd.Timestamp | None) -> pd.Series:
    """
    If ASOF_DATE is provided:
      - require data up to that date
      - pick the last row with RunDate <= ASOF_DATE
      - BUT if the picked date != ASOF_DATE, fail loudly (prevents silently using an older date).
    If ASOF_DATE is not provided:
      - use the most recent row.
    """
    if history.empty:
        raise RuntimeError("logs/history.csv is empty")

    if asof is None:
        return history.iloc[-1]

    sub = history[history["RunDate"] <= asof]
    if sub.empty:
        max_dt = history["RunDate"].max()
        raise RuntimeError(
            f"ASOF_DATE={asof.date()} requested but history has no rows <= that date. "
            f"Latest available is {max_dt.date()}."
        )

    picked = sub.iloc[-1]
    picked_date = picked["RunDate"]

    # STRICT: don’t silently use an older date if you asked for a specific date
    if picked_date != asof:
        raise RuntimeError(
            f"ASOF_DATE={asof.date()} requested but the latest row <= ASOF is {picked_date.date()}. "
            f"Your data fetch didn’t include {asof.date()} yet."
        )

    return picked


def load_official_prev_alloc(curr_date: pd.Timestamp) -> float | None:
    """
    For 'Previous Allocation (official)', we want the prior OFFICIAL run.
    Use logs/history_official.csv if it exists.
    """
    if not Path(HISTORY_OFFICIAL_PATH).exists():
        return None

    off = pd.read_csv(HISTORY_OFFICIAL_PATH, parse_dates=["RunDate"])
    off["RunDate"] = pd.to_datetime(off["RunDate"]).dt.normalize()
    off = off.sort_values("RunDate").drop_duplicates(subset=["RunDate"], keep="last").reset_index(drop=True)

    prev_rows = off[off["RunDate"] < curr_date]
    if prev_rows.empty:
        return None
    return float(prev_rows.iloc[-1]["AllocTQQQ"])


# -------------------------
# Inputs from workflow env
# -------------------------
MODE = (os.environ.get("MODE") or "").strip().lower()  # debug / official
ASOF_DATE = parse_asof_date()

# -------------------------
# Load history + select as-of row
# -------------------------
history = load_history_clean(HISTORY_PATH)
latest = pick_row_asof(history, ASOF_DATE)

curr_date = pd.to_datetime(latest["RunDate"]).normalize()

curr_alloc = float(latest["AllocTQQQ"])
curr_cash  = float(latest["AllocCash"])

# Previous allocation logic:
# - In OFFICIAL mode: use last official allocation from history_official.csv (if present)
# - Otherwise: use last row strictly before current date from history.csv
prev_alloc = None
if MODE == "official":
    prev_alloc = load_official_prev_alloc(curr_date)

if prev_alloc is None:
    prev_rows = history[history["RunDate"] < curr_date]
    if len(prev_rows) > 0:
        prev_alloc = float(prev_rows.iloc[-1]["AllocTQQQ"])
    else:
        prev_alloc = curr_alloc  # fallback for very first row

prev_alloc_pct = int(round(prev_alloc * 100))
curr_alloc_pct = int(round(curr_alloc * 100))
curr_cash_pct  = int(round(curr_cash * 100))

close_price = float(latest["Close"])
realized_vol_pct = float(latest["RealizedVol20d"]) * 100

run_date_str = curr_date.strftime("%Y-%m-%d")

# -------------------------
# Chart data (last 365 points)
# -------------------------
chart_df = history.tail(365).copy()
chart_dates = chart_df["RunDate"].dt.strftime("%Y-%m-%d").tolist()
chart_alloc = (chart_df["AllocTQQQ"] * 100).round(2).tolist()
chart_vol   = (chart_df["RealizedVol20d"] * 100).round(2).tolist()

chart_dates_js = json.dumps(chart_dates)
chart_alloc_js = json.dumps(chart_alloc)
chart_vol_js   = json.dumps(chart_vol)

# -------------------------
# Report paths + URLs (BASED ON ASOF/SELECTED DATE)
# -------------------------
report_rel_path = f"reports/{run_date_str}.html"
report_file = REPORTS_DIR / f"{run_date_str}.html"
latest_file = OUTPUT_DIR / "weekly_report.html"

report_url = f"{PAGES_BASE_URL}/{report_rel_path}"

# Save helper files for workflow notifications
(OUTPUT_DIR / "latest_report_path.txt").write_text(report_rel_path, encoding="utf-8")
(OUTPUT_DIR / "latest_report_url.txt").write_text(report_url, encoding="utf-8")

# Subject + message for Pushover/email
subject = f"TQQQ Vol Target | {curr_alloc_pct}% TQQQ / {curr_cash_pct}% Cash | Vol20={realized_vol_pct:.1f}%"
message = (
    f"{subject}\n"
    f"Date={run_date_str}\n"
    f"Mode={MODE or 'unknown'}\n"
    f"Report generated.\n"
)

(OUTPUT_DIR / "subject.txt").write_text(subject, encoding="utf-8")
(OUTPUT_DIR / "message.txt").write_text(message, encoding="utf-8")

# -------------------------
# HTML (Template to avoid f-string brace issues)
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
      <div style="margin-top:10px; color:#777;">Data comes from logs/history.csv (full log). “Previous Allocation (official)” comes from logs/history_official.csv.</div>
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
    MODE=(MODE or "unknown"),
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

# Write dated + latest
report_file.write_text(html, encoding="utf-8")
latest_file.write_text(html, encoding="utf-8")

print(f"✅ Wrote dated report: {report_file}")
print(f"✅ Wrote latest report: {latest_file}")
print(f"✅ Latest report URL: {report_url}")
