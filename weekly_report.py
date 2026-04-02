# weekly_report.py
# =========================
# TQQQ 200MA Strategy  —  v3
# Pure 200MA gate: 100% TQQQ above, 100% SGOV below
# Vol metrics retained as informational display only
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
TARGET_VOL    = 0.20        # kept for informational vol display only
LOOKBACK_DAYS = 20          # days for realized vol window
ROUND_STEP    = 0.05
TRADING_DAYS  = 252

# MA periods (all measured on QQQ daily closes)
MA_50  = 50
MA_100 = 100
MA_200 = 200

# RSI / MACD kept for informational display
RSI_PERIOD  = 14
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9

TQQQ_CSV_PATH = "data/TQQQ.csv"
QQQ_CSV_PATH  = "data/QQQ.csv"
HISTORY_PATH          = "logs/history.csv"
HISTORY_OFFICIAL_PATH = "logs/history_official.csv"

OUTPUT_DIR  = Path("output")
REPORTS_DIR = OUTPUT_DIR / "reports"

PAGES_BASE_URL = "https://stegitforme.github.io/tqqq-vol-target"

# -------------------------
# Helpers
# -------------------------
def round_to_step(x: float, step: float) -> float:
    return round(round(x / step) * step, 10)

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def parse_asof_date():
    raw = os.environ.get("ASOF_DATE", "").strip()
    if not raw:
        return None
    try:
        return pd.to_datetime(raw).normalize()
    except Exception:
        return None

def load_prices(path: str, ticker: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    if "Date" not in df.columns or "Close" not in df.columns:
        raise ValueError(f"{ticker}: expected Date,Close columns in {path}")
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").reset_index(drop=True)
    return df

def compute_realized_vol(series: pd.Series, window: int = 20) -> float | None:
    s = series.dropna()
    if len(s) < window + 1:
        return None
    recent = s.iloc[-(window + 1):]
    log_rets = [math.log(recent.iloc[i] / recent.iloc[i-1]) for i in range(1, len(recent))]
    mean = sum(log_rets) / len(log_rets)
    variance = sum((r - mean) ** 2 for r in log_rets) / (len(log_rets) - 1)
    return math.sqrt(variance) * math.sqrt(TRADING_DAYS)

def compute_sma(series: pd.Series, period: int) -> float | None:
    s = series.dropna()
    if len(s) < period:
        return None
    return float(s.iloc[-period:].mean())

def compute_rsi(series: pd.Series, period: int = 14) -> float | None:
    s = series.dropna()
    if len(s) < period + 1:
        return None
    deltas = s.diff().dropna()
    gains  = deltas.clip(lower=0).iloc[-period:]
    losses = (-deltas.clip(upper=0)).iloc[-period:]
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))

