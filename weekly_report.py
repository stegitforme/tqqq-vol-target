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
OFFICIAL_HISTORY_PATH = "logs/history_official.csv"

OUTPUT_DIR = Path("output")
REPORTS_DIR = OUTPUT_DIR / "reports"

# Your GitHub Pages base (update if repo/user changes)
PAGES_BASE_URL = "https://stegitforme.github.io/tqqq-vol-target"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
Path("logs").mkdir(parents=True, exist_ok=True)

RUN_MODE = os.environ.get("RUN_MODE", "debug").lower().strip()  # "official" or "debug"


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
    p = Path(path)
    if not p.exists():
        return pd.DataFrame()

    hist = pd.read_csv(p, parse_dates=["RunDate"])
    if hist.empty:
        return hist

    hist["RunDate"] = pd.to_datetime(hist["RunDate"]).dt.normalize()
    hist = hist.sort_values("RunDate")
    hist = hist.drop_duplicates(subset=["RunDate"], keep="last").reset_index(drop=True)
    return hist


def ensure_official_history_synced(full_hist: pd.DataFrame, official_path: str) -> pd.DataFrame:
    """
    Ensure logs/history_official.csv exists and is a subset of full history.
    We treat an "official" run as one where RUN_MODE == "official" for THAT run.
    This function doesn't guess Fridays; it just loads what exists.
    """
    p = Path(official_path)
    if not p.exists():
        # Create empty official file with correct columns
        cols = ["RunDate", "Close", "RealizedVol20d", "TargetVol", "AllocTQQQ", "AllocCash"]
        pd.DataFrame(columns=cols).to_csv(p, index=False)

    off = load_history_clean(official_path)
    return off


def append_official_row(latest_row: pd.Series, official_path: str) -> None:
    """
    Append the latest row to logs/history_official.csv (official-only),
    keeping one row per date.
    """
    p = Path(official_path)

    cols = ["RunDate", "Close", "RealizedVol20d", "TargetVol", "AllocTQQQ", "AllocCash"]
    row = {c: latest_row[c] for c in cols}

    # Load existing, append, de-dupe by date
    off = load_history_clean(official_path)
    new = pd.DataFrame([row])
    new["RunDate"] = pd.to_datetime(new["RunDate"]).dt.normalize()

    if off is None or off.empty:
        out = new
    else:
        out = pd.concat([off, new], ignore_index=True)
        out["RunDate"] = pd.to_datetime(out["RunDate"]).dt.normalize()
        out = out.sort_values("RunDate")
        out = out.drop_duplicates(subset=["RunDate"], keep="last").reset_index(drop=True)

    out.to_csv(p, index=False)


# -------------------------
# Load history (full)
# -------------------------
history = load_history_clean(HISTORY_PATH)
if history is None or history.empty:
    raise RuntimeError("logs/history.csv is empty")

latest = history.iloc[-1]
curr_date = pd.to_datetime(latest["RunDate"]).normalize()

# -------------------------
# Official history (for “Previous Allocation”)
# -------------------------
official_hist = ensure_official_history_synced(history, OFFICIAL_HISTORY_PATH)

# If this run is OFFICIAL, append to official history NOW (so Friday shows continuity next week)
if RUN_MODE == "official":
    append_official_row(latest, OFFICIAL_HISTORY_PATH)
    official_hist = load_history_clean(OFFICIAL_HISTORY_PATH)

# Previous allocation should come from last OFFICIAL row strictly before current date
prev_alloc = None
if official_hist is not None and not official_hist.empty:
    prev_off = official_hist[official_hist["RunDate"] < curr_date]
    if len(prev_off) > 0:
        prev_alloc = float(prev_off.iloc[-1]["AllocTQQQ"])

# Fallback if no official history yet (first week)
if prev_alloc is None:
    prev_alloc = float(latest["AllocTQQQ"])

# Current allocation always from latest full history row
curr_alloc = float(latest["AllocTQQQ"])
curr_cash = float(latest["AllocCash"])

prev_alloc_pct = int(round(prev_alloc * 100))
curr_alloc_pct = int(round(curr_alloc * 100))
curr_cash_pct = int(round(curr_cash * 100))

close_price = float(latest["Close"])
realized_vol_pct = float(latest["RealizedVol20d"]) * 100

run_date_str = curr_date.strftime("%Y-%m-%d")

# -------------------------
# Chart data (last 365 points from FULL history)
# -------------------------
chart_df = history.tail(365).copy()
chart_dates = chart_df["RunDate"].dt.strftime("%Y-%m-%d").tolist()
chart_alloc = (chart_df["AllocTQQQ"] * 100).round(2).tolist()
chart_vol = (chart_df["RealizedVol20d"] * 100).round(2).tolist()

chart_dates_js = json.dumps(chart_dates)
chart_alloc_js = json.dumps(chart_alloc)
chart_vol_js = json.dumps(chart_vol)

# -------------------------
# Report paths + URLs
# -------------------------
report_rel_path = f"reports/{run_date_str}.html"
report_file = REPORTS_DIR / f"{run_date_str}.html"
latest_file = OUTPUT_DIR / "weekly_report.html"

report_url = f"{PAGES_BASE_URL}/{report_rel_path}"

# Save helper files for workflow notifications
(OUTPUT_DIR / "latest_report_path.txt").write_text(report_rel_path, encoding="utf-8")
(OUTPUT_DIR / "latest_report_url.txt").write_text(report_url, encoding="utf-8")

# Subject + message
subject = f"TQQQ Vol Target | {curr_alloc_pct}% TQQQ / {curr_cash_pct}% Cash | Vol20={realized_vol_pct:.1f}%"
message = (
    f"{subject}\n"
    f"Date={run_date_str}\n"
    f"Mode={RUN_MODE}\n"
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
      <div style="margin-top:10px; color:#777;">Data comes from logs/history.csv (full log). “Previous Allocation” comes from logs/history_official.csv.</div>
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
        { label: "TQQQ Allocation (%)", data: alloc, borderColor: "#4f83ff", tension: 0.25, yAxisID: "y" },
        { label: "Realized Vol 20d (%)", data: vol, borderColor: "#ff6b81", tension: 0.25, yAxisID: "y1" }
      ]
    },
    options: {
      responsive: true,
      interaction: { mode: "index", intersect: false },
      scales: {
        y: { position: "left", min: 0, max: 100, title: { display: true, text: "Allocation (%)" } },
        y1: { position: "right", min: 0, max: 60, grid: { drawOnChartArea: false }, title: { display: true, text: "Realized Vol (%)" } }
      }
    }
  });
</script>

</body>
</html>
""")

html = html_tpl.substitute(
    MODE=RUN_MODE,
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

print(f"✅ Mode: {RUN_MODE}")
print(f"✅ Wrote dated report: {report_file}")
print(f"✅ Wrote latest report: {latest_file}")
print(f"✅ Latest report URL: {report_url}")
print(f"✅ Previous Allocation (official): {prev_alloc_pct}%")
