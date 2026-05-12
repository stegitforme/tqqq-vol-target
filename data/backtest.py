#!/usr/bin/env python3
"""
TQQQ Strategy Falsification Backtest
=====================================

LOCKED DESIGN — DO NOT TUNE PARAMETERS AFTER SEEING RESULTS.

This script tests 9 strategies against TQQQ/QQQ historical data to determine
whether any hedge-fund-style enhancement to the current Vol(35%)+200MA
strategy passes a strict 4-condition decision rule across ≥4 of 6 regimes.

Strategies:
    1. Buy & Hold QQQ              (baseline)
    2. Buy & Hold TQQQ             (baseline)
    3. Vol(35%) + 200MA            (current — baseline)
    4. A: 100MA Partial Re-Entry
    5. B: Momentum Thrust Re-Entry (your "breadth thrust" proxy)
    6. C: Vol Acceleration Exit Overlay
    7. D: Dual-Speed Trend (200MA + 50MA, NOT 20MA)
    8. E: Combo1 — B + C
    9. F: Combo2 — A + C

Author: Claude (designed with Steven, falsification framework locked 2026-05-12)
"""

import csv
import math
import sys
import os
from datetime import date, datetime, timedelta
from collections import defaultdict

# ============================================================
# CONFIG — LOCKED. DO NOT MODIFY AFTER RESULTS.
# ============================================================

# Strategy parameters (standard defaults — no tuning)
TARGET_VOL = 0.35              # 35% annualized vol target
VOL_LOOKBACK_DAYS = 20         # 20-day realized vol
MA_FAST = 50                   # 50-day moving average
MA_MED = 100                   # 100-day moving average
MA_SLOW = 200                  # 200-day moving average
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
SGOV_ANNUAL_YIELD = 0.045      # ~4.5% cash yield assumption

# Vol acceleration overlay thresholds (Variant C)
VOL_CAP_55 = 0.55              # Above 55% → cap at 50% of normal
VOL_CAP_75 = 0.75              # Above 75% → cap at 25%
VOL_CAP_95 = 0.95              # Above 95% → cash

# Momentum Thrust (Variant B, renamed from "breadth thrust")
MT_DRAWDOWN_TRIGGER = 0.15     # Must be down >15% from recent high
MT_RECOVERY_THRESHOLD = 0.08   # 10-day return > +8%
MT_LOOKBACK = 60               # Recent high lookback window (60 days)

# Trading costs
COMMISSION_BPS = 1             # 1bp Fidelity-realistic
SLIPPAGE_BPS = 5               # 5bp round-trip slippage

# Trading frequency
SIGNAL_WEEKDAY = 4             # Friday=4 (0=Mon, 6=Sun)

# Tolerance for "borderline" in decision rule (Steven's clarification):
# A variant misses a condition by >50% of its slack → real fail
# ≤50% of slack → borderline (still passes)
BORDERLINE_SLACK_PCT = 0.50

# Decision rule slack (must IMPROVE OR not worsen by more than these amounts):
CAGR_SLACK = 2.0               # CAGR not worse by more than 2pp
MAX_DD_SLACK = 3.0             # Max DD not worse by more than 3pp
WHIPSAW_SLACK_MULT = 1.5       # Whipsaws not more than 50% worse


# ============================================================
# REGIME DEFINITIONS — LOCKED
# ============================================================

REGIMES = [
    ('2010-2014_recovery', '2010-02-11', '2014-12-31'),  # TQQQ inception was 2010-02-11
    ('2015-2016_chop',     '2015-01-01', '2016-12-31'),
    ('2018_Q4_crash',      '2018-10-01', '2018-12-31'),
    ('2020_COVID',         '2020-02-01', '2020-12-31'),
    ('2022_ratehike_bear', '2022-01-01', '2022-12-31'),
    ('2023-2024_AI_bull',  '2023-01-01', '2024-12-31'),
    ('2025-2026_YTD',      '2025-01-01', '2099-12-31'),  # excluded from decision
]
DECISION_REGIMES = [r[0] for r in REGIMES if r[0] != '2025-2026_YTD']


# ============================================================
# DATA LOADING
# ============================================================

def load_csv(path):
    """Load Date, Close CSV. Returns list of (date_str, close) sorted by date."""
    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        # Auto-detect close column
        sample = next(reader)
        close_col = None
        for col in ['Close', 'close', 'Adj Close', 'AdjClose', 'adjclose']:
            if col in sample:
                close_col = col
                break
        if close_col is None:
            raise ValueError(f"No Close column in {path}. Headers: {list(sample.keys())}")
        rows.append((sample['Date'][:10], float(sample[close_col])))
        for row in reader:
            try:
                rows.append((row['Date'][:10], float(row[close_col])))
            except (ValueError, KeyError):
                continue
    rows.sort()
    return rows


def parse_date(s):
    return date.fromisoformat(s)


def is_friday(date_str):
    return parse_date(date_str).weekday() == SIGNAL_WEEKDAY


# ============================================================
# INDICATORS
# ============================================================