def compute_macd(series: pd.Series, fast=12, slow=26, signal=9) -> dict:
    s = series.dropna()
    if len(s) < slow + signal:
        return {"macd_line": None, "signal_line": None, "histogram": None, "bullish": None}
    ema_fast    = s.ewm(span=fast,   adjust=False).mean()
    ema_slow    = s.ewm(span=slow,   adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram   = macd_line - signal_line
    bullish = bool(macd_line.iloc[-1] > signal_line.iloc[-1])
    return {
        "macd_line":   round(float(macd_line.iloc[-1]), 4),
        "signal_line": round(float(signal_line.iloc[-1]), 4),
        "histogram":   round(float(histogram.iloc[-1]), 4),
        "bullish":     bullish,
    }

def upsert_history_row(history_path: str, row: dict) -> pd.DataFrame:
    if Path(history_path).exists():
        hist = pd.read_csv(history_path)
    else:
        Path(history_path).parent.mkdir(parents=True, exist_ok=True)
        hist = pd.DataFrame()
    new_row = pd.DataFrame([row])
    hist = pd.concat([hist, new_row], ignore_index=True)
    hist = hist.drop_duplicates(subset=["RunDate"], keep="last")
    hist.to_csv(history_path, index=False)
    return hist

def prev_official_alloc(asof: pd.Timestamp) -> float | None:
    p = Path(HISTORY_OFFICIAL_PATH)
    if not p.exists():
        return None
    h = pd.read_csv(p)
    h["RunDate"] = pd.to_datetime(h["RunDate"])
    past = h[h["RunDate"] < asof].sort_values("RunDate")
    if past.empty:
        return None
    return float(past["AllocTQQQ"].iloc[-1])

# -------------------------
# Env inputs
# -------------------------
MODE      = (os.environ.get("MODE") or "debug").strip().lower()
ASOF_DATE = parse_asof_date()

# -------------------------
# Load TQQQ prices
# -------------------------
tqqq = load_prices(TQQQ_CSV_PATH, "TQQQ")

if ASOF_DATE is None:
    asof_dt = pd.to_datetime(tqqq["Date"].iloc[-1]).normalize()
else:
    asof_dt = ASOF_DATE

asof_rows = tqqq[pd.to_datetime(tqqq["Date"]).dt.normalize() == asof_dt]
if asof_rows.empty:
    last_dt = pd.to_datetime(tqqq["Date"].iloc[-1]).normalize()
    raise RuntimeError(
        f"ASOF_DATE={asof_dt.date()} not in data/TQQQ.csv. Latest: {last_dt.date()}"
    )

tqqq_close = float(asof_rows["Close"].iloc[-1])
tqqq_prices = tqqq[pd.to_datetime(tqqq["Date"]).dt.normalize() <= asof_dt]["Close"]

# -------------------------
# Load QQQ prices
# -------------------------
qqq = load_prices(QQQ_CSV_PATH, "QQQ")
qqq_rows = qqq[pd.to_datetime(qqq["Date"]).dt.normalize() == asof_dt]
if qqq_rows.empty:
    qqq_close = float(qqq["Close"].iloc[-1])
else:
    qqq_close = float(qqq_rows["Close"].iloc[-1])

qqq_prices = qqq[pd.to_datetime(qqq["Date"]).dt.normalize() <= asof_dt]["Close"]

# -------------------------
# Compute indicators
# -------------------------
vol_ann = compute_realized_vol(tqqq_prices, LOOKBACK_DAYS)
if vol_ann is None or vol_ann == 0:
    vol_ann = 0.50  # fallback

# Vol-implied allocation — INFORMATIONAL ONLY, not used for sizing
vol_implied = round_to_step(min(1.0, TARGET_VOL / vol_ann), ROUND_STEP)

ma50  = compute_sma(qqq_prices, MA_50)
ma100 = compute_sma(qqq_prices, MA_100)
ma200 = compute_sma(qqq_prices, MA_200)
rsi   = compute_rsi(qqq_prices, RSI_PERIOD)
macd  = compute_macd(qqq_prices, MACD_FAST, MACD_SLOW, MACD_SIGNAL)

# -------------------------
# 200MA Gate — pure binary
# THIS IS THE ONLY ALLOCATION LOGIC
# -------------------------
above_200ma = (ma200 is not None) and (qqq_close > ma200)

alloc_final = 1.0 if above_200ma else 0.0
cash_final  = 1.0 - alloc_final

run_date_str    = asof_dt.strftime("%Y-%m-%d")
vol_pct         = vol_ann * 100
alloc_final_pct = int(round(alloc_final * 100))
vol_implied_pct = int(round(vol_implied * 100))
cash_pct        = int(round(cash_final * 100))

# -------------------------
# Build history row
# -------------------------
row = {
    "RunDate":        run_date_str,
    "TQQQ_Close":     tqqq_close,
    "QQQ_Close":      qqq_close,
    "RealizedVol20d": vol_ann,
    "VolImpliedAlloc": vol_implied,   # informational
    "QQQ_MA50":       round(ma50,  2) if ma50  is not None else None,
    "QQQ_MA100":      round(ma100, 2) if ma100 is not None else None,
    "QQQ_MA200":      round(ma200, 2) if ma200 is not None else None,
    "QQQ_RSI":        round(rsi,   2) if rsi   is not None else None,
    "MACD_Bullish":   macd["bullish"],
    "MA200_Gate":     above_200ma,
    "AllocTQQQ":      alloc_final,
    "AllocCash":      cash_final,
}

history = upsert_history_row(HISTORY_PATH, row)
if MODE == "official":
    _ = upsert_history_row(HISTORY_OFFICIAL_PATH, row)

# -------------------------
# Previous allocation for display
# -------------------------
prev_alloc = None
if MODE == "official":
    prev_alloc = prev_official_alloc(asof_dt)
if prev_alloc is None:
    prev_rows = history[pd.to_datetime(history["RunDate"]) < asof_dt].sort_values("RunDate")
    if not prev_rows.empty:
        prev_alloc = float(prev_rows["AllocTQQQ"].iloc[-1])

prev_alloc_pct = int(round(prev_alloc * 100)) if prev_alloc is not None else None

# -------------------------
# Determine action label
# -------------------------
if prev_alloc is None:
    action_label = "HOLD"
elif alloc_final > prev_alloc:
    action_label = "BUY / ADD"
elif alloc_final < prev_alloc:
    action_label = "SELL / REDUCE"
else:
    action_label = "HOLD — no change"

# -------------------------
# MA distance display
# -------------------------
def ma_dist(price, ma):
    if ma is None:
        return "N/A"
    pct = (price - ma) / ma * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"

# -------------------------
# Email subject line
# -------------------------
gate_str   = "Above 200MA" if above_200ma else "Below 200MA"
alloc_str  = f"{alloc_final_pct}% TQQQ" if alloc_final_pct > 0 else "0% TQQQ / 100% SGOV"
email_subject = (
    f"TQQQ | 200MA-Only | {alloc_str} | "
    f"Vol20={vol_pct:.1f}% | QQQ {gate_str}"
)

# -------------------------
# Status colors
# -------------------------
gate_color   = "#00C853" if above_200ma else "#FF1744"
gate_label   = "✅ ABOVE — 100% TQQQ" if above_200ma else "🔴 BELOW — 100% SGOV"
action_color = "#00C853" if action_label.startswith("BUY") else ("#FF1744" if action_label.startswith("SELL") else "#FFC107")

# -------------------------
# HTML report
# -------------------------
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
report_date = asof_dt.strftime("%Y-%m-%d")
report_path = REPORTS_DIR / f"{report_date}.html"

ma50_str  = f"{ma50:.2f}"  if ma50  is not None else "N/A"
ma100_str = f"{ma100:.2f}" if ma100 is not None else "N/A"
ma200_str = f"{ma200:.2f}" if ma200 is not None else "N/A"
rsi_str   = f"{rsi:.1f}"   if rsi   is not None else "N/A"
macd_str  = "Bullish ▲" if macd["bullish"] else ("Bearish ▼" if macd["bullish"] is False else "N/A")
macd_color = "#00C853" if macd["bullish"] else ("#FF1744" if macd["bullish"] is False else "#888")

prev_str = f"{prev_alloc_pct}%" if prev_alloc_pct is not None else "—"

html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>TQQQ Report {report_date}</title>
<style>
  body {{ background:#0d0d0d; color:#e0e0e0; font-family:'Courier New',monospace; padding:24px; max-width:680px; margin:auto; }}
  h1 {{ color:#00e5ff; font-size:18px; letter-spacing:2px; border-bottom:1px solid #333; padding-bottom:8px; }}
  .section {{ margin:18px 0; }}
  .label {{ color:#888; font-size:12px; text-transform:uppercase; letter-spacing:1px; }}
  .big {{ font-size:28px; font-weight:bold; }}
  .pill {{ display:inline-block; padding:6px 16px; border-radius:4px; font-size:13px; font-weight:bold; }}
  .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
  .card {{ background:#1a1a1a; border:1px solid #2a2a2a; border-radius:6px; padding:12px; }}
  .card .v {{ font-size:16px; font-weight:bold; color:#e0e0e0; margin-top:4px; }}
  .note {{ font-size:11px; color:#555; margin-top:4px; }}
  .action-box {{ background:#1a1a1a; border:2px solid {action_color}; border-radius:6px; padding:16px; margin:16px 0; }}
  .strategy-note {{ background:#111; border:1px solid #333; border-radius:6px; padding:12px; margin:16px 0; font-size:12px; color:#888; }}
</style>
</head>
<body>

<h1>TQQQ WEEKLY SIGNAL — {report_date}</h1>

<div class="section">
  <div class="label">Strategy</div>
  <div style="font-size:14px; color:#00e5ff; margin-top:4px;">200MA-Only Binary Gate</div>
  <div class="note">QQQ above 200MA → 100% TQQQ &nbsp;|&nbsp; QQQ below 200MA → 100% SGOV</div>
</div>

<div class="section">
  <div class="label">200MA Gate</div>
  <div class="big" style="color:{gate_color}; margin-top:6px;">{gate_label}</div>
  <div style="margin-top:4px; font-size:13px; color:#888;">
    QQQ: ${qqq_close:.2f} &nbsp;|&nbsp; 200MA: {ma200_str} &nbsp;|&nbsp; Distance: {ma_dist(qqq_close, ma200)}
  </div>
</div>

<div class="section">
  <div class="label">This Week's Allocation</div>
  <div class="big" style="color:{'#00C853' if alloc_final_pct > 0 else '#FF1744'}; margin-top:6px;">
    {alloc_final_pct}% TQQQ &nbsp;/&nbsp; {cash_pct}% SGOV
  </div>
  <div style="margin-top:4px; font-size:13px; color:#888;">Previous: {prev_str}</div>
</div>

<div class="action-box">
  <div class="label">What to do Monday</div>
  <div style="font-size:20px; font-weight:bold; color:{action_color}; margin-top:6px;">{action_label}</div>
  <div style="font-size:13px; color:#888; margin-top:6px;">
    Target: {alloc_final_pct}% TQQQ / {cash_pct}% SGOV
  </div>
</div>

<div class="section">
  <div class="label">QQQ Moving Averages</div>
  <div class="grid" style="margin-top:8px;">
    <div class="card">
      <div class="label">50-day MA</div>
      <div class="v" style="color:{'#00C853' if ma50 and qqq_close > ma50 else '#FF1744'}">{ma50_str}</div>
      <div class="note">QQQ {ma_dist(qqq_close, ma50)} from 50MA</div>
    </div>
    <div class="card">
      <div class="label">100-day MA</div>
      <div class="v" style="color:{'#00C853' if ma100 and qqq_close > ma100 else '#FF1744'}">{ma100_str}</div>
      <div class="note">QQQ {ma_dist(qqq_close, ma100)} from 100MA</div>
    </div>
    <div class="card">
      <div class="label">200-day MA ★ GATE</div>
      <div class="v" style="color:{gate_color}">{ma200_str}</div>
      <div class="note">QQQ {ma_dist(qqq_close, ma200)} from 200MA</div>
    </div>
    <div class="card">
      <div class="label">RSI (14)</div>
      <div class="v">{rsi_str}</div>
      <div class="note">{'Oversold (<35)' if rsi and rsi < 35 else 'Overbought (>65)' if rsi and rsi > 65 else 'Neutral'}</div>
    </div>
  </div>
</div>

<div class="section">
  <div class="label">Momentum Signals (informational)</div>
  <div class="grid" style="margin-top:8px;">
    <div class="card">
      <div class="label">MACD</div>
      <div class="v" style="color:{macd_color}">{macd_str}</div>
      <div class="note">Line: {macd['macd_line']} / Signal: {macd['signal_line']}</div>
    </div>
    <div class="card">
      <div class="label">Vol-implied alloc</div>
      <div class="v" style="color:#888">{vol_implied_pct}%</div>
      <div class="note">20d vol: {vol_pct:.1f}% (info only — not used)</div>
    </div>
  </div>
</div>

<div class="strategy-note">
  <strong style="color:#555;">Strategy note:</strong> This report uses the pure 200MA binary gate.
  Vol-implied allocation is shown for reference but does not affect the actual position.
  Backtest (2017–2025): 200MA-only +43% CAGR, $100K → $2.49M vs vol+200MA combo +23% CAGR, $100K → $627K.
</div>

<div style="font-size:11px; color:#333; margin-top:24px; border-top:1px solid #1a1a1a; padding-top:8px;">
  Generated {report_date} | Mode: {MODE.upper()} | TQQQ: ${tqqq_close:.2f} | QQQ: ${qqq_close:.2f}
</div>

</body>
</html>"""

with open(report_path, "w") as f:
    f.write(html)

# -------------------------
# Output summary
# -------------------------
print(f"Subject: {email_subject}")
print(f"Date:    {run_date_str}")
print(f"Mode:    {MODE}")
print()
print(f"200MA Gate:    {'ABOVE ✅' if above_200ma else 'BELOW 🔴'}")
print(f"QQQ:           ${qqq_close:.2f}  |  200MA: {ma200_str}  |  Dist: {ma_dist(qqq_close, ma200)}")
print()
print(f"ALLOCATION:    {alloc_final_pct}% TQQQ / {cash_pct}% SGOV")
print(f"Previous:      {prev_str}")
print(f"Action:        {action_label}")
print()
print(f"--- Informational (not used for sizing) ---")
print(f"Vol (20d):     {vol_pct:.1f}%")
print(f"Vol-implied:   {vol_implied_pct}%")
print(f"RSI:           {rsi_str}")
print(f"MACD:          {macd_str}")
print(f"50MA:          {ma50_str}  ({ma_dist(qqq_close, ma50)})")
print(f"100MA:         {ma100_str}  ({ma_dist(qqq_close, ma100)})")
print()
print(f"Report saved:  {report_path}")
