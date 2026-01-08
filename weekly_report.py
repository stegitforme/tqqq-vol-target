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
# History retention
# ============================================================
HISTORY_DAYS = 365
HISTORY_CSV = LOG_DIR / "history.csv"

# ============================================================
# Files (repo-relative)
# ============================================================
FETCH_SCRIPT = BASE / "fetch_tqqq.py"
PRICE_FILE = DATA_DIR / "TQQQ.csv"
HTML_FILE = OUT_DIR / "weekly_report.html"
SUBJECT_FILE = OUT_DIR / "subject.txt"
LOG_FILE = LOG_DIR / "friday_run.log"


def utc_now() -> datetime:
    # tz-aware UTC now (best practice)
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    ts = utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
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


def append_history_row(*, run_date_iso: str, close: float, vol20: float, target_vol: float, alloc_tqqq: float) -> pd.DataFrame:
    """
    Appends one row per run to logs/history.csv and trims to last HISTORY_DAYS days.

    IMPORTANT: We store RunDate as a DATE (YYYY-MM-DD) and keep it tz-naive on purpose.
    That avoids tz-aware vs tz-naive comparison errors in GitHub Actions.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    row = {
        "RunDate": run_date_iso,               # "YYYY-MM-DD"
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

    hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)

    # Parse RunDate as tz-naive midnight
    hist["RunDate"] = pd.to_datetime(hist["RunDate"], errors="coerce").dt.normalize()

    # Drop duplicates if rerun same day
    hist = hist.sort_values("RunDate")
    hist = hist.drop_duplicates(subset=["RunDate"], keep="last")

    # Cutoff as tz-naive midnight (UTC date, but tz-naive)
    cutoff = pd.Timestamp(utc_now().date()) - pd.Timedelta(days=HISTORY_DAYS)
    cutoff = pd.to_datetime(cutoff).normalize()

    hist = hist[hist["RunDate"] >= cutoff].copy()

    # Write back as YYYY-MM-DD strings
    hist["RunDate"] = hist["RunDate"].dt.date.astype(str)
    hist.to_csv(HISTORY_CSV, index=False)

    return hist


def build_allocation_table_html(target_vol: float) -> str:
    """
    Small helper table: If realized vol is X, allocation is roughly TargetVol / X.
    """
    vols = [0.20, 0.30, 0.40, 0.50, 0.60, 0.80, 1.00]  # 20%..100%
    rows = []
    for v in vols:
        alloc = target_vol / v
        alloc = max(MIN_ALLOC, min(MAX_ALLOC, alloc))
        alloc = round_step(alloc, ROUND_STEP)
        rows.append((v, alloc, 1.0 - alloc))

    tr = "\n".join(
        f"<tr><td>{v:.0%}</td><td><b>{a:.0%}</b></td><td>{c:.0%}</td></tr>"
        for v, a, c in rows
    )
    return f"""
      <div class="subcard">
        <div class="subhead">Rule-of-thumb examples</div>
        <div class="muted2">If realized vol is higher, your TQQQ allocation drops.</div>
        <table class="tbl">
          <thead><tr><th>Realized vol (20d, ann.)</th><th>TQQQ alloc</th><th>Cash/BIL</th></tr></thead>
          <tbody>
            {tr}
          </tbody>
        </table>
      </div>
    """


def main() -> None:
    log("Starting weekly TQQQ volatility report")

    # Fetch latest prices using the same Python interpreter
    log("Fetching TQQQ price data")
    subprocess.run([sys.executable, str(FETCH_SCRIPT)], check=True)

    if not PRICE_FILE.exists():
        raise FileNotFoundError(
            f"Expected price file not found: {PRICE_FILE}\n"
            f"Repo root BASE={BASE}\n"
            f"Tip: ensure fetch_tqqq.py writes to data/TQQQ.csv in the repo."
        )

    # Load price data
    df = pd.read_csv(PRICE_FILE, parse_dates=["Date"])
    df = df.sort_values("Date").set_index("Date")

    if len(df) < LOOKBACK_DAYS + 5:
        raise RuntimeError(f"Not enough rows in {PRICE_FILE} to compute volatility.")

    close = df["Close"]

    # Compute vol + allocations
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

    # Build subject line (saved for workflow email/pushover)
    subject = f"TQQQ Vol Target | {curr_alloc:.0%} TQQQ / {cash_alloc:.0%} Cash | Vol20={curr_vol:.1%}"
    SUBJECT_FILE.write_text(subject, encoding="utf-8")
    log(f"Wrote subject: {subject}")

    # Append history (365 days)
    run_date = df.index[-1].date().isoformat()
    last_close = float(close.iloc[-1])
    hist = append_history_row(
        run_date_iso=run_date,
        close=last_close,
        vol20=curr_vol,
        target_vol=TARGET_VOL_ANNUAL,
        alloc_tqqq=curr_alloc,
    )

    # Build 90/365 day mini-trend chart using simple inline SVG (no JS libs)
    # Show last 365 days (or fewer if new)
    hist2 = hist.copy()
    hist2["RunDate"] = pd.to_datetime(hist2["RunDate"])
    hist2 = hist2.sort_values("RunDate")
    # columns: AllocTQQQ, RealizedVol20d
    alloc_series = hist2["AllocTQQQ"].astype(float).fillna(0.0).to_list()
    vol_series = hist2["RealizedVol20d"].astype(float).fillna(0.0).to_list()

    def sparkline(values, height=56, width=640, pad=6):
        if not values:
            return ""
        mn, mx = min(values), max(values)
        if mx - mn < 1e-12:
            mx = mn + 1e-12
        pts = []
        n = len(values)
        for i, val in enumerate(values):
            x = pad + (width - 2 * pad) * (i / (n - 1 if n > 1 else 1))
            y = pad + (height - 2 * pad) * (1 - (val - mn) / (mx - mn))
            pts.append(f"{x:.1f},{y:.1f}")
        poly = " ".join(pts)
        return f"""
          <svg viewBox="0 0 {width} {height}" width="100%" height="{height}" class="spark">
            <polyline fill="none" stroke="currentColor" stroke-width="2" points="{poly}"/>
          </svg>
        """

    alloc_svg = sparkline(alloc_series)
    vol_svg = sparkline(vol_series)

    # HTML report
    examples_html = build_allocation_table_html(TARGET_VOL_ANNUAL)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>TQQQ Volatility Target Report</title>
<style>
  :root {{
    --bg: #f6f7f9;
    --card: #ffffff;
    --text: #111827;
    --muted: #6b7280;
    --line: #e5e7eb;
    --pill: #eef2ff;
  }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial;
    background: var(--bg);
    color: var(--text);
    padding: 38px 18px;
  }}
  .card {{
    background: var(--card);
    padding: 26px;
    border-radius: 14px;
    max-width: 860px;
    margin: auto;
    box-shadow: 0 10px 30px rgba(0,0,0,.08);
  }}
  h1 {{ margin: 0 0 6px 0; font-size: 26px; }}
  .muted {{ color: var(--muted); font-size: 14px; }}
  .pill {{
    display: inline-block;
    padding: 6px 12px;
    border-radius: 999px;
    background: var(--pill);
    color: #1f2937;
    font-size: 13px;
    margin-top: 12px;
  }}
  .grid {{
    margin-top: 18px;
    display: grid;
    grid-template-columns: 1fr;
    gap: 14px;
  }}
  @media (min-width: 860px) {{
    .grid {{ grid-template-columns: 1fr 1fr; }}
  }}
  .kv {{
    display: flex;
    justify-content: space-between;
    padding: 9px 0;
    border-bottom: 1px solid var(--line);
    font-size: 15px;
  }}
  .kv:last-child {{ border-bottom: none; }}
  .subcard {{
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 14px 14px 10px 14px;
    background: #fbfbfc;
  }}
  .subhead {{ font-weight: 700; margin-bottom: 6px; }}
  .muted2 {{ color: var(--muted); font-size: 13px; margin-bottom: 10px; }}
  .rule {{
    margin-top: 14px;
    padding: 14px 16px;
    background: #f3f4f6;
    border-radius: 12px;
    font-size: 14px;
    line-height: 1.55;
  }}
  .tbl {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }}
  .tbl th, .tbl td {{
    text-align: left;
    padding: 8px 8px;
    border-bottom: 1px solid var(--line);
  }}
  .tbl th {{ color: #374151; font-weight: 700; }}
  .spark {{ color: #111827; }}
  .sparkwrap {{ margin-top: 6px; }}
</style>
</head>
<body>
  <div class="card">
    <h1>TQQQ Volatility Target</h1>
    <div class="muted">Weekly sizing based on 20-day realized volatility (annualized)</div>
    <div class="pill">Target Vol = {TARGET_VOL_ANNUAL:.0%} • Lookback = {LOOKBACK_DAYS} days • Rounding = {int(ROUND_STEP*100)}%</div>

    <div style="height:16px"></div>

    <div class="grid">
      <div class="subcard">
        <div class="subhead">This week</div>
        <div class="kv"><b>Date</b><span>{run_date}</span></div>
        <div class="kv"><b>TQQQ Close</b><span>${last_close:,.2f}</span></div>
        <div class="kv"><b>Realized Vol (20d)</b><span>{curr_vol:.1%}</span></div>
        <div class="kv"><b>Previous Allocation</b><span>{prev_alloc:.0%} TQQQ</span></div>
        <div class="kv"><b>Current Allocation</b><span><b>{curr_alloc:.0%} TQQQ</b></span></div>
        <div class="kv"><b>Cash / BIL</b><span>{cash_alloc:.0%}</span></div>
      </div>

      <div class="subcard">
        <div class="subhead">Trend (last {HISTORY_DAYS} days)</div>
        <div class="muted2">Allocation and volatility history from automation runs.</div>

        <div class="muted2"><b>% in TQQQ</b></div>
        <div class="sparkwrap">{alloc_svg}</div>

        <div style="height:10px"></div>

        <div class="muted2"><b>Realized Vol (20d)</b></div>
        <div class="sparkwrap">{vol_svg}</div>
      </div>
    </div>

    {examples_html}

    <div class="rule">
      <b>How it works (reminder)</b><br>
      • Compute 20-day realized volatility (annualized) from daily closes.<br>
      • Allocation ≈ TargetVol ÷ RealizedVol (clamped 0–100%).<br>
      • Rounded to {int(ROUND_STEP*100)}% steps to reduce churn.<br>
      • Run after Friday close; execute Monday after open.
    </div>

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
