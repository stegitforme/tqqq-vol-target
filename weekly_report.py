import math
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone

# -----------------------------
# Config
# -----------------------------
TARGET_VOL = 0.20          # 20%
LOOKBACK_DAYS = 20         # realized vol lookback
ROUND_STEP = 0.05          # 5% steps
MAX_LOOKBACK_ROWS = 365    # chart history

# -----------------------------
# Paths
# -----------------------------
BASE = Path(".")
DATA_PATH = BASE / "data" / "TQQQ.csv"
LOG_DIR = BASE / "logs"
HISTORY_PATH = LOG_DIR / "history.csv"

OUT_DIR = BASE / "output"
REPORTS_DIR = OUT_DIR / "reports"

OUT_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

WEEKLY_REPORT_PATH = OUT_DIR / "weekly_report.html"
SUBJECT_PATH = OUT_DIR / "subject.txt"
MESSAGE_PATH = OUT_DIR / "message.txt"


def ts(msg: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {msg}")


def round_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return round(x / step) * step


def compute_realized_vol(close: pd.Series, lookback_days: int) -> float:
    # daily log returns
    rets = close.pct_change().dropna()
    if len(rets) < lookback_days:
        raise ValueError(f"Not enough data to compute vol({lookback_days}). Have {len(rets)} returns.")
    window = rets.tail(lookback_days)
    # annualize (252 trading days)
    vol = window.std(ddof=1) * math.sqrt(252)
    return float(vol)


def load_history() -> pd.DataFrame:
    if not HISTORY_PATH.exists():
        return pd.DataFrame(columns=["RunDate", "Close", "RealizedVol20d", "TargetVol", "AllocTQQQ", "AllocCash"])

    hist = pd.read_csv(HISTORY_PATH)
    # Ensure types
    hist["RunDate"] = pd.to_datetime(hist["RunDate"], errors="coerce")
    for c in ["Close", "RealizedVol20d", "TargetVol", "AllocTQQQ", "AllocCash"]:
        hist[c] = pd.to_numeric(hist[c], errors="coerce")
    hist = hist.dropna(subset=["RunDate"]).sort_values("RunDate").reset_index(drop=True)
    return hist


def write_html(report_path: Path, date_str: str, close: float, vol20: float,
               prev_alloc: float, curr_alloc: float, hist_tail: pd.DataFrame) -> None:
    # Prepare chart arrays
    hist_tail = hist_tail.tail(MAX_LOOKBACK_ROWS).copy()
    hist_tail["RunDateStr"] = hist_tail["RunDate"].dt.strftime("%Y-%m-%d")

    labels = hist_tail["RunDateStr"].tolist()
    allocs = (hist_tail["AllocTQQQ"] * 100.0).round(2).tolist()
    vols = (hist_tail["RealizedVol20d"] * 100.0).round(2).tolist()

    # Basic HTML + Chart.js (no external build step)
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>TQQQ Volatility Target</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: #f4f6f8;
      margin: 0;
      padding: 28px;
      color: #111;
    }}
    h1 {{
      margin: 0 0 6px 0;
      font-size: 42px;
      letter-spacing: -0.5px;
    }}
    .sub {{
      color: #667085;
      margin-bottom: 16px;
      font-size: 16px;
    }}
    .pill {{
      display: inline-block;
      background: #e8f0ff;
      color: #1f4fbf;
      padding: 10px 14px;
      border-radius: 999px;
      font-weight: 600;
      margin-bottom: 18px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 18px;
      margin-bottom: 18px;
    }}
    .card {{
      background: #fff;
      border-radius: 18px;
      padding: 18px 18px;
      box-shadow: 0 6px 18px rgba(16, 24, 40, 0.08);
    }}
    .card h2 {{
      margin: 0 0 10px 0;
      font-size: 26px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 18px;
    }}
    td {{
      padding: 10px 0;
      border-bottom: 1px solid #eef2f7;
    }}
    td:first-child {{
      color: #344054;
      font-weight: 600;
    }}
    td:last-child {{
      text-align: right;
      font-weight: 700;
    }}
    .how ul {{
      margin: 10px 0 0 0;
      padding-left: 18px;
      color: #344054;
      font-size: 18px;
      line-height: 1.5;
    }}
    .chart-card {{
      background: #fff;
      border-radius: 18px;
      padding: 18px;
      box-shadow: 0 6px 18px rgba(16, 24, 40, 0.08);
    }}
    .footer {{
      color: #667085;
      margin-top: 14px;
      font-size: 14px;
    }}
  </style>
