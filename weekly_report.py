# weekly_report.py
# =========================
# TQQQ Vol(35%) + 200MA Strategy — v6
# Vol-targeting: size TQQQ to target 35% annualized portfolio vol
# 200MA gate: if QQQ below 200MA → 100% SGOV regardless
# Vol Acceleration Guard: if vol rises 30%+ in 5 days AND vol > 45% → cap at 50%
# Turbo mode tracker retained
# =========================
from __future__ import annotations
import json
import os
import math
import pandas as pd
from pathlib import Path

# -------------------------
# Config
# -------------------------
TARGET_VOL      = 0.35   # vol target (35%)
LOOKBACK_DAYS   = 20
ROUND_STEP      = 0.05
TRADING_DAYS    = 252
MA_50           = 50
MA_100          = 100
MA_200          = 200
RSI_PERIOD      = 14
MACD_FAST       = 12
MACD_SLOW       = 26
MACD_SIGNAL     = 9
TURBO_MIN_WEEKS_BELOW = 8
TURBO_DURATION_WEEKS  = 12

# Vol Acceleration Guard parameters
# If vol rose 30%+ vs 5 trading days ago AND current vol > 45% → cap weight at 50%
VOL_ACCEL_THRESHOLD = 1.30   # vol must be 30% higher than 5 days ago
VOL_ACCEL_FLOOR     = 0.45   # only engage guard when vol is already elevated (>45%)
VOL_ACCEL_CAP       = 0.50   # cap allocation at 50% when guard fires

TQQQ_CSV_PATH        = "data/TQQQ.csv"
QQQ_CSV_PATH         = "data/QQQ.csv"
HISTORY_PATH         = "logs/history.csv"
HISTORY_OFFICIAL_PATH= "logs/history_official.csv"
OUTPUT_DIR           = Path("output")
REPORTS_DIR          = OUTPUT_DIR / "reports"
PAGES_BASE_URL       = "https://stegitforme.github.io/tqqq-vol-target"

# -------------------------
# Helpers
# -------------------------
def round_to_step(x, step):
    return round(round(x / step) * step, 10)

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def parse_asof_date():
    raw = os.environ.get("ASOF_DATE", "").strip()
    if not raw:
        return None
    try:
        return pd.to_datetime(raw).normalize()
    except Exception:
        return None

def load_prices(path, ticker):
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    if "Date" not in df.columns or "Close" not in df.columns:
        raise ValueError(f"{ticker}: expected Date,Close columns in {path}")
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").reset_index(drop=True)
    return df

def compute_realized_vol(series, window=20):
    s = series.dropna()
    if len(s) < window + 1:
        return None
    recent   = s.iloc[-(window + 1):]
    log_rets = [math.log(recent.iloc[i] / recent.iloc[i-1]) for i in range(1, len(recent))]
    mean     = sum(log_rets) / len(log_rets)
    variance = sum((r - mean)**2 for r in log_rets) / (len(log_rets) - 1)
    return math.sqrt(variance) * math.sqrt(TRADING_DAYS)

def compute_vol_alloc(vol_ann, target_vol=TARGET_VOL):
    """Size TQQQ so portfolio vol = target_vol. Capped at 100%."""
    if vol_ann <= 0:
        return 1.0
    raw = target_vol / vol_ann
    return round_to_step(min(1.0, raw), ROUND_STEP)

def compute_sma(series, period):
    s = series.dropna()
    if len(s) < period:
        return None
    return float(s.iloc[-period:].mean())

def compute_rsi(series, period=14):
    s = series.dropna()
    if len(s) < period + 1:
        return None
    deltas    = s.diff().dropna()
    gains     = deltas.clip(lower=0).iloc[-period:]
    losses    = (-deltas.clip(upper=0)).iloc[-period:]
    avg_gain  = gains.mean()
    avg_loss  = losses.mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))

