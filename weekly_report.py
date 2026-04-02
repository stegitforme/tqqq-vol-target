# weekly_report.py
# =========================
# TQQQ Volatility Target  —  v2 with 200MA gate + staged re-entry signals
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
TARGET_VOL    = 0.20        # annualised vol target (20%)
LOOKBACK_DAYS = 20          # days for realized vol window
ROUND_STEP    = 0.05        # round allocation to nearest 5%
TRADING_DAYS  = 252

# MA periods (all measured on QQQ daily closes)
MA_50  = 50
MA_100 = 100
MA_200 = 200

# RSI period
RSI_PERIOD = 14

# MACD parameters
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
    return round(x / step) * step

def load_prices(path: str, ticker: str) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"Missing {path}. Run fetch_tqqq.py first.")
    df = pd.read_csv(path, parse_dates=["Date"])
    df = df.sort_values("Date").reset_index(drop=True)
    return df

def realized_vol_lookback(prices: pd.Series) -> float:
    rets = (prices / prices.shift(1)).apply(lambda x: math.log(x) if pd.notna(x) else x)
    rets = rets.dropna()
    if len(rets) < LOOKBACK_DAYS:
        raise RuntimeError(f"Not enough data for {LOOKBACK_DAYS}-day vol. Have {len(rets)}.")
    window = rets.tail(LOOKBACK_DAYS)
    return float(window.std(ddof=1)) * math.sqrt(TRADING_DAYS)

def compute_sma(series: pd.Series, period: int) -> float | None:
    s = series.dropna()
    if len(s) < period:
        return None
    return float(s.tail(period).mean())

def compute_rsi(series: pd.Series, period: int = 14) -> float | None:
    s = series.dropna()
    if len(s) < period + 1:
        return None
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.tail(period * 3).ewm(com=period - 1, min_periods=period).mean().iloc[-1]
    avg_loss = loss.tail(period * 3).ewm(com=period - 1, min_periods=period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))

def compute_macd(series: pd.Series, fast=12, slow=26, signal=9) -> dict:
    """Returns dict with macd_line, signal_line, histogram, is_bullish_cross."""
    s = series.dropna()
    if len(s) < slow + signal:
        return {"macd_line": None, "signal_line": None, "histogram": None, "bullish": None}
    ema_fast   = s.ewm(span=fast,   adjust=False).mean()
    ema_slow   = s.ewm(span=slow,   adjust=False).mean()
    macd_line  = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram  = macd_line - signal_line
    # bullish = MACD line is above its signal line right now
    bullish = bool(macd_line.iloc[-1] > signal_line.iloc[-1])
    return {
        "macd_line":   round(float(macd_line.iloc[-1]), 4),
        "signal_line": round(float(signal_line.iloc[-1]), 4),
        "histogram":   round(float(histogram.iloc[-1]), 4),
        "bullish":     bullish,
    }

def upsert_history_row(history_path: str, row: dict) -> pd.DataFrame:
    if Path(history_path).exists():
        hist = pd.read_csv(history_path, parse_dates=["RunDate"])
        hist["RunDate"] = pd.to_datetime(hist["RunDate"]).dt.normalize()
    else:
        hist = pd.DataFrame(columns=[
            "RunDate","TQQQ_Close","QQQ_Close","RealizedVol20d","TargetVol",
            "QQQ_MA50","QQQ_MA100","QQQ_MA200","QQQ_RSI","MACD_Bullish",
            "MA200_Gate","ReentryStage","AllocTQQQ","AllocCash"
        ])
    new_row = pd.DataFrame([row])
    new_row["RunDate"] = pd.to_datetime(new_row["RunDate"]).dt.normalize()
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