</head>
<body>
  <h1>TQQQ Volatility Target</h1>
  <div class="sub">Weekly sizing based on realized volatility</div>
  <div class="pill">Target Vol = {int(TARGET_VOL*100)}% • Lookback = {LOOKBACK_DAYS} days • Rounding = {int(ROUND_STEP*100)}%</div>

  <div class="grid">
    <div class="card">
      <h2>This week</h2>
      <table>
        <tr><td>Date</td><td>{date_str}</td></tr>
        <tr><td>TQQQ Close</td><td>${close:.2f}</td></tr>
        <tr><td>Realized Vol (20d)</td><td>{vol20*100:.1f}%</td></tr>
        <tr><td>Previous Allocation</td><td>{prev_alloc*100:.0f}% TQQQ</td></tr>
        <tr><td>Current Allocation</td><td>{curr_alloc*100:.0f}% TQQQ</td></tr>
        <tr><td>Cash / BIL</td><td>{(1-curr_alloc)*100:.0f}%</td></tr>
      </table>
    </div>

    <div class="card how">
      <h2>How it works</h2>
      <ul>
        <li>Compute 20-day realized volatility (annualized) from daily closes.</li>
        <li>Allocation ≈ TargetVol ÷ RealizedVol (clamped 0–100%).</li>
        <li>Round to {int(ROUND_STEP*100)}% steps to reduce churn.</li>
        <li>Run after Friday close; execute Monday after the open.</li>
        <li>“Cash” sleeve can be BIL/SGOV (or your preferred T-bill ETF).</li>
      </ul>
    </div>
  </div>

  <div class="chart-card">
    <h2>Last 365 days trend (allocation + vol)</h2>
    <canvas id="chart" height="110"></canvas>
    <div class="footer">Data comes from logs/history.csv (updated each run). Allocation is rounded to {int(ROUND_STEP*100)}% steps.</div>
  </div>

  <div class="footer" style="margin-top:10px;">Generated by automation. Data source: yfinance.</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script>
  const labels = {labels};
  const allocs = {allocs};
  const vols = {vols};

  const ctx = document.getElementById('chart').getContext('2d');

  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: labels,
      datasets: [
        {{
          label: 'TQQQ Allocation (%)',
          data: allocs,
          yAxisID: 'y',
          tension: 0.2
        }},
        {{
          label: 'Realized Vol 20d (%)',
          data: vols,
          yAxisID: 'y1',
          tension: 0.2
        }}
      ]
    }},
    options: {{
      responsive: true,
      interaction: {{ mode: 'index', intersect: false }},
      scales: {{
        y: {{
          beginAtZero: true,
          max: 100,
          title: {{ display: true, text: 'Allocation (%)' }}
        }},
        y1: {{
          beginAtZero: true,
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
    report_path.write_text(html, encoding="utf-8")


def main():
    ts("Starting weekly TQQQ volatility report")

    # Load price data
    ts("Fetching TQQQ price data")
    px = pd.read_csv(DATA_PATH)
    px["Date"] = pd.to_datetime(px["Date"])
    px = px.sort_values("Date").reset_index(drop=True)

    last_date = px["Date"].iloc[-1].date()
    last_close = float(px["Close"].iloc[-1])

    # Load history BEFORE appending (this is the truth for "Previous Allocation")
    hist = load_history()

    # Previous allocation should be the most recent AllocTQQQ in history (if exists),
    # NOT some computed intermediate, and NOT "previous row by date group".
    if len(hist) > 0:
        prev_alloc = float(hist.iloc[-1]["AllocTQQQ"])
    else:
        # If no history yet, treat previous = current (or 0). We’ll set after we compute.
        prev_alloc = None

    # Compute realized vol
    vol20 = compute_realized_vol(px["Close"], LOOKBACK_DAYS)

    # Compute allocation = target / realized, clamped
    raw_alloc = TARGET_VOL / vol20 if vol20 > 0 else 0.0
    raw_alloc = max(0.0, min(1.0, raw_alloc))
    curr_alloc = round_step(raw_alloc, ROUND_STEP)
    curr_alloc = max(0.0, min(1.0, curr_alloc))

    if prev_alloc is None:
        prev_alloc = curr_alloc

    cash_alloc = 1.0 - curr_alloc

    ts(f"Prev: {prev_alloc*100:.0f}%  Curr: {curr_alloc*100:.0f}%  Cash: {cash_alloc*100:.0f}%")

    # Append new history row (use market close date)
    new_row = {
        "RunDate": pd.to_datetime(str(last_date)),
        "Close": last_close,
        "RealizedVol20d": vol20,
        "TargetVol": TARGET_VOL,
        "AllocTQQQ": curr_alloc,
        "AllocCash": cash_alloc,
    }

    hist2 = pd.concat([hist, pd.DataFrame([new_row])], ignore_index=True)
    hist2["RunDate"] = pd.to_datetime(hist2["RunDate"])
    hist2 = hist2.sort_values("RunDate").reset_index(drop=True)

    # Save history
    HISTORY_PATH.write_text(hist2.to_csv(index=False), encoding="utf-8")

    # Write subject + message for notifications
    subject = f"TQQQ Vol Target | {curr_alloc*100:.0f}% TQQQ / {cash_alloc*100:.0f}% Cash | Vol20={vol20*100:.1f}%"
    SUBJECT_PATH.write_text(subject, encoding="utf-8")

    # IMPORTANT: make message plain ascii-safe (strip NBSP etc)
    msg_lines = [
        subject,
        f"Date={last_date}",
        "Report generated.",
    ]
    MESSAGE_PATH.write_text("\n".join(msg_lines), encoding="utf-8")

    # Generate reports
    date_str = str(last_date)
    report_file = REPORTS_DIR / f"{date_str}.html"

    # Use hist2 tail for chart (the graph should match history.csv)
    write_html(report_file, date_str, last_close, vol20, prev_alloc, curr_alloc, hist2)
    write_html(WEEKLY_REPORT_PATH, date_str, last_close, vol20, prev_alloc, curr_alloc, hist2)

    ts(f"Wrote report: {WEEKLY_REPORT_PATH}")
    ts("Weekly report completed successfully")


if __name__ == "__main__":
    main()