def compute_macd(series, fast=12, slow=26, signal=9):
    s = series.dropna()
    if len(s) < slow + signal:
        return {"macd_line": None, "signal_line": None, "histogram": None, "bullish": None}
    ema_fast    = s.ewm(span=fast,   adjust=False).mean()
    ema_slow    = s.ewm(span=slow,   adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram   = macd_line - signal_line
    bullish     = bool(macd_line.iloc[-1] > signal_line.iloc[-1])
    return {
        "macd_line":   round(float(macd_line.iloc[-1]),   4),
        "signal_line": round(float(signal_line.iloc[-1]), 4),
        "histogram":   round(float(histogram.iloc[-1]),   4),
        "bullish":     bullish,
    }

def upsert_history_row(history_path, row):
    if Path(history_path).exists():
        hist = pd.read_csv(history_path)
    else:
        Path(history_path).parent.mkdir(parents=True, exist_ok=True)
        hist = pd.DataFrame()
    new_row = pd.DataFrame([row])
    hist    = pd.concat([hist, new_row], ignore_index=True)
    hist    = hist.drop_duplicates(subset=["RunDate"], keep="last")
    hist.to_csv(history_path, index=False)
    return hist

def prev_official_alloc(asof):
    p = Path(HISTORY_OFFICIAL_PATH)
    if not p.exists():
        return None
    h    = pd.read_csv(p)
    h["RunDate"] = pd.to_datetime(h["RunDate"])
    past = h[h["RunDate"] < asof].sort_values("RunDate")
    if past.empty:
        return None
    return float(past["AllocTQQQ"].iloc[-1])

# -------------------------
# Turbo mode tracker
# -------------------------
def compute_turbo_status(qqq_all_data, asof_dt, above_200ma_today):
    df      = qqq_all_data.copy()
    df      = df.sort_values("Date").reset_index(drop=True)
    closes  = df["Close"].values
    above   = []
    for i in range(len(closes)):
        if i < 199:
            above.append(None)
        else:
            ma = closes[i-199:i+1].mean()
            above.append(closes[i] > ma)
    df["above_200"] = above
    df["weekday"]   = pd.to_datetime(df["Date"]).dt.weekday
    fridays = df[
        (df["weekday"] == 4) &
        (pd.to_datetime(df["Date"]) <= asof_dt) &
        (df["above_200"].notna())
    ].copy().reset_index(drop=True)
    if fridays.empty:
        return _turbo_default()
    weeks_below_now = 0
    for i in range(len(fridays) - 1, -1, -1):
        if fridays.iloc[i]["above_200"]:
            break
        weeks_below_now += 1
    turbo_active      = False
    turbo_weeks_done  = 0
    turbo_weeks_left  = 0
    last_reclaim_date = None
    last_below_count  = None
    i = 0
    while i < len(fridays):
        row = fridays.iloc[i]
        if not row["above_200"]:
            j = i
            while j < len(fridays) and not fridays.iloc[j]["above_200"]:
                j += 1
            below_weeks = j - i
            if below_weeks >= TURBO_MIN_WEEKS_BELOW and j < len(fridays):
                reclaim_date     = pd.to_datetime(fridays.iloc[j]["Date"])
                last_reclaim_date = reclaim_date
                last_below_count  = below_weeks
                fridays_since     = len(fridays) - j
                if fridays_since <= TURBO_DURATION_WEEKS:
                    turbo_active     = True
                    turbo_weeks_done = fridays_since
                    turbo_weeks_left = TURBO_DURATION_WEEKS - fridays_since
                else:
                    turbo_active = False
            i = j + 1 if j < len(fridays) else j
        else:
            i += 1
    return {
        "weeks_below_now":   weeks_below_now,
        "turbo_active":      turbo_active,
        "turbo_reclaim_date": last_reclaim_date.strftime("%Y-%m-%d") if last_reclaim_date else None,
        "turbo_weeks_done":  turbo_weeks_done,
        "turbo_weeks_left":  turbo_weeks_left,
        "last_below_count":  last_below_count,
        "trigger_threshold": TURBO_MIN_WEEKS_BELOW,
        "turbo_duration":    TURBO_DURATION_WEEKS,
        "weeks_to_trigger":  max(0, TURBO_MIN_WEEKS_BELOW - weeks_below_now) if weeks_below_now > 0 and not above_200ma_today else 0,
    }

def _turbo_default():
    return {
        "weeks_below_now":   0,
        "turbo_active":      False,
        "turbo_reclaim_date": None,
        "turbo_weeks_done":  0,
        "turbo_weeks_left":  0,
        "last_below_count":  None,
        "trigger_threshold": TURBO_MIN_WEEKS_BELOW,
        "turbo_duration":    TURBO_DURATION_WEEKS,
        "weeks_to_trigger":  0,
    }

# -------------------------
# Env + load data
# -------------------------
MODE      = (os.environ.get("MODE") or "debug").strip().lower()
ASOF_DATE = parse_asof_date()

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

tqqq_close  = float(asof_rows["Close"].iloc[-1])
tqqq_prices = tqqq[pd.to_datetime(tqqq["Date"]).dt.normalize() <= asof_dt]["Close"]

qqq       = load_prices(QQQ_CSV_PATH, "QQQ")
qqq_rows  = qqq[pd.to_datetime(qqq["Date"]).dt.normalize() == asof_dt]
qqq_close = float(qqq_rows["Close"].iloc[-1]) if not qqq_rows.empty else float(qqq["Close"].iloc[-1])
qqq_prices = qqq[pd.to_datetime(qqq["Date"]).dt.normalize() <= asof_dt]["Close"]

# -------------------------
# Compute indicators
# -------------------------
vol_ann = compute_realized_vol(tqqq_prices, LOOKBACK_DAYS) or 0.50
ma50    = compute_sma(qqq_prices, MA_50)
ma100   = compute_sma(qqq_prices, MA_100)
ma200   = compute_sma(qqq_prices, MA_200)
rsi     = compute_rsi(qqq_prices, RSI_PERIOD)
macd    = compute_macd(qqq_prices, MACD_FAST, MACD_SLOW, MACD_SIGNAL)

# Vol 5 days ago (for acceleration guard)
tqqq_prices_5d_ago = tqqq[
    pd.to_datetime(tqqq["Date"]).dt.normalize() <= (asof_dt - pd.Timedelta(days=7))
]["Close"]  # 7 calendar days ≈ 5 trading days
vol_ann_5d_ago = compute_realized_vol(tqqq_prices_5d_ago, LOOKBACK_DAYS) or vol_ann

# -------------------------
# Vol(35%) + 200MA Gate
# -------------------------
above_200ma = (ma200 is not None) and (qqq_close > ma200)

if above_200ma:
    alloc_base = compute_vol_alloc(vol_ann, TARGET_VOL)

    # ── Vol Acceleration Guard ─────────────────────────────────────────────
    # If vol has risen 30%+ vs 5 trading days ago AND is already above 45%
    # → cap allocation at 50% to reduce exposure during fast vol spikes
    vol_accelerating = (
        vol_ann_5d_ago > 0 and
        vol_ann > vol_ann_5d_ago * VOL_ACCEL_THRESHOLD and
        vol_ann > VOL_ACCEL_FLOOR
    )
    if vol_accelerating:
        alloc_final = min(alloc_base, VOL_ACCEL_CAP)
    else:
        alloc_final = alloc_base
    # ──────────────────────────────────────────────────────────────────────

    cash_final = 1.0 - alloc_final
else:
    # QQQ below 200MA — exit entirely to SGOV
    alloc_final      = 0.0
    cash_final       = 1.0
    vol_accelerating = False
    alloc_base       = 0.0

# -------------------------
# Turbo mode status
# -------------------------
turbo = compute_turbo_status(qqq, asof_dt, above_200ma)

# -------------------------
# Build history row
# -------------------------
run_date_str    = asof_dt.strftime("%Y-%m-%d")
vol_pct         = vol_ann * 100
vol_5d_pct      = vol_ann_5d_ago * 100
alloc_final_pct = int(round(alloc_final * 100))
cash_pct        = int(round(cash_final * 100))

row = {
    "RunDate":           run_date_str,
    "TQQQ_Close":        tqqq_close,
    "QQQ_Close":         qqq_close,
    "RealizedVol20d":    vol_ann,
    "Vol5dAgo":          vol_ann_5d_ago,
    "VolAccelGuard":     vol_accelerating,
    "TargetVol":         TARGET_VOL,
    "VolImpliedAlloc":   alloc_base,
    "QQQ_MA50":          round(ma50,  2) if ma50  is not None else None,
    "QQQ_MA100":         round(ma100, 2) if ma100 is not None else None,
    "QQQ_MA200":         round(ma200, 2) if ma200 is not None else None,
    "QQQ_RSI":           round(rsi,   2) if rsi   is not None else None,
    "MACD_Bullish":      macd["bullish"],
    "MA200_Gate":        above_200ma,
    "AllocTQQQ":         alloc_final,
    "AllocCash":         cash_final,
    "WeeksBelow200MA":   turbo["weeks_below_now"],
    "TurboActive":       turbo["turbo_active"],
    "TurboWeeksLeft":    turbo["turbo_weeks_left"],
}

history = upsert_history_row(HISTORY_PATH, row)
if MODE == "official":
    _ = upsert_history_row(HISTORY_OFFICIAL_PATH, row)

# -------------------------
# Previous allocation
# -------------------------
prev_alloc = None
if MODE == "official":
    prev_alloc = prev_official_alloc(asof_dt)
if prev_alloc is None:
    prev_rows = history[pd.to_datetime(history["RunDate"]) < asof_dt].sort_values("RunDate")
    if not prev_rows.empty:
        prev_alloc = float(prev_rows["AllocTQQQ"].iloc[-1])

prev_alloc_pct = int(round(prev_alloc * 100)) if prev_alloc is not None else None

if prev_alloc is None:
    action_label = "HOLD"
elif alloc_final > prev_alloc + 0.04:
    action_label = "BUY / ADD — increase TQQQ"
elif alloc_final < prev_alloc - 0.04:
    action_label = "SELL / REDUCE — decrease TQQQ"
else:
    action_label = "HOLD — no meaningful change"

def ma_dist(price, ma):
    if ma is None:
        return "N/A"
    pct = (price - ma) / ma * 100
    return f"{'+'if pct>=0 else ''}{pct:.1f}%"

# -------------------------
# Turbo display strings
# -------------------------
def turbo_status_html(t):
    if t["turbo_active"]:
        wl = t["turbo_weeks_left"]; wd = t["turbo_weeks_done"]
        rd = t["turbo_reclaim_date"]
        bar_pct = int((wd / t["turbo_duration"]) * 100)
        return f"""
 <div style="background:#1a2a1a;border:2px solid #00C853;border-radius:6px;padding:16px;margin:16px 0;">
  <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">🚀 Turbo Mode — ACTIVE</div>
  <div style="font-size:22px;font-weight:bold;color:#00C853;">Week {wd} of {t['turbo_duration']}</div>
  <div style="font-size:13px;color:#888;margin-top:4px;">{wl} weeks remaining | Reclaim: {rd}</div>
  <div style="margin-top:10px;background:#0a0a0a;border-radius:4px;height:8px;overflow:hidden;">
   <div style="background:#00C853;height:100%;width:{bar_pct}%;border-radius:4px;"></div>
  </div>
 </div>"""
    elif not t["turbo_active"] and t["weeks_below_now"] > 0:
        wb = t["weeks_below_now"]; thr = t["trigger_threshold"]
        remaining = max(0, thr - wb)
        bar_pct   = int(min(100, (wb / thr) * 100))
        bar_color = "#FFC107" if remaining == 0 else "#FF6B35"
        status_line = (
            "⏳ Threshold reached — waiting for 200MA reclaim" if remaining == 0
            else f"⏳ {remaining} more week{'s' if remaining!=1 else ''} below to arm turbo"
        )
        return f"""
 <div style="background:#1a1a0a;border:2px solid {bar_color};border-radius:6px;padding:16px;margin:16px 0;">
  <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">⏳ Turbo Mode — Arming</div>
  <div style="font-size:22px;font-weight:bold;color:{bar_color};">{wb} / {thr} weeks below</div>
  <div style="font-size:13px;color:#888;margin-top:4px;">{status_line}</div>
  <div style="margin-top:10px;background:#0a0a0a;border-radius:4px;height:8px;overflow:hidden;">
   <div style="background:{bar_color};height:100%;width:{bar_pct}%;border-radius:4px;"></div>
  </div>
 </div>"""
    else:
        last_rd   = t.get("turbo_reclaim_date")
        last_note = f"Last turbo: {last_rd}" if last_rd else "No turbo events yet"
        return f"""
 <div style="background:#1a1a1a;border:1px solid #2a2a2a;border-radius:6px;padding:14px;margin:16px 0;">
  <div style="font-size:11px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">Turbo Mode — Standby</div>
  <div style="font-size:14px;color:#555;">QQQ above 200MA — vol-sized TQQQ position active.</div>
  <div style="font-size:11px;color:#444;margin-top:4px;">{last_note} | Triggers after {TURBO_MIN_WEEKS_BELOW}+ weeks below</div>
 </div>"""

def turbo_status_text(t):
    if t["turbo_active"]:
        return (f"🚀 TURBO ACTIVE: Week {t['turbo_weeks_done']} of {t['turbo_duration']} "
                f"({t['turbo_weeks_left']} weeks left) — reclaim {t['turbo_reclaim_date']}")
    elif t["weeks_below_now"] > 0:
        rem = max(0, t["trigger_threshold"] - t["weeks_below_now"])
        if rem == 0:
            return f"⏳ TURBO ARMED: {t['weeks_below_now']} weeks below — waiting for 200MA reclaim"
        return f"⏳ TURBO ARMING: {t['weeks_below_now']}/{t['trigger_threshold']} weeks below"
    else:
        return "Turbo standby — QQQ above 200MA, vol-sized position active"

# -------------------------
# Email subject
# -------------------------
turbo_tag = ""
if turbo["turbo_active"]:
    turbo_tag = f" | 🚀 TURBO Wk{turbo['turbo_weeks_done']}/{turbo['turbo_duration']}"
elif turbo["weeks_below_now"] >= TURBO_MIN_WEEKS_BELOW:
    turbo_tag = " | ⏳ TURBO ARMED"
elif turbo["weeks_below_now"] > 0:
    turbo_tag = f" | Arming {turbo['weeks_below_now']}/{TURBO_MIN_WEEKS_BELOW}wks"

# Show guard status in subject when active
guard_tag = " | ⚡ GUARD" if vol_accelerating else ""

gate_str  = "Above 200MA" if above_200ma else "Below 200MA"
alloc_str = f"{alloc_final_pct}% TQQQ" if alloc_final_pct > 0 else "0% TQQQ / 100% SGOV"

email_subject = (
    f"TQQQ | Vol({int(TARGET_VOL*100)}%)+200MA | {alloc_str} | "
    f"Vol20={vol_pct:.1f}% | QQQ {gate_str}{turbo_tag}{guard_tag}"
)

# -------------------------
# Colors + display helpers
# -------------------------
gate_color  = "#00C853" if above_200ma else "#FF1744"
gate_label  = "✅ ABOVE 200MA — Vol-sized TQQQ" if above_200ma else "🔴 BELOW 200MA — 100% SGOV"
action_color = (
    "#00C853" if "BUY"  in action_label else
    "#FF1744" if "SELL" in action_label else
    "#FFC107"
)

ma50_str  = f"{ma50:.2f}"  if ma50  is not None else "N/A"
ma100_str = f"{ma100:.2f}" if ma100 is not None else "N/A"
ma200_str = f"{ma200:.2f}" if ma200 is not None else "N/A"
rsi_str   = f"{rsi:.1f}"   if rsi   is not None else "N/A"
macd_str  = ("Bullish ▲" if macd["bullish"] else "Bearish ▼" if macd["bullish"] is False else "N/A")
macd_color = "#00C853" if macd["bullish"] else ("#FF1744" if macd["bullish"] is False else "#888")
prev_str  = f"{prev_alloc_pct}%" if prev_alloc_pct is not None else "—"

# Vol explanation with guard status
if above_200ma:
    vol_explain = (
        f"TQQQ 20d vol = {vol_pct:.1f}% annualized → "
        f"target {int(TARGET_VOL*100)}% portfolio vol → "
        f"base alloc = {int(TARGET_VOL*100)}% ÷ {vol_pct:.1f}% = {int(round(alloc_base*100))}% TQQQ"
    )
    if vol_accelerating:
        vol_explain += (
            f" → ⚡ Vol Accel Guard fired "
            f"(vol rose from {vol_5d_pct:.1f}% to {vol_pct:.1f}%, "
            f"{((vol_ann/vol_ann_5d_ago-1)*100):.0f}% in 5d, above {int(VOL_ACCEL_FLOOR*100)}% floor) "
            f"→ capped at {int(VOL_ACCEL_CAP*100)}%"
        )
else:
    vol_explain = "QQQ below 200MA — vol sizing overridden, 100% SGOV"

# Guard HTML block (only shown when relevant)
guard_html = ""
if vol_accelerating:
    guard_html = f"""
 <div style="background:#1a1500;border:2px solid #FFC107;border-radius:6px;padding:14px;margin:16px 0;">
  <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">⚡ Vol Acceleration Guard — ACTIVE</div>
  <div style="font-size:16px;font-weight:bold;color:#FFC107;">Allocation capped at {int(VOL_ACCEL_CAP*100)}% TQQQ</div>
  <div style="font-size:13px;color:#888;margin-top:6px;">
   Vol rose from {vol_5d_pct:.1f}% → {vol_pct:.1f}% ({((vol_ann/vol_ann_5d_ago-1)*100):.0f}% in ~5 trading days)<br>
   Base alloc would have been {int(round(alloc_base*100))}% — guard reduced to {int(VOL_ACCEL_CAP*100)}%
  </div>
 </div>"""
elif above_200ma and vol_ann_5d_ago > 0:
    accel_ratio = vol_ann / vol_ann_5d_ago
    guard_html = f"""
 <div style="background:#111;border:1px solid #222;border-radius:6px;padding:12px;margin:16px 0;">
  <div style="font-size:11px;color:#444;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">⚡ Vol Acceleration Guard — Standby</div>
  <div style="font-size:13px;color:#555;">
   Vol trend: {vol_5d_pct:.1f}% → {vol_pct:.1f}% ({accel_ratio:.2f}x in ~5d)
   — guard fires if vol rises {int((VOL_ACCEL_THRESHOLD-1)*100)}%+ AND exceeds {int(VOL_ACCEL_FLOOR*100)}%
  </div>
 </div>"""

# -------------------------
# HTML report
# -------------------------
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
report_path = REPORTS_DIR / f"{run_date_str}.html"

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TQQQ Signal — {run_date_str}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#080a0e;color:#e8eaf0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;padding:0 0 48px}}
.hdr{{background:#0d0f14;border-bottom:1px solid #1a1d24;padding:16px 24px;display:flex;align-items:center;justify-content:space-between}}
.logo{{font-size:20px;font-weight:800;color:#f0c040;letter-spacing:-0.5px}}
.logo-sub{{font-size:11px;color:#555c6e;margin-left:8px}}
.dbadge{{font-size:12px;color:#555c6e;background:#111318;border:1px solid #1a1d24;padding:4px 12px;border-radius:20px}}
.wrap{{max-width:680px;margin:0 auto;padding:24px 16px}}
.gate{{border-radius:10px;padding:16px 20px;text-align:center;margin-bottom:20px}}
.gate.up{{background:#0d1f0d;border:1px solid #1a4d1a}}.gate.dn{{background:#1f0d0d;border:1px solid #4d1a1a}}
.glbl{{font-size:14px;font-weight:700;margin-bottom:4px}}
.gate.up .glbl{{color:#00d68f}}.gate.dn .glbl{{color:#ff4560}}
.gmeta{{font-size:12px;color:#555c6e}}
.ac{{background:#0d0f14;border:1px solid #1a1d24;border-radius:12px;padding:28px 24px;text-align:center;margin-bottom:16px}}
.aclbl{{font-size:11px;color:#555c6e;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:12px}}
.acnum{{font-size:80px;font-weight:900;line-height:1;margin-bottom:6px}}
.acsplit{{font-size:20px;color:#8b91a0;margin-bottom:16px}}.acsplit span{{color:#4da6ff}}
.bw{{height:10px;background:#1a1d24;border-radius:5px;overflow:hidden;margin-bottom:16px}}
.bf{{height:100%;border-radius:5px}}
.actbadge{{display:inline-block;padding:10px 24px;border-radius:8px;font-size:15px;font-weight:700;margin-bottom:8px}}
.prevlbl{{font-size:12px;color:#555c6e}}
.g2{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px}}
@media(max-width:500px){{.g2{{grid-template-columns:1fr}}}}
.card{{background:#0d0f14;border:1px solid #1a1d24;border-radius:10px;padding:16px 18px}}
.cmb{{background:#0d0f14;border:1px solid #1a1d24;border-radius:10px;padding:16px 18px;margin-bottom:16px}}
.ctitle{{font-size:10px;color:#555c6e;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px}}
.vrow{{display:flex;align-items:center;justify-content:center;gap:12px;padding:8px 0}}
.vn{{text-align:center}}.vv{{font-size:26px;font-weight:700;color:#e8eaf0}}.vsub{{font-size:11px;color:#555c6e;margin-top:2px}}.vop{{font-size:22px;color:#333a4a}}
.mr{{display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid #111318;font-size:12px}}
.mr:last-child{{border-bottom:none}}.mn{{color:#555c6e}}.mv{{color:#8b91a0;font-family:monospace}}.md{{font-weight:600}}
.ir{{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid #111318;font-size:13px}}
.ir:last-child{{border-bottom:none}}.ik{{color:#555c6e}}.iv{{color:#8b91a0}}
.footer{{text-align:center;padding:24px 0 0;font-size:11px;color:#333a4a;line-height:1.8}}
</style>
</head>
<body>
<div class="hdr">
  <div><span class="logo">GOGO</span><span class="logo-sub">TQQQ Vol-Targeting Signal</span></div>
  <div class="dbadge">{run_date_str}</div>
</div>
<div class="wrap">
  <div class="gate {'up' if above_200ma else 'dn'}">
    <div class="glbl">{gate_label}</div>
    <div class="gmeta">QQQ ${qqq_close:.2f} &nbsp;·&nbsp; 200MA ${ma200_str} &nbsp;·&nbsp; {ma_dist(qqq_close, ma200)}</div>
  </div>
  <div class="ac">
    <div class="aclbl">This Week’s Allocation</div>
    <div class="acnum" style="color:{'#00d68f' if alloc_final_pct>=80 else '#f0c040' if alloc_final_pct>=40 else '#ff8c42'}">{alloc_final_pct}%</div>
    <div class="acsplit">TQQQ &nbsp;/&nbsp; <span>{cash_pct}% SGOV</span></div>
    <div class="bw"><div class="bf" style="width:{alloc_final_pct}%;background:{'#00d68f' if alloc_final_pct>=80 else '#f0c040' if alloc_final_pct>=40 else '#ff8c42'}"></div></div>
    <div class="actbadge" style="background:{'rgba(0,214,143,.12)' if 'HOLD' in action_label else 'rgba(240,192,64,.12)' if 'BUY' in action_label else 'rgba(255,140,66,.12)'};color:{'#00d68f' if 'HOLD' in action_label else '#f0c040' if 'BUY' in action_label else '#ff8c42'}">{action_label}</div>
    <div class="prevlbl">Previous: {prev_str}</div>
  </div>
  <div class="g2">
    <div class="card">
      <div class="ctitle">Vol Sizing Calculation</div>
      <div class="vrow">
        <div class="vn"><div class="vv">{vol_pct:.1f}%</div><div class="vsub">20d Vol</div></div>
        <div class="vop">÷</div>
        <div class="vn"><div class="vv">{int(TARGET_VOL*100)}%</div><div class="vsub">Target</div></div>
        <div class="vop">=</div>
        <div class="vn"><div class="vv" style="color:{'#00d68f' if alloc_final_pct>=80 else '#f0c040' if alloc_final_pct>=40 else '#ff8c42'}">{alloc_final_pct}%</div><div class="vsub">TQQQ</div></div>
      </div>
      <div style="text-align:center;font-size:11px;color:#555c6e;margin-top:8px">5d ago: {vol_5d_pct:.1f}%</div>
    </div>
    <div class="card">
      <div class="ctitle">QQQ Moving Averages</div>
      {''.join([
        f'<div class="mr"><span class="mn">{lbl}</span><span class="mv">${val}</span>'
        f'<span class="md" style="color:{"#00d68f" if qqq_close>raw else "#ff4560"}>{"+" if qqq_close>raw else ""}{(qqq_close-raw)/raw*100:.1f}%</span></div>'
        for lbl,val,raw in [("50-day",ma50_str,ma50),("100-day",ma100_str,ma100),("200-day ★",ma200_str,ma200)]
        if raw is not None
      ])}
      <div style="font-size:11px;color:#555c6e;margin-top:8px">RSI {rsi_str} &nbsp;·&nbsp; MACD <span style="color:{macd_color}">{macd_str}</span></div>
    </div>
  </div>
  {guard_html}
  <div class="cmb">
    <div class="ctitle">Additional Signals (informational)</div>
    <div class="ir"><span class="ik">MACD</span><span class="iv" style="color:{macd_color}">{macd_str} &nbsp;·&nbsp; Line: {macd.get('line',0):.3f} / Signal: {macd.get('signal_line',0):.4f}</span></div>
    <div class="ir"><span class="ik">Realized Vol (20d)</span><span class="iv">{vol_pct:.1f}% &nbsp;·&nbsp; 5d ago: {vol_5d_pct:.1f}% &nbsp;·&nbsp; Target {int(TARGET_VOL*100)}% → {alloc_final_pct}% TQQQ</span></div>
    <div class="ir"><span class="ik">Turbo Mode</span><span class="iv">{turbo_status_text(turbo)}</span></div>
  </div>
  <div class="cmb">
    <div class="ctitle">Backtest 2017–present &nbsp;·&nbsp; Vol({int(TARGET_VOL*100)}%)+200MA+Guard</div>
    <div class="ir"><span class="ik">Strategy CAGR</span><span class="iv" style="color:#00d68f">+33.2%</span></div>
    <div class="ir"><span class="ik">Max Drawdown</span><span class="iv" style="color:#ff4560">-31.8%</span></div>
    <div class="ir"><span class="ik">vs TQQQ Buy &amp; Hold</span><span class="iv">-81% DD &nbsp;·&nbsp; +45.3% CAGR</span></div>
    <div class="ir"><span class="ik">vs QQQ Buy &amp; Hold</span><span class="iv">-35% DD &nbsp;·&nbsp; +16.1% CAGR</span></div>
  </div>
  <div class="footer">
    Strategy: Vol({int(TARGET_VOL*100)}%) + 200MA Gate + Vol Accel Guard<br>
    When QQQ is above 200MA, TQQQ is sized to target {int(TARGET_VOL*100)}% annualized vol.<br>
    Guard caps at 50% if vol spikes 30%+ in 5 days while above 45%. Below 200MA → 100% SGOV.<br><br>
    Generated {run_date_str} &nbsp;·&nbsp; Mode: {MODE} &nbsp;·&nbsp; TQQQ: ${tqqq_close:.2f} &nbsp;·&nbsp; QQQ: ${qqq_close:.2f}
  </div>
</div>
</body>
</html>"""
# =========================
# TQQQ Vol(35%) + 200MA Strategy — v6
# Vol-targeting: size TQQQ to target 35% annualized portfolio vol
# 200MA gate: if QQQ below 200MA → 100% SGOV regardless
# Vol Acceleration Guard: if vol rises 30%+ in 5 days AND vol > 45% → cap at 50%
# Turbo mode tracker retained
# =========================
from __future__ import annotations
import json
import os
import math
import pandas as pd
from pathlib import Path

# -------------------------
# Config
# -------------------------
TARGET_VOL      = 0.35   # vol target (35%)
LOOKBACK_DAYS   = 20
ROUND_STEP      = 0.05
TRADING_DAYS    = 252
MA_50           = 50
MA_100          = 100
MA_200          = 200
RSI_PERIOD      = 14
MACD_FAST       = 12
MACD_SLOW       = 26
MACD_SIGNAL     = 9
TURBO_MIN_WEEKS_BELOW = 8
TURBO_DURATION_WEEKS  = 12

# Vol Acceleration Guard parameters
# If vol rose 30%+ vs 5 trading days ago AND current vol > 45% → cap weight at 50%
VOL_ACCEL_THRESHOLD = 1.30   # vol must be 30% higher than 5 days ago
VOL_ACCEL_FLOOR     = 0.45   # only engage guard when vol is already elevated (>45%)
VOL_ACCEL_CAP       = 0.50   # cap allocation at 50% when guard fires

TQQQ_CSV_PATH        = "data/TQQQ.csv"
QQQ_CSV_PATH         = "data/QQQ.csv"
HISTORY_PATH         = "logs/history.csv"
HISTORY_OFFICIAL_PATH= "logs/history_official.csv"
OUTPUT_DIR           = Path("output")
REPORTS_DIR          = OUTPUT_DIR / "reports"
PAGES_BASE_URL       = "https://stegitforme.github.io/tqqq-vol-target"

# -------------------------
# Helpers
# -------------------------
def round_to_step(x, step):
    return round(round(x / step) * step, 10)

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def parse_asof_date():
    raw = os.environ.get("ASOF_DATE", "").strip()
    if not raw:
        return None
    try:
        return pd.to_datetime(raw).normalize()
    except Exception:
        return None

def load_prices(path, ticker):
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    if "Date" not in df.columns or "Close" not in df.columns:
        raise ValueError(f"{ticker}: expected Date,Close columns in {path}")
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").reset_index(drop=True)
    return df

def compute_realized_vol(series, window=20):
    s = series.dropna()
    if len(s) < window + 1:
        return None
    recent   = s.iloc[-(window + 1):]
    log_rets = [math.log(recent.iloc[i] / recent.iloc[i-1]) for i in range(1, len(recent))]
    mean     = sum(log_rets) / len(log_rets)
    variance = sum((r - mean)**2 for r in log_rets) / (len(log_rets) - 1)
    return math.sqrt(variance) * math.sqrt(TRADING_DAYS)

def compute_vol_alloc(vol_ann, target_vol=TARGET_VOL):
    """Size TQQQ so portfolio vol = target_vol. Capped at 100%."""
    if vol_ann <= 0:
        return 1.0
    raw = target_vol / vol_ann
    return round_to_step(min(1.0, raw), ROUND_STEP)

def compute_sma(series, period):
    s = series.dropna()
    if len(s) < period:
        return None
    return float(s.iloc[-period:].mean())

def compute_rsi(series, period=14):
    s = series.dropna()
    if len(s) < period + 1:
        return None
    deltas    = s.diff().dropna()
    gains     = deltas.clip(lower=0).iloc[-period:]
    losses    = (-deltas.clip(upper=0)).iloc[-period:]
    avg_gain  = gains.mean()
    avg_loss  = losses.mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))

def compute_macd(series, fast=12, slow=26, signal=9):
    s = series.dropna()
    if len(s) < slow + signal:
        return {"macd_line": None, "signal_line": None, "histogram": None, "bullish": None}
    ema_fast    = s.ewm(span=fast,   adjust=False).mean()
    ema_slow    = s.ewm(span=slow,   adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram   = macd_line - signal_line
    bullish     = bool(macd_line.iloc[-1] > signal_line.iloc[-1])
    return {
        "macd_line":   round(float(macd_line.iloc[-1]),   4),
        "signal_line": round(float(signal_line.iloc[-1]), 4),
        "histogram":   round(float(histogram.iloc[-1]),   4),
        "bullish":     bullish,
    }

def upsert_history_row(history_path, row):
    if Path(history_path).exists():
        hist = pd.read_csv(history_path)
    else:
        Path(history_path).parent.mkdir(parents=True, exist_ok=True)
        hist = pd.DataFrame()
    new_row = pd.DataFrame([row])
    hist    = pd.concat([hist, new_row], ignore_index=True)
    hist    = hist.drop_duplicates(subset=["RunDate"], keep="last")
    hist.to_csv(history_path, index=False)
    return hist

def prev_official_alloc(asof):
    p = Path(HISTORY_OFFICIAL_PATH)
    if not p.exists():
        return None
    h    = pd.read_csv(p)
    h["RunDate"] = pd.to_datetime(h["RunDate"])
    past = h[h["RunDate"] < asof].sort_values("RunDate")
    if past.empty:
        return None
    return float(past["AllocTQQQ"].iloc[-1])

# -------------------------
# Turbo mode tracker
# -------------------------
def compute_turbo_status(qqq_all_data, asof_dt, above_200ma_today):
    df      = qqq_all_data.copy()
    df      = df.sort_values("Date").reset_index(drop=True)
    closes  = df["Close"].values
    above   = []
    for i in range(len(closes)):
        if i < 199:
            above.append(None)
        else:
            ma = closes[i-199:i+1].mean()
            above.append(closes[i] > ma)
    df["above_200"] = above
    df["weekday"]   = pd.to_datetime(df["Date"]).dt.weekday
    fridays = df[
        (df["weekday"] == 4) &
        (pd.to_datetime(df["Date"]) <= asof_dt) &
        (df["above_200"].notna())
    ].copy().reset_index(drop=True)
    if fridays.empty:
        return _turbo_default()
    weeks_below_now = 0
    for i in range(len(fridays) - 1, -1, -1):
        if fridays.iloc[i]["above_200"]:
            break
        weeks_below_now += 1
    turbo_active      = False
    turbo_weeks_done  = 0
    turbo_weeks_left  = 0
    last_reclaim_date = None
    last_below_count  = None
    i = 0
    while i < len(fridays):
        row = fridays.iloc[i]
        if not row["above_200"]:
            j = i
            while j < len(fridays) and not fridays.iloc[j]["above_200"]:
                j += 1
            below_weeks = j - i
            if below_weeks >= TURBO_MIN_WEEKS_BELOW and j < len(fridays):
                reclaim_date     = pd.to_datetime(fridays.iloc[j]["Date"])
                last_reclaim_date = reclaim_date
                last_below_count  = below_weeks
                fridays_since     = len(fridays) - j
                if fridays_since <= TURBO_DURATION_WEEKS:
                    turbo_active     = True
                    turbo_weeks_done = fridays_since
                    turbo_weeks_left = TURBO_DURATION_WEEKS - fridays_since
                else:
                    turbo_active = False
            i = j + 1 if j < len(fridays) else j
        else:
            i += 1
    return {
        "weeks_below_now":   weeks_below_now,
        "turbo_active":      turbo_active,
        "turbo_reclaim_date": last_reclaim_date.strftime("%Y-%m-%d") if last_reclaim_date else None,
        "turbo_weeks_done":  turbo_weeks_done,
        "turbo_weeks_left":  turbo_weeks_left,
        "last_below_count":  last_below_count,
        "trigger_threshold": TURBO_MIN_WEEKS_BELOW,
        "turbo_duration":    TURBO_DURATION_WEEKS,
        "weeks_to_trigger":  max(0, TURBO_MIN_WEEKS_BELOW - weeks_below_now) if weeks_below_now > 0 and not above_200ma_today else 0,
    }

def _turbo_default():
    return {
        "weeks_below_now":   0,
        "turbo_active":      False,
        "turbo_reclaim_date": None,
        "turbo_weeks_done":  0,
        "turbo_weeks_left":  0,
        "last_below_count":  None,
        "trigger_threshold": TURBO_MIN_WEEKS_BELOW,
        "turbo_duration":    TURBO_DURATION_WEEKS,
        "weeks_to_trigger":  0,
    }

# -------------------------
# Env + load data
# -------------------------
MODE      = (os.environ.get("MODE") or "debug").strip().lower()
ASOF_DATE = parse_asof_date()

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

tqqq_close  = float(asof_rows["Close"].iloc[-1])
tqqq_prices = tqqq[pd.to_datetime(tqqq["Date"]).dt.normalize() <= asof_dt]["Close"]

qqq       = load_prices(QQQ_CSV_PATH, "QQQ")
qqq_rows  = qqq[pd.to_datetime(qqq["Date"]).dt.normalize() == asof_dt]
qqq_close = float(qqq_rows["Close"].iloc[-1]) if not qqq_rows.empty else float(qqq["Close"].iloc[-1])
qqq_prices = qqq[pd.to_datetime(qqq["Date"]).dt.normalize() <= asof_dt]["Close"]

# -------------------------
# Compute indicators
# -------------------------
vol_ann = compute_realized_vol(tqqq_prices, LOOKBACK_DAYS) or 0.50
ma50    = compute_sma(qqq_prices, MA_50)
ma100   = compute_sma(qqq_prices, MA_100)
ma200   = compute_sma(qqq_prices, MA_200)
rsi     = compute_rsi(qqq_prices, RSI_PERIOD)
macd    = compute_macd(qqq_prices, MACD_FAST, MACD_SLOW, MACD_SIGNAL)

# Vol 5 days ago (for acceleration guard)
tqqq_prices_5d_ago = tqqq[
    pd.to_datetime(tqqq["Date"]).dt.normalize() <= (asof_dt - pd.Timedelta(days=7))
]["Close"]  # 7 calendar days ≈ 5 trading days
vol_ann_5d_ago = compute_realized_vol(tqqq_prices_5d_ago, LOOKBACK_DAYS) or vol_ann

# -------------------------
# Vol(35%) + 200MA Gate
# -------------------------
above_200ma = (ma200 is not None) and (qqq_close > ma200)

if above_200ma:
    alloc_base = compute_vol_alloc(vol_ann, TARGET_VOL)

    # ── Vol Acceleration Guard ─────────────────────────────────────────────
    # If vol has risen 30%+ vs 5 trading days ago AND is already above 45%
    # → cap allocation at 50% to reduce exposure during fast vol spikes
    vol_accelerating = (
        vol_ann_5d_ago > 0 and
        vol_ann > vol_ann_5d_ago * VOL_ACCEL_THRESHOLD and
        vol_ann > VOL_ACCEL_FLOOR
    )
    if vol_accelerating:
        alloc_final = min(alloc_base, VOL_ACCEL_CAP)
    else:
        alloc_final = alloc_base
    # ──────────────────────────────────────────────────────────────────────

    cash_final = 1.0 - alloc_final
else:
    # QQQ below 200MA — exit entirely to SGOV
    alloc_final      = 0.0
    cash_final       = 1.0
    vol_accelerating = False
    alloc_base       = 0.0

# -------------------------
# Turbo mode status
# -------------------------
turbo = compute_turbo_status(qqq, asof_dt, above_200ma)

# -------------------------
# Build history row
# -------------------------
run_date_str    = asof_dt.strftime("%Y-%m-%d")
vol_pct         = vol_ann * 100
vol_5d_pct      = vol_ann_5d_ago * 100
alloc_final_pct = int(round(alloc_final * 100))
cash_pct        = int(round(cash_final * 100))

row = {
    "RunDate":           run_date_str,
    "TQQQ_Close":        tqqq_close,
    "QQQ_Close":         qqq_close,
    "RealizedVol20d":    vol_ann,
    "Vol5dAgo":          vol_ann_5d_ago,
    "VolAccelGuard":     vol_accelerating,
    "TargetVol":         TARGET_VOL,
    "VolImpliedAlloc":   alloc_base,
    "QQQ_MA50":          round(ma50,  2) if ma50  is not None else None,
    "QQQ_MA100":         round(ma100, 2) if ma100 is not None else None,
    "QQQ_MA200":         round(ma200, 2) if ma200 is not None else None,
    "QQQ_RSI":           round(rsi,   2) if rsi   is not None else None,
    "MACD_Bullish":      macd["bullish"],
    "MA200_Gate":        above_200ma,
    "AllocTQQQ":         alloc_final,
    "AllocCash":         cash_final,
    "WeeksBelow200MA":   turbo["weeks_below_now"],
    "TurboActive":       turbo["turbo_active"],
    "TurboWeeksLeft":    turbo["turbo_weeks_left"],
}

history = upsert_history_row(HISTORY_PATH, row)
if MODE == "official":
    _ = upsert_history_row(HISTORY_OFFICIAL_PATH, row)

# -------------------------
# Previous allocation
# -------------------------
prev_alloc = None
if MODE == "official":
    prev_alloc = prev_official_alloc(asof_dt)
if prev_alloc is None:
    prev_rows = history[pd.to_datetime(history["RunDate"]) < asof_dt].sort_values("RunDate")
    if not prev_rows.empty:
        prev_alloc = float(prev_rows["AllocTQQQ"].iloc[-1])

prev_alloc_pct = int(round(prev_alloc * 100)) if prev_alloc is not None else None

if prev_alloc is None:
    action_label = "HOLD"
elif alloc_final > prev_alloc + 0.04:
    action_label = "BUY / ADD — increase TQQQ"
elif alloc_final < prev_alloc - 0.04:
    action_label = "SELL / REDUCE — decrease TQQQ"
else:
    action_label = "HOLD — no meaningful change"

def ma_dist(price, ma):
    if ma is None:
        return "N/A"
    pct = (price - ma) / ma * 100
    return f"{'+'if pct>=0 else ''}{pct:.1f}%"

# -------------------------
# Turbo display strings
# -------------------------
def turbo_status_html(t):
    if t["turbo_active"]:
        wl = t["turbo_weeks_left"]; wd = t["turbo_weeks_done"]
        rd = t["turbo_reclaim_date"]
        bar_pct = int((wd / t["turbo_duration"]) * 100)
        return f"""
 <div style="background:#1a2a1a;border:2px solid #00C853;border-radius:6px;padding:16px;margin:16px 0;">
  <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">🚀 Turbo Mode — ACTIVE</div>
  <div style="font-size:22px;font-weight:bold;color:#00C853;">Week {wd} of {t['turbo_duration']}</div>
  <div style="font-size:13px;color:#888;margin-top:4px;">{wl} weeks remaining | Reclaim: {rd}</div>
  <div style="margin-top:10px;background:#0a0a0a;border-radius:4px;height:8px;overflow:hidden;">
   <div style="background:#00C853;height:100%;width:{bar_pct}%;border-radius:4px;"></div>
  </div>
 </div>"""
    elif not t["turbo_active"] and t["weeks_below_now"] > 0:
        wb = t["weeks_below_now"]; thr = t["trigger_threshold"]
        remaining = max(0, thr - wb)
        bar_pct   = int(min(100, (wb / thr) * 100))
        bar_color = "#FFC107" if remaining == 0 else "#FF6B35"
        status_line = (
            "⏳ Threshold reached — waiting for 200MA reclaim" if remaining == 0
            else f"⏳ {remaining} more week{'s' if remaining!=1 else ''} below to arm turbo"
        )
        return f"""
 <div style="background:#1a1a0a;border:2px solid {bar_color};border-radius:6px;padding:16px;margin:16px 0;">
  <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">⏳ Turbo Mode — Arming</div>
  <div style="font-size:22px;font-weight:bold;color:{bar_color};">{wb} / {thr} weeks below</div>
  <div style="font-size:13px;color:#888;margin-top:4px;">{status_line}</div>
  <div style="margin-top:10px;background:#0a0a0a;border-radius:4px;height:8px;overflow:hidden;">
   <div style="background:{bar_color};height:100%;width:{bar_pct}%;border-radius:4px;"></div>
  </div>
 </div>"""
    else:
        last_rd   = t.get("turbo_reclaim_date")
        last_note = f"Last turbo: {last_rd}" if last_rd else "No turbo events yet"
        return f"""
 <div style="background:#1a1a1a;border:1px solid #2a2a2a;border-radius:6px;padding:14px;margin:16px 0;">
  <div style="font-size:11px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">Turbo Mode — Standby</div>
  <div style="font-size:14px;color:#555;">QQQ above 200MA — vol-sized TQQQ position active.</div>
  <div style="font-size:11px;color:#444;margin-top:4px;">{last_note} | Triggers after {TURBO_MIN_WEEKS_BELOW}+ weeks below</div>
 </div>"""

def turbo_status_text(t):
    if t["turbo_active"]:
        return (f"🚀 TURBO ACTIVE: Week {t['turbo_weeks_done']} of {t['turbo_duration']} "
                f"({t['turbo_weeks_left']} weeks left) — reclaim {t['turbo_reclaim_date']}")
    elif t["weeks_below_now"] > 0:
        rem = max(0, t["trigger_threshold"] - t["weeks_below_now"])
        if rem == 0:
            return f"⏳ TURBO ARMED: {t['weeks_below_now']} weeks below — waiting for 200MA reclaim"
        return f"⏳ TURBO ARMING: {t['weeks_below_now']}/{t['trigger_threshold']} weeks below"
    else:
        return "Turbo standby — QQQ above 200MA, vol-sized position active"

# -------------------------
# Email subject
# -------------------------
turbo_tag = ""
if turbo["turbo_active"]:
    turbo_tag = f" | 🚀 TURBO Wk{turbo['turbo_weeks_done']}/{turbo['turbo_duration']}"
elif turbo["weeks_below_now"] >= TURBO_MIN_WEEKS_BELOW:
    turbo_tag = " | ⏳ TURBO ARMED"
elif turbo["weeks_below_now"] > 0:
    turbo_tag = f" | Arming {turbo['weeks_below_now']}/{TURBO_MIN_WEEKS_BELOW}wks"

# Show guard status in subject when active
guard_tag = " | ⚡ GUARD" if vol_accelerating else ""

gate_str  = "Above 200MA" if above_200ma else "Below 200MA"
alloc_str = f"{alloc_final_pct}% TQQQ" if alloc_final_pct > 0 else "0% TQQQ / 100% SGOV"

email_subject = (
    f"TQQQ | Vol({int(TARGET_VOL*100)}%)+200MA | {alloc_str} | "
    f"Vol20={vol_pct:.1f}% | QQQ {gate_str}{turbo_tag}{guard_tag}"
)

# -------------------------
# Colors + display helpers
# -------------------------
gate_color  = "#00C853" if above_200ma else "#FF1744"
gate_label  = "✅ ABOVE 200MA — Vol-sized TQQQ" if above_200ma else "🔴 BELOW 200MA — 100% SGOV"
action_color = (
    "#00C853" if "BUY"  in action_label else
    "#FF1744" if "SELL" in action_label else
    "#FFC107"
)

ma50_str  = f"{ma50:.2f}"  if ma50  is not None else "N/A"
ma100_str = f"{ma100:.2f}" if ma100 is not None else "N/A"
ma200_str = f"{ma200:.2f}" if ma200 is not None else "N/A"
rsi_str   = f"{rsi:.1f}"   if rsi   is not None else "N/A"
macd_str  = ("Bullish ▲" if macd["bullish"] else "Bearish ▼" if macd["bullish"] is False else "N/A")
macd_color = "#00C853" if macd["bullish"] else ("#FF1744" if macd["bullish"] is False else "#888")
prev_str  = f"{prev_alloc_pct}%" if prev_alloc_pct is not None else "—"

# Vol explanation with guard status
if above_200ma:
    vol_explain = (
        f"TQQQ 20d vol = {vol_pct:.1f}% annualized → "
        f"target {int(TARGET_VOL*100)}% portfolio vol → "
        f"base alloc = {int(TARGET_VOL*100)}% ÷ {vol_pct:.1f}% = {int(round(alloc_base*100))}% TQQQ"
    )
    if vol_accelerating:
        vol_explain += (
            f" → ⚡ Vol Accel Guard fired "
            f"(vol rose from {vol_5d_pct:.1f}% to {vol_pct:.1f}%, "
            f"{((vol_ann/vol_ann_5d_ago-1)*100):.0f}% in 5d, above {int(VOL_ACCEL_FLOOR*100)}% floor) "
            f"→ capped at {int(VOL_ACCEL_CAP*100)}%"
        )
else:
    vol_explain = "QQQ below 200MA — vol sizing overridden, 100% SGOV"

# Guard HTML block (only shown when relevant)
guard_html = ""
if vol_accelerating:
    guard_html = f"""
 <div style="background:#1a1500;border:2px solid #FFC107;border-radius:6px;padding:14px;margin:16px 0;">
  <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">⚡ Vol Acceleration Guard — ACTIVE</div>
  <div style="font-size:16px;font-weight:bold;color:#FFC107;">Allocation capped at {int(VOL_ACCEL_CAP*100)}% TQQQ</div>
  <div style="font-size:13px;color:#888;margin-top:6px;">
   Vol rose from {vol_5d_pct:.1f}% → {vol_pct:.1f}% ({((vol_ann/vol_ann_5d_ago-1)*100):.0f}% in ~5 trading days)<br>
   Base alloc would have been {int(round(alloc_base*100))}% — guard reduced to {int(VOL_ACCEL_CAP*100)}%
  </div>
 </div>"""
elif above_200ma and vol_ann_5d_ago > 0:
    accel_ratio = vol_ann / vol_ann_5d_ago
    guard_html = f"""
 <div style="background:#111;border:1px solid #222;border-radius:6px;padding:12px;margin:16px 0;">
  <div style="font-size:11px;color:#444;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">⚡ Vol Acceleration Guard — Standby</div>
  <div style="font-size:13px;color:#555;">
   Vol trend: {vol_5d_pct:.1f}% → {vol_pct:.1f}% ({accel_ratio:.2f}x in ~5d)
   — guard fires if vol rises {int((VOL_ACCEL_THRESHOLD-1)*100)}%+ AND exceeds {int(VOL_ACCEL_FLOOR*100)}%
  </div>
 </div>"""

# -------------------------
# HTML report
# -------------------------
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
report_path = REPORTS_DIR / f"{run_date_str}.html"

html = f"""<!DOCTYPE html>
<html>
<head>
 <meta charset="utf-8">
 <title>TQQQ Report {run_date_str}</title>
 <style>
  body {{ background:#0d0d0d; color:#e0e0e0; font-family:'Courier New',monospace; padding:24px; max-width:700px; margin:auto; }}
  h1   {{ color:#00e5ff; font-size:18px; letter-spacing:2px; border-bottom:1px solid #333; padding-bottom:8px; }}
  .section {{ margin:18px 0; }}
  .label   {{ color:#888; font-size:12px; text-transform:uppercase; letter-spacing:1px; }}
  .big     {{ font-size:28px; font-weight:bold; }}
  .grid    {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
  .card    {{ background:#1a1a1a; border:1px solid #2a2a2a; border-radius:6px; padding:12px; }}
  .card .v {{ font-size:16px; font-weight:bold; color:#e0e0e0; margin-top:4px; }}
  .note    {{ font-size:11px; color:#555; margin-top:4px; }}
  .action-box  {{ background:#1a1a1a; border:2px solid {action_color}; border-radius:6px; padding:16px; margin:16px 0; }}
  .vol-box     {{ background:#0a1a2a; border:1px solid #1a3a5a; border-radius:6px; padding:14px; margin:16px 0; font-size:13px; color:#7ab8e0; }}
  .strategy-note {{ background:#111; border:1px solid #333; border-radius:6px; padding:12px; margin:16px 0; font-size:12px; color:#888; }}
 </style>
</head>
<body>
<h1>TQQQ WEEKLY SIGNAL — {run_date_str}</h1>

<div class="section">
 <div class="label">Strategy</div>
 <div style="font-size:14px; color:#00e5ff; margin-top:4px;">Vol({int(TARGET_VOL*100)}%) + 200MA Gate + Vol Acceleration Guard</div>
 <div class="note">QQQ above 200MA: size TQQQ to {int(TARGET_VOL*100)}% target vol &nbsp;|&nbsp; Below 200MA: 100% SGOV</div>
 <div class="note">Guard: if TQQQ vol rises {int((VOL_ACCEL_THRESHOLD-1)*100)}%+ in 5 days AND vol &gt; {int(VOL_ACCEL_FLOOR*100)}% → cap at {int(VOL_ACCEL_CAP*100)}%</div>
</div>

<div class="section">
 <div class="label">200MA Gate</div>
 <div class="big" style="color:{gate_color}; margin-top:6px;">{gate_label}</div>
 <div style="margin-top:4px; font-size:13px; color:#888;">
  QQQ: ${qqq_close:.2f} &nbsp;|&nbsp; 200MA: {ma200_str} &nbsp;|&nbsp; Distance: {ma_dist(qqq_close, ma200)}
 </div>
</div>

<div class="vol-box">
 <div class="label" style="color:#4a8ab0;">Vol Sizing Calculation</div>
 <div style="margin-top:6px;">{vol_explain}</div>
</div>

{guard_html}

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
 <div style="font-size:13px; color:#888; margin-top:6px;">Target: {alloc_final_pct}% TQQQ / {cash_pct}% SGOV</div>
</div>

{turbo_status_html(turbo)}

<div class="section">
 <div class="label">QQQ Moving Averages</div>
 <div class="grid" style="margin-top:8px;">
  <div class="card">
   <div class="label">50-day MA</div>
   <div class="v" style="color:{'#00C853' if ma50 and qqq_close>ma50 else '#FF1744'}">{ma50_str}</div>
   <div class="note">QQQ {ma_dist(qqq_close, ma50)} from 50MA</div>
  </div>
  <div class="card">
   <div class="label">100-day MA</div>
   <div class="v" style="color:{'#00C853' if ma100 and qqq_close>ma100 else '#FF1744'}">{ma100_str}</div>
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
   <div class="note">{'Oversold (<35)' if rsi and rsi<35 else 'Overbought (>65)' if rsi and rsi>65 else 'Neutral'}</div>
  </div>
 </div>
</div>

<div class="section">
 <div class="label">Additional Signals (informational)</div>
 <div class="grid" style="margin-top:8px;">
  <div class="card">
   <div class="label">MACD</div>
   <div class="v" style="color:{macd_color}">{macd_str}</div>
   <div class="note">Line: {macd['macd_line']} / Signal: {macd['signal_line']}</div>
  </div>
  <div class="card">
   <div class="label">Realized Vol (20d)</div>
   <div class="v">{vol_pct:.1f}%</div>
   <div class="note">5d ago: {vol_5d_pct:.1f}% | Target: {int(TARGET_VOL*100)}% → Alloc: {alloc_final_pct}%</div>
  </div>
 </div>
</div>

<div class="strategy-note">
 <strong style="color:#777;">Vol({int(TARGET_VOL*100)}%) + 200MA + Guard:</strong>
 When QQQ is above its 200-day MA, TQQQ is sized so the portfolio targets {int(TARGET_VOL*100)}% annualized volatility.
 The Vol Acceleration Guard caps allocation at {int(VOL_ACCEL_CAP*100)}% if vol spikes {int((VOL_ACCEL_THRESHOLD-1)*100)}%+ in 5 days while already above {int(VOL_ACCEL_FLOOR*100)}% — adding ~+0.8% CAGR with improved drawdown protection.
 When QQQ drops below the 200MA, everything moves to SGOV.
 Backtest 2017–present: +33.2% CAGR / -31.8% max DD.
</div>

<div style="font-size:11px; color:#333; margin-top:24px; border-top:1px solid #1a1a1a; padding-top:8px;">
 Generated {run_date_str} | Mode: {MODE.upper()} | Strategy: Vol({int(TARGET_VOL*100)}%)+200MA+Guard | TQQQ: ${tqqq_close:.2f} | QQQ: ${qqq_close:.2f}
</div>
</body>
</html>"""

with open(report_path, "w") as f:
    f.write(html)

# Write metadata files for workflow email
report_url = PAGES_BASE_URL + "/reports/" + run_date_str + ".html"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
with open(OUTPUT_DIR / "latest_report_url.txt", "w") as f:
    f.write(report_url)
with open(OUTPUT_DIR / "subject.txt", "w") as f:
    f.write(email_subject)

email_body = "\n".join([
    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
    "  GOGO TQQQ SIGNAL \u2014 " + run_date_str,
    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
    "",
    "  ALLOCATION:  " + str(alloc_final_pct) + "% TQQQ / " + str(cash_pct) + "% SGOV",
    "  ACTION:      " + action_label,
    "  PREVIOUS:    " + (prev_str if prev_str else "\u2014"),
    "",
    "  200MA GATE:  " + ("\u2713 ABOVE" if above_200ma else "\u2717 BELOW") + "  (QQQ $" + str(round(qqq_close,2)) + " / MA $" + ma200_str + ")",
    "  VOL (20d):   " + str(round(vol_pct,1)) + "%  (5d ago: " + str(round(vol_5d_pct,1)) + "%)",
    "  FORMULA:     " + str(int(TARGET_VOL*100)) + "% \u00f7 " + str(round(vol_pct,1)) + "% = " + str(alloc_final_pct) + "%",
    "  GUARD:       " + ("\u26a1 FIRED \u2014 capped at 50%" if vol_accelerating else "Standby"),
    "",
    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
    "  " + report_url,
    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
])
with open(OUTPUT_DIR / "message.txt", "w") as f:
    f.write(email_body)


# ── HTML email body ─────────────────────────────────────────────────────────
alloc_color  = '#00C853' if alloc_final_pct >= 80 else '#FFC107' if alloc_final_pct >= 40 else '#FF6D00'
gate_bg      = '#0d1f0d' if above_200ma else '#1f0d0d'
gate_border  = '#00C853' if above_200ma else '#FF1744'
action_bg    = '#0d1f0d' if 'HOLD' in action_label else '#1f1500' if 'BUY' in action_label else '#1f0d0d'
action_color = '#00C853' if 'HOLD' in action_label else '#FFC107' if 'BUY' in action_label else '#FF6D00'

guard_email_html = ''
if vol_accelerating:
    guard_email_html = (
        '<tr><td style="padding:0 0 16px 0;">'
        '<div style="background:#1a1500;border:2px solid #FFC107;border-radius:8px;padding:16px;text-align:center;">'
        '<div style="font-size:11px;color:#888;text-transform:uppercase;">Vol Acceleration Guard — ACTIVE</div>'
        f'<div style="font-size:20px;font-weight:bold;color:#FFC107;">Allocation capped at {int(VOL_ACCEL_CAP*100)}% TQQQ</div>'
        f'<div style="font-size:13px;color:#888;margin-top:6px;">Vol rose {vol_5d_pct:.1f}% to {vol_pct:.1f}%</div>'
        '</div></td></tr>'
    )
elif above_200ma and vol_ann_5d_ago > 0:
    accel_pct = (vol_ann / vol_ann_5d_ago - 1) * 100
    guard_email_html = (
        '<tr><td style="padding:0 0 16px 0;">'
        '<div style="background:#111;border:1px solid #222;border-radius:8px;padding:12px;text-align:center;">'
        '<div style="font-size:11px;color:#444;text-transform:uppercase;">Vol Accel Guard — Standby</div>'
        f'<div style="font-size:12px;color:#555;margin-top:4px;">Vol {vol_5d_pct:.1f}% to {vol_pct:.1f}% ({accel_pct:+.0f}% in 5d)</div>'
        '</div></td></tr>'
    )

ma_rows_html = ''
for label, val, raw in [('50-day MA', ma50_str, ma50), ('100-day MA', ma100_str, ma100), ('200-day MA', ma200_str, ma200)]:
    if val == 'N/A' or raw is None:
        continue
    dist = (qqq_close - raw) / raw * 100
    dist_color = '#00C853' if dist > 0 else '#FF1744'
    dist_str = f'+{dist:.1f}%' if dist > 0 else f'{dist:.1f}%'
    ma_rows_html += (
        f'<tr><td style="font-size:12px;color:#555;padding:4px 0;">{label}</td>'
        f'<td style="font-size:12px;color:#888;text-align:right;">${val}</td>'
        f'<td style="font-size:12px;color:{dist_color};text-align:right;width:70px;">{dist_str}</td></tr>'
    )

email_html = (
    '<!DOCTYPE html><html>'
    '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>'
    '<body style="margin:0;padding:0;background:#0a0a0a;font-family:-apple-system,BlinkMacSystemFont,sans-serif;">'
    '<table width="100%" cellpadding="0" cellspacing="0" style="background:#0a0a0a;padding:24px 16px;"><tr><td>'
    '<table width="100%" cellpadding="0" cellspacing="0" style="max-width:580px;margin:0 auto;">'
    '<tr><td style="padding:0 0 20px 0;">'
    '<table width="100%" cellpadding="0" cellspacing="0"><tr>'
    '<td><span style="font-size:22px;font-weight:bold;color:#f0c040;">GOGO</span>'
    '<span style="font-size:13px;color:#555;margin-left:8px;">TQQQ Vol-Targeting Signal</span></td>'
    f'<td align="right"><span style="font-size:12px;color:#555;">{run_date_str}</span></td>'
    '</tr></table>'
    '<div style="height:2px;background:#f0c040;margin-top:8px;opacity:0.3;"></div>'
    '</td></tr>'
    '<tr><td style="padding:0 0 16px 0;">'
    f'<div style="background:{gate_bg};border:1px solid {gate_border};border-radius:8px;padding:14px 16px;text-align:center;">'
    f'<div style="font-size:13px;color:{gate_border};font-weight:600;">{gate_label}</div>'
    f'<div style="font-size:12px;color:#666;margin-top:4px;">QQQ ${qqq_close:.2f} &nbsp;·&nbsp; 200MA ${ma200_str} &nbsp;·&nbsp; {ma_dist(qqq_close, ma200)}</div>'
    '</div></td></tr>'
    '<tr><td style="padding:0 0 16px 0;">'
    '<div style="background:#111;border:1px solid #222;border-radius:8px;padding:20px;text-align:center;">'
    '<div style="font-size:11px;color:#555;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px;">This Week\'s Allocation</div>'
    f'<div style="font-size:56px;font-weight:900;color:{alloc_color};line-height:1;">{alloc_final_pct}%</div>'
    f'<div style="font-size:18px;color:#888;margin-top:4px;">TQQQ &nbsp;/&nbsp; <span style="color:#4da6ff;">{cash_pct}% SGOV</span></div>'
    '<div style="height:8px;background:#1a1a1a;border-radius:4px;margin:14px 0 8px 0;overflow:hidden;">'
    f'<div style="height:100%;width:{alloc_final_pct}%;background:{alloc_color};border-radius:4px;"></div></div>'
    f'<div style="background:{action_bg};border-radius:6px;padding:8px 16px;margin-top:8px;">'
    f'<span style="font-size:14px;font-weight:700;color:{action_color};">{action_label}</span></div>'
    f'<div style="font-size:12px;color:#444;margin-top:8px;">Previous: {prev_str}</div>'
    '</div></td></tr>'
    '<tr><td style="padding:0 0 16px 0;">'
    '<div style="background:#111;border:1px solid #1a1a1a;border-radius:8px;padding:14px 16px;">'
    '<div style="font-size:11px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px;">Vol Sizing</div>'
    '<table width="100%" cellpadding="0" cellspacing="0"><tr>'
    f'<td style="text-align:center;padding:8px;"><div style="font-size:22px;font-weight:700;color:#e8e8e8;">{vol_pct:.1f}%</div><div style="font-size:11px;color:#555;">20d Vol</div></td>'
    '<td style="text-align:center;color:#333;font-size:20px;">÷</td>'
    f'<td style="text-align:center;padding:8px;"><div style="font-size:22px;font-weight:700;color:#e8e8e8;">{int(TARGET_VOL*100)}%</div><div style="font-size:11px;color:#555;">Target</div></td>'
    '<td style="text-align:center;color:#333;font-size:20px;">=</td>'
    f'<td style="text-align:center;padding:8px;"><div style="font-size:22px;font-weight:700;color:{alloc_color};">{alloc_final_pct}%</div><div style="font-size:11px;color:#555;">TQQQ</div></td>'
    '</tr></table>'
    f'<div style="font-size:11px;color:#444;margin-top:8px;text-align:center;">5d ago: {vol_5d_pct:.1f}% &nbsp;·&nbsp; RSI {rsi_str} &nbsp;·&nbsp; MACD <span style="color:{macd_color};">{macd_str}</span></div>'
    '</div></td></tr>'
    + guard_email_html +
    '<tr><td style="padding:0 0 16px 0;">'
    '<div style="background:#111;border:1px solid #1a1a1a;border-radius:8px;padding:14px 16px;">'
    '<div style="font-size:11px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px;">QQQ Moving Averages</div>'
    f'<table width="100%" cellpadding="0" cellspacing="6">{ma_rows_html}</table>'
    '</div></td></tr>'
    '<tr><td style="padding:0 0 20px 0;text-align:center;">'
    f'<a href="{report_url}" style="display:inline-block;background:#f0c040;color:#0a0a0a;font-size:14px;font-weight:700;padding:12px 32px;border-radius:6px;text-decoration:none;">Open Full Report</a>'
    '</td></tr>'
    '<tr><td style="border-top:1px solid #1a1a1a;padding-top:16px;text-align:center;">'
    f'<div style="font-size:11px;color:#333;line-height:1.7;">Strategy: Vol({int(TARGET_VOL*100)}%) + 200MA Gate + Vol Accel Guard<br>Runs every Friday after close &nbsp;·&nbsp; Act Monday morning<br>Generated {run_date_str} &nbsp;·&nbsp; Mode: {MODE}</div>'
    '</td></tr>'
    '</table></td></tr></table></body></html>'
)
# Write HTML email
with open(OUTPUT_DIR / "email_html.html", "w") as f:
    f.write(email_html)

# -------------------------
# Console output
# -------------------------
print(f"Subject: {email_subject}")
print(f"Date: {run_date_str} | Mode: {MODE} | Strategy: Vol({int(TARGET_VOL*100)}%)+200MA+Guard")
print()
print(f"200MA Gate: {'ABOVE ✅' if above_200ma else 'BELOW 🔴'}")
print(f"QQQ: ${qqq_close:.2f} | 200MA: {ma200_str} | Dist: {ma_dist(qqq_close, ma200)}")
print()
print(f"VOL SIZING: {vol_explain}")
print(f"Vol 5d ago: {vol_5d_pct:.1f}%  →  Today: {vol_pct:.1f}%  →  Guard: {'⚡ FIRED' if vol_accelerating else 'standby'}")
print(f"ALLOCATION: {alloc_final_pct}% TQQQ / {cash_pct}% SGOV")
print(f"Previous: {prev_str} | Action: {action_label}")
print()
print(f"--- TURBO MODE ---")
print(f"{turbo_status_text(turbo)}")
print()
print(f"--- Informational ---")
print(f"Vol (20d): {vol_pct:.1f}% | Target: {int(TARGET_VOL*100)}% | Alloc: {alloc_final_pct}%")
print(f"RSI: {rsi_str} | MACD: {macd_str}")
print(f"50MA: {ma50_str} ({ma_dist(qqq_close,ma50)}) | 100MA: {ma100_str}")
print()
print(f"Report: {report_path}")