def reentry_stage(
    qqq_price: float,
    ma50: float | None,
    ma200: float | None,
    rsi: float | None,
    macd_bullish: bool | None,
    vol_alloc: float,
) -> tuple[int, str]:
    """
    Returns (stage_number, description).
    Stage 0 = fully blocked (below 200MA, no signals)
    Stage 1 = RSI < 30 and bouncing + MACD bullish  → 20% max
    Stage 2 = QQQ above 50MA                         → 50% max
    Stage 3 = QQQ above 200MA (current rule)         → 80% max
    Stage 4 = 3+ weeks above 200MA (golden cross approach) → full vol alloc
    """
    above_200 = (ma200 is not None) and (qqq_price > ma200)
    above_50  = (ma50  is not None) and (qqq_price > ma50)
    rsi_oversold_bounce = (rsi is not None) and (rsi <= 35) and (macd_bullish is True)

    if above_200:
        return (3, f"QQQ above 200MA ({ma200:.1f}) — Stage 3: up to 80% of vol target")
    if above_50:
        return (2, f"QQQ above 50MA ({ma50:.1f}) but below 200MA ({ma200:.1f if ma200 else '?'}) — Stage 2: up to 50%")
    if rsi_oversold_bounce:
        return (1, f"RSI={rsi:.1f} oversold + MACD bullish — Stage 1: up to 20%")
    return (0, f"QQQ below all key MAs, no oversold bounce — Stage 0: 0% (SGOV)")

def apply_stage_cap(vol_alloc: float, stage: int) -> float:
    """Cap the vol-target allocation based on re-entry stage."""
    caps = {0: 0.0, 1: 0.20, 2: 0.50, 3: 0.80, 4: 1.0}
    cap = caps.get(stage, 0.0)
    return min(vol_alloc, cap)

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
        f"ASOF_DATE={asof_dt.date()} not in data/TQQQ.csv. Latest: {last_dt.date()}."
    )

tqqq_close = float(asof_rows.iloc[-1]["Close"])
tqqq_upto  = tqqq[pd.to_datetime(tqqq["Date"]).dt.normalize() <= asof_dt].copy()
tqqq_upto  = tqqq_upto.sort_values("Date").reset_index(drop=True)

# -------------------------
# Vol calculation (unchanged from original)
# -------------------------
vol_ann   = realized_vol_lookback(tqqq_upto["Close"])
alloc_raw = clamp(TARGET_VOL / vol_ann if vol_ann > 0 else 0.0, 0.0, 1.0)
alloc_vol = round_to_step(alloc_raw, ROUND_STEP)   # vol-only allocation (before gates)

# -------------------------
# Load QQQ prices + compute all signals
# -------------------------
qqq = load_prices(QQQ_CSV_PATH, "QQQ")
qqq_upto = qqq[pd.to_datetime(qqq["Date"]).dt.normalize() <= asof_dt].copy()
qqq_upto = qqq_upto.sort_values("Date").reset_index(drop=True)

if qqq_upto.empty:
    raise RuntimeError("No QQQ data available up to ASOF_DATE. Run fetch_tqqq.py first.")

qqq_close = float(qqq_upto["Close"].iloc[-1])
qqq_prices = qqq_upto["Close"]

ma50  = compute_sma(qqq_prices, MA_50)
ma100 = compute_sma(qqq_prices, MA_100)
ma200 = compute_sma(qqq_prices, MA_200)
rsi   = compute_rsi(qqq_prices, RSI_PERIOD)
macd  = compute_macd(qqq_prices, MACD_FAST, MACD_SLOW, MACD_SIGNAL)

# -------------------------
# 200MA Gate (hard override)
# -------------------------
above_200ma = (ma200 is not None) and (qqq_close > ma200)

# -------------------------
# Staged re-entry
# -------------------------
stage, stage_desc = reentry_stage(
    qqq_price    = qqq_close,
    ma50         = ma50,
    ma200        = ma200,
    rsi          = rsi,
    macd_bullish = macd["bullish"],
    vol_alloc    = alloc_vol,
)