def sma(values, period, end_idx):
    """Simple moving average ending at end_idx (inclusive)."""
    if end_idx < period - 1:
        return None
    return sum(values[end_idx - period + 1: end_idx + 1]) / period


def ema(values, period):
    """Exponential moving average as a list parallel to values."""
    if len(values) < period:
        return [None] * len(values)
    result = [None] * (period - 1)
    # Seed with SMA
    seed = sum(values[:period]) / period
    result.append(seed)
    k = 2 / (period + 1)
    for i in range(period, len(values)):
        result.append(values[i] * k + result[-1] * (1 - k))
    return result


def compute_rsi(closes, period=RSI_PERIOD):
    """RSI(14) — returns list parallel to closes."""
    if len(closes) < period + 1:
        return [None] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    rsi = [None]  # No RSI for index 0
    # Wilder's smoothing
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for _ in range(period):
        rsi.append(None)
    if avg_loss == 0:
        rsi.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi.append(100 - 100 / (1 + rs))
    for i in range(period + 1, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi.append(100 - 100 / (1 + rs))
    return rsi


def compute_macd(closes):
    """MACD line, signal line. Returns (macd_list, signal_list)."""
    ema_fast = ema(closes, MACD_FAST)
    ema_slow = ema(closes, MACD_SLOW)
    macd_line = []
    for i in range(len(closes)):
        if ema_fast[i] is None or ema_slow[i] is None:
            macd_line.append(None)
        else:
            macd_line.append(ema_fast[i] - ema_slow[i])
    # Signal line = EMA(9) of MACD line
    macd_clean = [v for v in macd_line if v is not None]
    signal_clean = ema(macd_clean, MACD_SIGNAL)
    none_count = len(macd_line) - len(macd_clean)
    signal_line = [None] * none_count + signal_clean
    return macd_line, signal_line


def compute_realized_vol(closes, period=VOL_LOOKBACK_DAYS):
    """Annualized realized vol from daily log returns."""
    result = [None] * len(closes)
    log_returns = [None]
    for i in range(1, len(closes)):
        log_returns.append(math.log(closes[i] / closes[i-1]))
    for i in range(period, len(closes)):
        window = log_returns[i - period + 1: i + 1]
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / (period - 1)
        result[i] = math.sqrt(var * 252)
    return result


def compute_drawdown_from_recent_high(closes, lookback=MT_LOOKBACK):
    """% drawdown from rolling N-day high."""
    result = [None] * len(closes)
    for i in range(lookback, len(closes)):
        window_high = max(closes[i - lookback: i + 1])
        result[i] = (closes[i] - window_high) / window_high
    return result


def compute_n_day_return(closes, n):
    result = [None] * len(closes)
    for i in range(n, len(closes)):
        result[i] = (closes[i] - closes[i - n]) / closes[i - n]
    return result


# ============================================================
# STRATEGY IMPLEMENTATIONS
# Each strategy returns target_alloc (0.0 to ~2.0+) for TQQQ on a given day.
# 1.0 = full vol-target allocation (which may be >100% mathematically but
# capped at 100% practically since you can't short SGOV to buy more TQQQ).
# ============================================================

def vol_target_allocation(qqq_vol):
    """Base vol-target: cap at 100% (no shorting)."""
    if qqq_vol is None or qqq_vol <= 0:
        return 0.0
    return min(TARGET_VOL / qqq_vol, 1.0)


def strat_bh_qqq(state):
    """Buy & Hold QQQ — but we trade TQQQ. So this gets 0% TQQQ + tracks QQQ separately."""
    return ('QQQ_HOLD', None)  # Handled specially in equity curve


def strat_bh_tqqq(state):
    return ('TQQQ_FULL', 1.0)  # Always 100% TQQQ


def strat_vol_200ma(state):
    """Current production strategy."""
    if state['qqq_close'] is None or state['ma200'] is None:
        return ('IDLE', 0.0)
    if state['qqq_close'] > state['ma200']:
        return ('VOL_TARGET', vol_target_allocation(state['vol']))
    return ('CASH', 0.0)


def strat_A_100ma_partial(state):
    """100MA partial re-entry overlay."""
    if state['qqq_close'] is None or state['ma200'] is None or state['ma100'] is None:
        return ('IDLE', 0.0)
    vt = vol_target_allocation(state['vol'])
    if state['qqq_close'] > state['ma200']:
        return ('FULL', vt)
    if state['qqq_close'] > state['ma100']:
        return ('PARTIAL_40', vt * 0.40)
    return ('CASH', 0.0)


def strat_B_momentum_thrust(state):
    """Momentum Thrust Re-Entry (Steven's 'breadth thrust' proxy).
    
    When QQQ is below 200MA, allow partial re-entry if:
    - QQQ is down >15% from 60-day high, AND
    - QQQ 10-day return > +8%, AND
    - QQQ closes above 20MA
    
    Allocation: 50% of vol-target until 200MA reclaimed.
    """
    if state['qqq_close'] is None or state['ma200'] is None or state['ma20'] is None:
        return ('IDLE', 0.0)
    vt = vol_target_allocation(state['vol'])
    if state['qqq_close'] > state['ma200']:
        return ('FULL', vt)
    # Below 200MA — check momentum thrust
    if (state['dd_60d'] is not None and state['ret_10d'] is not None
        and state['dd_60d'] < -MT_DRAWDOWN_TRIGGER
        and state['ret_10d'] > MT_RECOVERY_THRESHOLD
        and state['qqq_close'] > state['ma20']):
        return ('MOMENTUM_THRUST_50', vt * 0.50)
    return ('CASH', 0.0)


def strat_C_vol_acceleration(state):
    """Vol Acceleration Exit Overlay.
    
    Applies caps to current Vol+200MA when realized vol explodes.
    """
    if state['qqq_close'] is None or state['ma200'] is None or state['vol'] is None:
        return ('IDLE', 0.0)
    if state['qqq_close'] <= state['ma200']:
        return ('CASH', 0.0)
    vt = vol_target_allocation(state['vol'])
    if state['vol'] > VOL_CAP_95:
        return ('VOL_PANIC', 0.0)
    if state['vol'] > VOL_CAP_75:
        return ('VOL_HIGH_25', min(vt, vt * 0.25))
    if state['vol'] > VOL_CAP_55:
        return ('VOL_ELEV_50', min(vt, vt * 0.50))
    return ('FULL', vt)


def strat_D_dual_speed(state):
    """Dual-Speed Trend Overlay.
    
    NOTE: Modified from spec to use 50MA instead of 20MA.
    20MA was a whipsaw machine; 50MA is medium-term signal.
    """
    if state['qqq_close'] is None or state['ma200'] is None or state['ma50'] is None:
        return ('IDLE', 0.0)
    if state['qqq_close'] <= state['ma200']:
        return ('CASH', 0.0)
    vt = vol_target_allocation(state['vol'])
    if state['qqq_close'] > state['ma50']:
        return ('FULL', vt)
    return ('DUAL_REDUCED_60', vt * 0.60)


def strat_E_combo_B_plus_C(state):
    """Combo 1: Momentum Thrust + Vol Acceleration.
    
    Priority: vol panic cap overrides everything.
    Then: full = trend OK + below vol cap.
    Else: momentum thrust re-entry if conditions met.
    """
    if state['qqq_close'] is None or state['ma200'] is None or state['vol'] is None:
        return ('IDLE', 0.0)
    vt = vol_target_allocation(state['vol'])
    # Vol panic always wins
    if state['vol'] > VOL_CAP_95:
        return ('VOL_PANIC', 0.0)
    # Above 200MA — apply vol caps to vol-target
    if state['qqq_close'] > state['ma200']:
        if state['vol'] > VOL_CAP_75:
            return ('VOL_HIGH_25', vt * 0.25)
        if state['vol'] > VOL_CAP_55:
            return ('VOL_ELEV_50', vt * 0.50)
        return ('FULL', vt)
    # Below 200MA — check momentum thrust (with vol caps still applying)
    if (state['dd_60d'] is not None and state['ret_10d'] is not None and state['ma20'] is not None
        and state['dd_60d'] < -MT_DRAWDOWN_TRIGGER
        and state['ret_10d'] > MT_RECOVERY_THRESHOLD
        and state['qqq_close'] > state['ma20']):
        # Apply vol caps to thrust allocation too
        base = vt * 0.50
        if state['vol'] > VOL_CAP_75:
            return ('THRUST_VOL_25', base * 0.5)  # thrust×0.5×vol-cap-0.5
        if state['vol'] > VOL_CAP_55:
            return ('THRUST_VOL_50', base * 0.75)
        return ('MOMENTUM_THRUST_50', base)
    return ('CASH', 0.0)


def strat_F_combo_A_plus_C(state):
    """Combo 2: 100MA Partial + Vol Acceleration.
    
    Simpler institutional variant — no momentum signals, just trend layers + vol cap.
    """
    if state['qqq_close'] is None or state['ma200'] is None or state['ma100'] is None or state['vol'] is None:
        return ('IDLE', 0.0)
    vt = vol_target_allocation(state['vol'])
    # Vol panic always wins
    if state['vol'] > VOL_CAP_95:
        return ('VOL_PANIC', 0.0)
    if state['qqq_close'] > state['ma200']:
        if state['vol'] > VOL_CAP_75:
            return ('VOL_HIGH_25', vt * 0.25)
        if state['vol'] > VOL_CAP_55:
            return ('VOL_ELEV_50', vt * 0.50)
        return ('FULL', vt)
    if state['qqq_close'] > state['ma100']:
        base = vt * 0.40
        if state['vol'] > VOL_CAP_75:
            return ('PARTIAL_VOL_HIGH', base * 0.25 / 0.40)
        if state['vol'] > VOL_CAP_55:
            return ('PARTIAL_VOL_ELEV', base * 0.5)
        return ('PARTIAL_40', base)
    return ('CASH', 0.0)


STRATEGIES = {
    '1_BH_QQQ':        strat_bh_qqq,
    '2_BH_TQQQ':       strat_bh_tqqq,
    '3_Vol35_200MA':   strat_vol_200ma,  # CURRENT
    '4_A_100MA':       strat_A_100ma_partial,
    '5_B_MomThrust':   strat_B_momentum_thrust,
    '6_C_VolAccel':    strat_C_vol_acceleration,
    '7_D_DualSpeed':   strat_D_dual_speed,
    '8_E_B_plus_C':    strat_E_combo_B_plus_C,
    '9_F_A_plus_C':    strat_F_combo_A_plus_C,
}


# ============================================================
# BACKTEST ENGINE
# ============================================================

def build_state_series(qqq_closes, tqqq_closes, dates):
    """Compute all indicators in parallel arrays."""
    rsi = compute_rsi(qqq_closes)
    macd_line, macd_signal = compute_macd(qqq_closes)
    vol = compute_realized_vol(qqq_closes)
    dd_60d = compute_drawdown_from_recent_high(qqq_closes)
    ret_10d = compute_n_day_return(qqq_closes, 10)
    
    states = []
    for i, d in enumerate(dates):
        ma20 = sma(qqq_closes, 20, i)
        ma50 = sma(qqq_closes, MA_FAST, i)
        ma100 = sma(qqq_closes, MA_MED, i)
        ma200 = sma(qqq_closes, MA_SLOW, i)
        states.append({
            'date': d,
            'qqq_close': qqq_closes[i],
            'tqqq_close': tqqq_closes[i],
            'ma20': ma20,
            'ma50': ma50,
            'ma100': ma100,
            'ma200': ma200,
            'vol': vol[i],
            'rsi': rsi[i],
            'macd': macd_line[i],
            'macd_signal': macd_signal[i],
            'dd_60d': dd_60d[i],
            'ret_10d': ret_10d[i],
        })
    return states


def run_strategy(strategy_name, strategy_fn, states):
    """Run a single strategy and return equity curve + trade log.
    
    Rules:
    - Signal computed at Friday close
    - Allocation change executes at NEXT trading day's close
    - Costs (commission + slippage) applied to ALLOCATION DELTA, not total
    - SGOV portion earns SGOV_ANNUAL_YIELD as daily compounded interest
    """
    n = len(states)
    nav = 1.0  # Start at $1
    curr_tqqq_alloc = 0.0
    pending_alloc = 0.0
    pending_signal_idx = None
    
    equity_curve = []
    trade_log = []
    
    for i, s in enumerate(states):
        # Apply pending allocation change if it's the day after the signal
        if pending_signal_idx is not None and i > pending_signal_idx:
            delta = abs(pending_alloc - curr_tqqq_alloc)
            # Costs: 1bp commission + 5bp slippage round-trip, applied to delta
            cost = delta * (COMMISSION_BPS + SLIPPAGE_BPS) / 10000.0
            nav *= (1 - cost)
            curr_tqqq_alloc = pending_alloc
            pending_signal_idx = None
        
        # Daily mark-to-market
        if i > 0:
            tqqq_ret = (s['tqqq_close'] - states[i-1]['tqqq_close']) / states[i-1]['tqqq_close']
            sgov_daily = (1 + SGOV_ANNUAL_YIELD) ** (1/252) - 1
            portfolio_ret = curr_tqqq_alloc * tqqq_ret + (1 - curr_tqqq_alloc) * sgov_daily
            nav *= (1 + portfolio_ret)
        
        # Signal generation on Fridays (after market close)
        if is_friday(s['date']):
            if strategy_name == '1_BH_QQQ':
                # Special: track QQQ price directly, no allocation logic
                pass
            elif strategy_name == '2_BH_TQQQ':
                if curr_tqqq_alloc < 1.0:
                    pending_alloc = 1.0
                    pending_signal_idx = i
                    trade_log.append({
                        'date': s['date'], 'signal_idx': i,
                        'old_alloc': curr_tqqq_alloc, 'new_alloc': 1.0,
                        'reason': 'TQQQ_FULL',
                    })
            else:
                reason, target_alloc = strategy_fn(s)
                if target_alloc is None:
                    target_alloc = curr_tqqq_alloc
                # Only trigger if allocation changes meaningfully (>1pp)
                if abs(target_alloc - curr_tqqq_alloc) > 0.01:
                    pending_alloc = target_alloc
                    pending_signal_idx = i
                    trade_log.append({
                        'date': s['date'], 'signal_idx': i,
                        'old_alloc': curr_tqqq_alloc, 'new_alloc': target_alloc,
                        'reason': reason,
                    })
        
        equity_curve.append({
            'date': s['date'],
            'nav': nav,
            'alloc': curr_tqqq_alloc,
            'qqq_close': s['qqq_close'],
            'tqqq_close': s['tqqq_close'],
        })
    
    return equity_curve, trade_log


def run_bh_qqq(states):
    """Special: Buy & Hold QQQ tracks QQQ price directly."""
    n = len(states)
    qqq_start = states[0]['qqq_close']
    equity_curve = []
    for s in states:
        equity_curve.append({
            'date': s['date'],
            'nav': s['qqq_close'] / qqq_start,
            'alloc': 1.0,  # always "in" QQQ
            'qqq_close': s['qqq_close'],
            'tqqq_close': s['tqqq_close'],
        })
    return equity_curve, []


# ============================================================
# METRICS
# ============================================================

def compute_metrics(equity_curve, trade_log, start_date=None, end_date=None):
    """All metrics required by the spec."""
    # Filter to date range
    rows = equity_curve
    if start_date or end_date:
        rows = [r for r in equity_curve
                if (start_date is None or r['date'] >= start_date)
                and (end_date is None or r['date'] <= end_date)]
    if len(rows) < 30:
        return None
    
    # Normalize NAV to 1.0 at start of period
    start_nav = rows[0]['nav']
    nav_series = [r['nav'] / start_nav for r in rows]
    dates = [r['date'] for r in rows]
    
    # Daily returns
    daily_rets = []
    for i in range(1, len(nav_series)):
        daily_rets.append(nav_series[i] / nav_series[i-1] - 1)
    
    n_days = len(rows)
    years = n_days / 252
    
    # CAGR
    total_return = nav_series[-1] / nav_series[0] - 1
    cagr = (nav_series[-1] / nav_series[0]) ** (1 / years) - 1 if years > 0 else 0
    
    # Drawdown
    peak = nav_series[0]
    max_dd = 0
    peak_idx = 0
    longest_underwater = 0
    current_underwater = 0
    for i, v in enumerate(nav_series):
        if v > peak:
            peak = v
            longest_underwater = max(longest_underwater, current_underwater)
            current_underwater = 0
        else:
            current_underwater += 1
        dd = (v - peak) / peak
        if dd < max_dd:
            max_dd = dd
    longest_underwater = max(longest_underwater, current_underwater)
    
    # Vol, Sharpe, Sortino
    mean_daily = sum(daily_rets) / len(daily_rets) if daily_rets else 0
    var_daily = sum((r - mean_daily) ** 2 for r in daily_rets) / max(len(daily_rets) - 1, 1)
    daily_vol = math.sqrt(var_daily)
    ann_vol = daily_vol * math.sqrt(252)
    rf_daily = SGOV_ANNUAL_YIELD / 252
    sharpe = ((mean_daily - rf_daily) / daily_vol) * math.sqrt(252) if daily_vol > 0 else 0
    
    # Sortino (downside deviation)
    downside_rets = [r for r in daily_rets if r < 0]
    if downside_rets:
        downside_var = sum(r ** 2 for r in downside_rets) / len(downside_rets)
        downside_vol = math.sqrt(downside_var) * math.sqrt(252)
        sortino = ((mean_daily - rf_daily) * 252) / downside_vol if downside_vol > 0 else 0
    else:
        sortino = float('inf')
    
    # Calmar
    calmar = cagr / abs(max_dd) if max_dd < 0 else float('inf')
    
    # Ulcer Index — measures depth AND duration of drawdowns
    # UI = sqrt(mean(DD^2)) — penalizes long drawdowns more than single deep ones
    peak_ui = nav_series[0]
    dd_sq_sum = 0
    for v in nav_series:
        if v > peak_ui:
            peak_ui = v
        dd_pct = (v - peak_ui) / peak_ui * 100
        dd_sq_sum += dd_pct ** 2
    ulcer = math.sqrt(dd_sq_sum / len(nav_series))
    
    # Worst rolling N-month period
    def worst_rolling_pct(nav_series, n_months):
        n_days_window = int(n_months * 21)  # ~21 trading days/month
        if len(nav_series) < n_days_window:
            return None
        worst = 0
        for i in range(len(nav_series) - n_days_window):
            ret = nav_series[i + n_days_window] / nav_series[i] - 1
            worst = min(worst, ret)
        return worst
    
    worst_3m = worst_rolling_pct(nav_series, 3)
    worst_12m = worst_rolling_pct(nav_series, 12)
    
    # Allocation metrics — from equity curve
    alloc_series = [r['alloc'] for r in rows]
    pct_time_in_tqqq = sum(1 for a in alloc_series if a > 0.1) / len(alloc_series)
    avg_alloc = sum(alloc_series) / len(alloc_series)
    
    # Allocation changes per year (from trade log)
    relevant_trades = [t for t in trade_log if (start_date is None or t['date'] >= start_date)
                       and (end_date is None or t['date'] <= end_date)]
    n_changes = len(relevant_trades)
    changes_per_year = n_changes / years if years > 0 else 0
    
    # Whipsaws — flip back within 4 weeks
    whipsaws = 0
    for i, t in enumerate(relevant_trades):
        for j in range(i + 1, len(relevant_trades)):
            t2 = relevant_trades[j]
            d1 = parse_date(t['date'])
            d2 = parse_date(t2['date'])
            if (d2 - d1).days > 28:
                break
            # Whipsaw: alloc returns within 10% of original within 4 weeks
            if abs(t2['new_alloc'] - t['old_alloc']) < 0.10:
                whipsaws += 1
                break
    whipsaws_per_year = whipsaws / years if years > 0 else 0
    
    # Taxable events (proxy for short-term cap-gains incidents = allocation changes)
    taxable_events_per_year = changes_per_year
    
    return {
        'cagr_pct': cagr * 100,
        'max_dd_pct': max_dd * 100,
        'sharpe': sharpe,
        'sortino': sortino,
        'calmar': calmar,
        'ulcer': ulcer,
        'longest_underwater_days': longest_underwater,
        'pct_time_in_tqqq': pct_time_in_tqqq * 100,
        'avg_alloc_pct': avg_alloc * 100,
        'changes_per_year': changes_per_year,
        'whipsaws_per_year': whipsaws_per_year,
        'taxable_events_per_year': taxable_events_per_year,
        'worst_3m_pct': worst_3m * 100 if worst_3m else None,
        'worst_12m_pct': worst_12m * 100 if worst_12m else None,
        'corr_to_tenx_m5': 'pending_data',  # placeholder per Steven's instruction
        'n_days': n_days,
        'years': years,
        'total_return_pct': total_return * 100,
    }


# ============================================================
# DECISION RULE — LOCKED
# ============================================================

def evaluate_decision_rule(variant_metrics, baseline_metrics):
    """Returns: 'PASS', 'BORDERLINE', or 'FAIL' with reasons."""
    if variant_metrics is None or baseline_metrics is None:
        return 'NO_DATA', []
    
    reasons = []
    
    # Condition 1: CAGR not worse by more than 2pp
    cagr_diff = variant_metrics['cagr_pct'] - baseline_metrics['cagr_pct']
    if cagr_diff < -CAGR_SLACK:
        miss_pct = abs(cagr_diff - (-CAGR_SLACK)) / CAGR_SLACK
        sev = 'BORDERLINE' if miss_pct <= BORDERLINE_SLACK_PCT else 'FAIL'
        reasons.append((f'CAGR worse by {-cagr_diff:.1f}pp (slack 2pp)', sev))
    else:
        reasons.append((f'CAGR diff {cagr_diff:+.1f}pp ok', 'PASS'))
    
    # Condition 2: Max DD improved or not worse by more than 3pp
    # Note: DDs are negative. "Worse" means more negative.
    dd_diff = variant_metrics['max_dd_pct'] - baseline_metrics['max_dd_pct']
    if dd_diff < -MAX_DD_SLACK:
        miss_pct = abs(dd_diff - (-MAX_DD_SLACK)) / MAX_DD_SLACK
        sev = 'BORDERLINE' if miss_pct <= BORDERLINE_SLACK_PCT else 'FAIL'
        reasons.append((f'Max DD worse by {-dd_diff:.1f}pp (slack 3pp)', sev))
    else:
        reasons.append((f'Max DD diff {dd_diff:+.1f}pp ok', 'PASS'))
    
    # Condition 3: Ulcer improved OR time-underwater improved
    ulcer_better = variant_metrics['ulcer'] < baseline_metrics['ulcer']
    tuw_better = variant_metrics['longest_underwater_days'] < baseline_metrics['longest_underwater_days']
    if ulcer_better or tuw_better:
        reasons.append(('Recovery speed improved (Ulcer or TUW)', 'PASS'))
    else:
        # How close? If within 5% on both, borderline
        ulcer_miss = (variant_metrics['ulcer'] - baseline_metrics['ulcer']) / baseline_metrics['ulcer']
        tuw_miss = (variant_metrics['longest_underwater_days'] - baseline_metrics['longest_underwater_days']) / max(baseline_metrics['longest_underwater_days'], 1)
        sev = 'BORDERLINE' if min(ulcer_miss, tuw_miss) < 0.05 else 'FAIL'
        reasons.append((f'Neither Ulcer nor TUW improved', sev))
    
    # Condition 4: Whipsaws not more than 50% worse
    if baseline_metrics['whipsaws_per_year'] == 0:
        whipsaw_ok = variant_metrics['whipsaws_per_year'] <= 2.0  # allow up to 2/yr if baseline is 0
    else:
        ratio = variant_metrics['whipsaws_per_year'] / baseline_metrics['whipsaws_per_year']
        whipsaw_ok = ratio <= WHIPSAW_SLACK_MULT
    if whipsaw_ok:
        reasons.append((f'Whipsaws {variant_metrics["whipsaws_per_year"]:.1f}/yr ok', 'PASS'))
    else:
        ratio = variant_metrics['whipsaws_per_year'] / max(baseline_metrics['whipsaws_per_year'], 0.5)
        miss_pct = (ratio - WHIPSAW_SLACK_MULT) / WHIPSAW_SLACK_MULT
        sev = 'BORDERLINE' if miss_pct <= BORDERLINE_SLACK_PCT else 'FAIL'
        reasons.append((f'Whipsaws {ratio:.1f}x baseline (slack 1.5x)', sev))
    
    severities = [r[1] for r in reasons]
    if 'FAIL' in severities:
        return 'FAIL', reasons
    if 'BORDERLINE' in severities:
        return 'BORDERLINE', reasons
    return 'PASS', reasons


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 70)
    print("TQQQ STRATEGY FALSIFICATION BACKTEST")
    print("LOCKED DESIGN — NO TUNING AFTER RESULTS")
    print("=" * 70)
    
    # Find data files
    data_dir = None
    for candidate in ['./data', '../data', '../../data', 'data']:
        if os.path.exists(os.path.join(candidate, 'TQQQ.csv')):
            data_dir = candidate
            break
    if data_dir is None:
        print("ERROR: Could not find data/TQQQ.csv. Run from your tqqq-vol-target repo root.")
        sys.exit(1)
    
    print(f"\nLoading data from {data_dir}/...")
    tqqq_raw = load_csv(os.path.join(data_dir, 'TQQQ.csv'))
    qqq_raw = load_csv(os.path.join(data_dir, 'QQQ.csv'))
    print(f"  TQQQ: {len(tqqq_raw)} rows ({tqqq_raw[0][0]} to {tqqq_raw[-1][0]})")
    print(f"  QQQ:  {len(qqq_raw)} rows ({qqq_raw[0][0]} to {qqq_raw[-1][0]})")
    
    # Align dates — use only days both exist
    tqqq_dict = dict(tqqq_raw)
    qqq_dict = dict(qqq_raw)
    common_dates = sorted(set(tqqq_dict.keys()) & set(qqq_dict.keys()))
    print(f"  Common dates: {len(common_dates)} ({common_dates[0]} to {common_dates[-1]})")
    
    qqq_closes = [qqq_dict[d] for d in common_dates]
    tqqq_closes = [tqqq_dict[d] for d in common_dates]
    
    print(f"\nBuilding indicators...")
    states = build_state_series(qqq_closes, tqqq_closes, common_dates)
    
    # Run all strategies
    print(f"\nRunning {len(STRATEGIES)} strategies...")
    results = {}
    for name, fn in STRATEGIES.items():
        print(f"  {name}...")
        if name == '1_BH_QQQ':
            equity, trades = run_bh_qqq(states)
        else:
            equity, trades = run_strategy(name, fn, states)
        results[name] = {'equity': equity, 'trades': trades}
    
    # Compute aggregate metrics
    print(f"\nComputing aggregate metrics...")
    aggregate = {}
    for name, r in results.items():
        aggregate[name] = compute_metrics(r['equity'], r['trades'])
    
    # Per-regime metrics
    print(f"\nComputing per-regime metrics...")
    regime_metrics = defaultdict(dict)
    for regime_name, start_d, end_d in REGIMES:
        for name, r in results.items():
            regime_metrics[name][regime_name] = compute_metrics(r['equity'], r['trades'], start_d, end_d)
    
    # ==========================================
    # OUTPUT
    # ==========================================
    out_dir = './backtest_output'
    os.makedirs(out_dir, exist_ok=True)
    
    # 1. Aggregate summary
    with open(f'{out_dir}/results_summary.csv', 'w', newline='') as f:
        if any(aggregate.values()):
            sample = next(v for v in aggregate.values() if v)
            cols = ['strategy'] + list(sample.keys())
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for name in STRATEGIES.keys():
                if aggregate[name]:
                    row = {'strategy': name}
                    row.update({k: (f'{v:.3f}' if isinstance(v, float) else v) for k, v in aggregate[name].items()})
                    w.writerow(row)
    print(f"\nWrote {out_dir}/results_summary.csv")
    
    # 2. Per-regime
    with open(f'{out_dir}/results_per_regime.csv', 'w', newline='') as f:
        sample = None
        for s in aggregate.values():
            if s: sample = s; break
        cols = ['strategy', 'regime'] + list(sample.keys())
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for name in STRATEGIES.keys():
            for regime_name, _, _ in REGIMES:
                m = regime_metrics[name].get(regime_name)
                if m:
                    row = {'strategy': name, 'regime': regime_name}
                    row.update({k: (f'{v:.3f}' if isinstance(v, float) else v) for k, v in m.items()})
                    w.writerow(row)
    print(f"Wrote {out_dir}/results_per_regime.csv")
    
    # 3. Trade logs per variant
    for name, r in results.items():
        if r['trades']:
            with open(f'{out_dir}/trade_log_{name}.csv', 'w', newline='') as f:
                w = csv.DictWriter(f, fieldnames=['date', 'signal_idx', 'old_alloc', 'new_alloc', 'reason'])
                w.writeheader()
                for t in r['trades']:
                    w.writerow(t)
    print(f"Wrote trade logs for each variant")
    
    # 4. Equity curves
    with open(f'{out_dir}/equity_curves.csv', 'w', newline='') as f:
        cols = ['date'] + list(STRATEGIES.keys())
        w = csv.writer(f)
        w.writerow(cols)
        # Align all to first strategy's dates
        first_dates = [e['date'] for e in results[list(STRATEGIES.keys())[0]]['equity']]
        nav_lookup = {name: {e['date']: e['nav'] for e in results[name]['equity']}
                      for name in STRATEGIES.keys()}
        for d in first_dates:
            row = [d] + [f'{nav_lookup[n].get(d, ""):.4f}' if d in nav_lookup[n] else '' for n in STRATEGIES.keys()]
            w.writerow(row)
    print(f"Wrote {out_dir}/equity_curves.csv")
    
    # 5. Decision rule evaluation
    baseline_name = '3_Vol35_200MA'
    baseline = aggregate[baseline_name]
    print(f"\n{'='*70}")
    print(f"DECISION RULE EVALUATION (baseline = {baseline_name})")
    print(f"{'='*70}")
    
    decision_summary = []
    for name in STRATEGIES.keys():
        if name == baseline_name:
            continue
        if name in ('1_BH_QQQ', '2_BH_TQQQ'):
            # Baselines for comparison, not candidates
            continue
        
        # Aggregate (across all years 2010-2024 excl 2025+)
        aggregate_excl_2025 = compute_metrics(
            results[name]['equity'], results[name]['trades'],
            start_date='2010-01-01', end_date='2024-12-31'
        )
        baseline_excl_2025 = compute_metrics(
            results[baseline_name]['equity'], results[baseline_name]['trades'],
            start_date='2010-01-01', end_date='2024-12-31'
        )
        agg_verdict, agg_reasons = evaluate_decision_rule(aggregate_excl_2025, baseline_excl_2025)
        
        # Per-regime
        regime_verdicts = {}
        for regime_name in DECISION_REGIMES:
            v_m = regime_metrics[name].get(regime_name)
            b_m = regime_metrics[baseline_name].get(regime_name)
            verdict, _ = evaluate_decision_rule(v_m, b_m)
            regime_verdicts[regime_name] = verdict
        
        passing_regimes = sum(1 for v in regime_verdicts.values() if v == 'PASS')
        borderline_regimes = sum(1 for v in regime_verdicts.values() if v == 'BORDERLINE')
        
        # Final adoption verdict
        adoption = 'REJECT'
        if agg_verdict == 'PASS' and (passing_regimes + borderline_regimes) >= 4:
            if passing_regimes >= 4:
                adoption = 'ADOPT'
            else:
                adoption = 'BORDERLINE_ADOPT'
        elif agg_verdict == 'BORDERLINE' and passing_regimes >= 4:
            adoption = 'BORDERLINE_ADOPT'
        
        decision_summary.append({
            'variant': name,
            'aggregate_verdict': agg_verdict,
            'passing_regimes': passing_regimes,
            'borderline_regimes': borderline_regimes,
            'failing_regimes': len(DECISION_REGIMES) - passing_regimes - borderline_regimes,
            'final_adoption': adoption,
            'regime_breakdown': regime_verdicts,
            'agg_reasons': agg_reasons,
        })
        
        print(f"\n{name}:")
        print(f"  Aggregate (2010-2024): {agg_verdict}")
        for reason, sev in agg_reasons:
            marker = '✓' if sev == 'PASS' else ('~' if sev == 'BORDERLINE' else '✗')
            print(f"    {marker} {reason}")
        print(f"  Per-regime: {passing_regimes} PASS, {borderline_regimes} BORDERLINE, {len(DECISION_REGIMES)-passing_regimes-borderline_regimes} FAIL")
        for rn in DECISION_REGIMES:
            print(f"    {rn}: {regime_verdicts[rn]}")
        print(f"  ADOPTION VERDICT: {adoption}")
    
    # Write decision file
    with open(f'{out_dir}/decision_rule.csv', 'w', newline='') as f:
        cols = ['variant', 'aggregate_verdict', 'passing_regimes', 'borderline_regimes', 'failing_regimes', 'final_adoption']
        for rn in DECISION_REGIMES:
            cols.append(f'regime_{rn}')
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for d in decision_summary:
            row = {k: v for k, v in d.items() if k in cols}
            for rn in DECISION_REGIMES:
                row[f'regime_{rn}'] = d['regime_breakdown'][rn]
            w.writerow(row)
    print(f"\nWrote {out_dir}/decision_rule.csv")
    print(f"\n{'='*70}")
    print(f"Files in {out_dir}/:")
    print(f"  results_summary.csv      — all variants, all metrics, full period")
    print(f"  results_per_regime.csv   — all variants × all regimes")
    print(f"  decision_rule.csv        — adoption verdicts per variant")
    print(f"  equity_curves.csv        — daily NAV per variant")
    print(f"  trade_log_*.csv          — per-variant signal/allocation history")
    print(f"{'='*70}")
    print(f"\nNext step: run failure tests (see failure_tests.py)")


if __name__ == '__main__':
    main()