# Final allocation = vol-target capped by stage
alloc_staged = apply_stage_cap(alloc_vol, stage)
alloc_final  = round_to_step(alloc_staged, ROUND_STEP)
alloc_final  = clamp(alloc_final, 0.0, 1.0)
cash_final   = 1.0 - alloc_final

run_date_str     = asof_dt.strftime("%Y-%m-%d")
vol_pct          = vol_ann * 100
alloc_final_pct  = int(round(alloc_final * 100))
alloc_vol_pct    = int(round(alloc_vol * 100))    # what vol alone would give
cash_pct         = int(round(cash_final * 100))

# -------------------------
# Build history row
# -------------------------
row = {
    "RunDate":         run_date_str,
    "TQQQ_Close":      tqqq_close,
    "QQQ_Close":       qqq_close,
    "RealizedVol20d":  vol_ann,
    "TargetVol":       TARGET_VOL,
    "QQQ_MA50":        round(ma50,  2) if ma50  is not None else None,
    "QQQ_MA100":       round(ma100, 2) if ma100 is not None else None,
    "QQQ_MA200":       round(ma200, 2) if ma200 is not None else None,
    "QQQ_RSI":         round(rsi,   2) if rsi   is not None else None,
    "MACD_Bullish":    macd["bullish"],
    "MA200_Gate":      above_200ma,
    "ReentryStage":    stage,
    "AllocTQQQ":       alloc_final,
    "AllocCash":       cash_final,
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
    prev_rows  = history[pd.to_datetime(history["RunDate"]).dt.normalize() < asof_dt]
    prev_alloc = float(prev_rows.iloc[-1]["AllocTQQQ"]) if not prev_rows.empty else alloc_final

prev_alloc_pct = int(round(prev_alloc * 100))

# -------------------------
# Chart data
# -------------------------
chart_df    = history.tail(365).copy()
chart_dates = chart_df["RunDate"].apply(lambda x: str(x)[:10] if pd.notna(x) else "").tolist()
chart_alloc = (chart_df["AllocTQQQ"] * 100).round(2).tolist()
chart_vol_s = (chart_df["RealizedVol20d"] * 100).round(2).tolist()

chart_dates_js = json.dumps(chart_dates)
chart_alloc_js = json.dumps(chart_alloc)
chart_vol_js   = json.dumps(chart_vol_s)

# Stage color for the HTML pill
stage_colors = {
    0: ("#fee2e2", "#991b1b", "BLOCKED — 0% TQQQ"),
    1: ("#fef9c3", "#854d0e", "STAGE 1 — up to 20%"),
    2: ("#fef3c7", "#92400e", "STAGE 2 — up to 50%"),
    3: ("#dcfce7", "#166534", "STAGE 3 — up to 80%"),
    4: ("#d1fae5", "#065f46", "STAGE 4 — full allocation"),
}
stage_bg, stage_text, stage_label = stage_colors.get(stage, ("#f3f4f6","#111","Unknown"))

# Signal table rows
def yn(val, true_str, false_str, none_str="—"):
    if val is None:
        return none_str
    return true_str if val else false_str

ma50_str  = f"${ma50:.2f}"   if ma50  is not None else "—"
ma100_str = f"${ma100:.2f}"  if ma100 is not None else "—"
ma200_str = f"${ma200:.2f}"  if ma200 is not None else "—"
rsi_str   = f"{rsi:.1f}"     if rsi   is not None else "—"
macd_str  = yn(macd["bullish"], "✅ Bullish", "❌ Bearish")

qqq_vs_50  = yn(ma50  is not None and qqq_close > ma50,  "✅ Above", "❌ Below")
qqq_vs_100 = yn(ma100 is not None and qqq_close > ma100, "✅ Above", "❌ Below")
qqq_vs_200 = yn(ma200 is not None and qqq_close > ma200, "✅ Above", "❌ Below")
rsi_signal = "⚠️ Oversold" if rsi is not None and rsi < 35 else ("🔵 Neutral" if rsi is not None and rsi < 60 else ("🔴 Overbought" if rsi is not None else "—"))

# Action narrative
if stage == 0:
    action_text = (
        f"⛔ QQQ (${qqq_close:.2f}) is below its 200-day MA ({ma200_str}). "
        f"TQQQ allocation is <strong>0%</strong>. Park everything in SGOV and wait. "
        f"Watch for Stage 1: RSI ≤ 35 + MACD bullish crossover."
    )
elif stage == 1:
    action_text = (
        f"⚠️ Stage 1 triggered. RSI is oversold ({rsi_str}) and MACD is turning bullish. "
        f"QQQ still below 200MA — enter <strong>up to 20% TQQQ</strong>. "
        f"Vol target alone says {alloc_vol_pct}% but stage cap limits to 20%. "
        f"Watch for QQQ to reclaim 50MA ({ma50_str}) to advance to Stage 2."
    )
elif stage == 2:
    action_text = (
        f"🟡 Stage 2: QQQ (${qqq_close:.2f}) is above its 50MA ({ma50_str}) but below its 200MA ({ma200_str}). "
        f"Advance to <strong>up to 50% TQQQ</strong>. "
        f"Vol target says {alloc_vol_pct}%, stage cap applies. "
        f"Watch for QQQ to close above 200MA ({ma200_str}) for Stage 3."
    )
elif stage == 3:
    action_text = (
        f"✅ Stage 3: QQQ (${qqq_close:.2f}) is above its 200MA ({ma200_str}). "
        f"<strong>{alloc_final_pct}% TQQQ / {cash_pct}% SGOV</strong> per vol target (capped at 80%). "
        f"After 3 consecutive weeks above 200MA, consider removing the 80% cap (Stage 4)."
    )
else:
    action_text = (
        f"🟢 Stage 4: Trend fully confirmed. "
        f"<strong>{alloc_final_pct}% TQQQ / {cash_pct}% SGOV</strong> per vol target, no cap."
    )

# -------------------------
# Report paths + URLs
# -------------------------
report_rel_path = f"reports/{run_date_str}.html"
report_file     = REPORTS_DIR / f"{run_date_str}.html"
latest_file     = OUTPUT_DIR / "weekly_report.html"
report_url      = f"{PAGES_BASE_URL}/{report_rel_path}"

(OUTPUT_DIR / "latest_report_path.txt").write_text(report_rel_path, encoding="utf-8")
(OUTPUT_DIR / "latest_report_url.txt").write_text(report_url,       encoding="utf-8")

subject = (
    f"TQQQ | Stage {stage} | {alloc_final_pct}% TQQQ / {cash_pct}% Cash | "
    f"Vol20={vol_pct:.1f}% | QQQ {qqq_vs_200.replace('✅','').replace('❌','').strip()} 200MA"
)
message = (
    f"{subject}\n"
    f"Date={run_date_str}  Mode={MODE}\n"
    f"QQQ Close=${qqq_close:.2f}  200MA={ma200_str}  50MA={ma50_str}\n"
    f"RSI={rsi_str}  MACD={macd_str}\n"
    f"Stage: {stage_desc}\n"
    f"Vol-only alloc: {alloc_vol_pct}%  →  Final alloc after stage cap: {alloc_final_pct}%\n"
)

(OUTPUT_DIR / "subject.txt").write_text(subject, encoding="utf-8")
(OUTPUT_DIR / "message.txt").write_text(message, encoding="utf-8")

# -------------------------
# HTML report
# -------------------------
html_tpl = Template(r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>TQQQ Vol Target — $RUN_DATE</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  *{box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f4f4f5;margin:0;padding:24px;color:#111}
  h1{margin:0 0 4px;font-size:32px;font-weight:700}
  .sub{color:#666;margin-bottom:16px;font-size:15px}
  .config-pill{display:inline-block;background:#e0e7ff;color:#3730a3;padding:7px 14px;border-radius:999px;font-size:13px;margin-bottom:18px}
  .stage-pill{display:inline-block;padding:10px 18px;border-radius:999px;font-size:15px;font-weight:600;margin-bottom:20px;background:$STAGE_BG;color:$STAGE_TEXT}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
  .card{background:#fff;border-radius:16px;padding:20px 22px;box-shadow:0 4px 14px rgba(0,0,0,0.06)}
  .card h2{margin:0 0 14px;font-size:20px;font-weight:600}
  .wide{grid-column:1/-1}
  table{width:100%;border-collapse:collapse}
  td{padding:9px 0;border-bottom:1px solid #f0f0f0;font-size:15px;vertical-align:middle}
  td:last-child{text-align:right;font-weight:600}
  .muted{color:#777;font-weight:400}
  .big{font-size:20px;font-weight:700}
  .action-box{background:#f8faff;border-left:4px solid #4f46e5;border-radius:8px;padding:14px 16px;font-size:15px;line-height:1.65;margin-top:4px}
  .signal-ok{color:#15803d}.signal-bad{color:#dc2626}.signal-warn{color:#b45309}.signal-neu{color:#4b5563}
  @media(max-width:760px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>

<h1>TQQQ Volatility Target</h1>
<div class="sub">Weekly sizing · runs after Friday close · execute Monday open</div>
<div class="config-pill">Target Vol = $TARGET_VOL_PCT% &nbsp;|&nbsp; Lookback = 20d &nbsp;|&nbsp; Round = 5% &nbsp;|&nbsp; Mode = $MODE</div>
<br>
<div class="stage-pill">$STAGE_LABEL</div>

<div class="grid">

  <!-- This week card -->
  <div class="card">
    <h2>This week — $RUN_DATE</h2>
    <table>
      <tr><td class="muted">TQQQ close</td>      <td>$$$TQQQ_CLOSE</td></tr>
      <tr><td class="muted">QQQ close</td>       <td>$$$QQQ_CLOSE</td></tr>
      <tr><td class="muted">Realized vol (20d)</td><td>$VOL_PCT%</td></tr>
      <tr><td class="muted">Vol-only allocation</td><td>$ALLOC_VOL_PCT% TQQQ</td></tr>
      <tr><td class="big">Re-entry stage</td>    <td class="big">Stage $STAGE</td></tr>
      <tr><td class="big">Previous alloc</td>    <td class="big">$PREV_PCT% TQQQ</td></tr>
      <tr><td class="big">➡ Final allocation</td><td class="big">$ALLOC_PCT% TQQQ&nbsp;/&nbsp;$CASH_PCT% SGOV</td></tr>
    </table>
  </div>

  <!-- Signal dashboard -->
  <div class="card">
    <h2>Signal dashboard</h2>
    <table>
      <tr><td class="muted">QQQ vs 50MA ($MA50)</td>    <td>$QQQ_VS_50</td></tr>
      <tr><td class="muted">QQQ vs 100MA ($MA100)</td>  <td>$QQQ_VS_100</td></tr>
      <tr><td class="muted">QQQ vs 200MA ($MA200)</td>  <td>$QQQ_VS_200</td></tr>
      <tr><td class="muted">RSI (14)</td>                <td>$RSI_VAL &nbsp; $RSI_SIGNAL</td></tr>
      <tr><td class="muted">MACD (12/26/9)</td>          <td>$MACD_STR</td></tr>
    </table>
  </div>

  <!-- Action narrative -->
  <div class="card wide">
    <h2>What to do Monday</h2>
    <div class="action-box">$ACTION_TEXT</div>
    <br>
    <div style="font-size:13px;color:#888;line-height:1.6">
      <strong>Stage ladder:</strong>
      Stage 0 = QQQ below all MAs → 0% TQQQ.&nbsp;
      Stage 1 = RSI ≤ 35 + MACD bullish → up to 20%.&nbsp;
      Stage 2 = QQQ above 50MA → up to 50%.&nbsp;
      Stage 3 = QQQ above 200MA → up to 80%.&nbsp;
      Stage 4 = 3+ weeks above 200MA → full vol target.
    </div>
  </div>

  <!-- Chart -->
  <div class="card wide">
    <h2>Last 365 days — allocation &amp; realized vol</h2>
    <canvas id="chart" style="max-height:320px"></canvas>
    <div style="margin-top:10px;color:#888;font-size:13px">
      Allocation shown is the <em>final staged allocation</em> (vol target × stage cap). History from logs/history.csv.
    </div>
  </div>

</div>

<script>
const labels = $CHART_DATES;
const alloc  = $CHART_ALLOC;
const vol    = $CHART_VOL;
new Chart(document.getElementById("chart"),{
  type:"line",
  data:{
    labels,
    datasets:[
      {label:"TQQQ Allocation (%)",data:alloc,tension:0.25,yAxisID:"y",
       borderColor:"#4f46e5",backgroundColor:"rgba(79,70,229,0.08)",fill:true,pointRadius:2},
      {label:"Realized Vol 20d (%)",data:vol,tension:0.25,yAxisID:"y1",
       borderColor:"#f59e0b",backgroundColor:"transparent",pointRadius:2}
    ]
  },
  options:{
    responsive:true,
    interaction:{mode:"index",intersect:false},
    scales:{
      y: {position:"left", min:0,max:100,title:{display:true,text:"Allocation (%)"}},
      y1:{position:"right",min:0,max:80, grid:{drawOnChartArea:false},title:{display:true,text:"Realized Vol (%)"}}
    }
  }
});
</script>
</body>
</html>
""")

html = html_tpl.substitute(
    MODE            = MODE,
    RUN_DATE        = run_date_str,
    TARGET_VOL_PCT  = int(TARGET_VOL * 100),
    TQQQ_CLOSE      = f"{tqqq_close:.2f}",
    QQQ_CLOSE       = f"{qqq_close:.2f}",
    VOL_PCT         = f"{vol_pct:.1f}",
    ALLOC_VOL_PCT   = str(alloc_vol_pct),
    STAGE           = str(stage),
    STAGE_LABEL     = stage_label,
    STAGE_BG        = stage_bg,
    STAGE_TEXT      = stage_text,
    PREV_PCT        = str(prev_alloc_pct),
    ALLOC_PCT       = str(alloc_final_pct),
    CASH_PCT        = str(cash_pct),
    MA50            = ma50_str,
    MA100           = ma100_str,
    MA200           = ma200_str,
    QQQ_VS_50       = qqq_vs_50,
    QQQ_VS_100      = qqq_vs_100,
    QQQ_VS_200      = qqq_vs_200,
    RSI_VAL         = rsi_str,
    RSI_SIGNAL      = rsi_signal,
    MACD_STR        = macd_str,
    ACTION_TEXT     = action_text,
    CHART_DATES     = chart_dates_js,
    CHART_ALLOC     = chart_alloc_js,
    CHART_VOL       = chart_vol_js,
)

report_file.write_text(html, encoding="utf-8")
latest_file.write_text(html, encoding="utf-8")

print(f"✅  Date       : {run_date_str}")
print(f"✅  TQQQ close : ${tqqq_close:.2f}")
print(f"✅  QQQ close  : ${qqq_close:.2f}")
print(f"✅  200MA      : {ma200_str}  ({'ABOVE' if above_200ma else 'BELOW'})")
print(f"✅  50MA       : {ma50_str}")
print(f"✅  RSI        : {rsi_str}")
print(f"✅  MACD       : {macd_str}")
print(f"✅  Stage      : {stage} — {stage_label}")
print(f"✅  Vol alloc  : {alloc_vol_pct}%  →  Final: {alloc_final_pct}%  Cash: {cash_pct}%")
print(f"✅  Report     : {report_file}")
print(f"✅  URL        : {report_url}")
