#!/usr/bin/env python3
"""
TQQQ Failure Tests — Falsification of Backtest Results
========================================================

LOCKED. NO PARAMETER CHANGES AFTER RESULTS.

For each non-baseline variant, run these stress tests:
    1. +20bps extra slippage (does alpha survive realistic costs?)
    2. Skip best 5 TQQQ days (is variant relying on outlier days?)
    3. Skip worst 5 TQQQ days (is variant's edge from outlier-dodging?)
    4. Remove 2020 (does variant survive without the COVID recovery?)
    5. Remove 2022 (is variant only winning in one bear?)
    6. Delay signal execution by 1 week (is variant exploiting timing?)

For each test, report CAGR delta and Max DD delta vs the baseline test.

Run AFTER backtest.py completes.
"""

import csv
import math
import os
import sys
import importlib.util

# Import backtest module
this_dir = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("backtest", os.path.join(this_dir, "backtest.py"))
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)


# Override slippage for the +20bps test by monkey-patching
# (Cleaner: parameterize SLIPPAGE_BPS into run_strategy, but for now we override)

def run_with_extra_slippage(states, extra_bps):
    orig = bt.SLIPPAGE_BPS
    bt.SLIPPAGE_BPS = orig + extra_bps
    results = {}
    for name, fn in bt.STRATEGIES.items():
        if name == '1_BH_QQQ':
            results[name] = bt.run_bh_qqq(states)
        else:
            results[name] = bt.run_strategy(name, fn, states)
    bt.SLIPPAGE_BPS = orig
    return results


def run_skip_best_n(states, n):
    """Skip the N best TQQQ daily-return days by setting next-day TQQQ close to prior close."""
    # Compute TQQQ daily returns
    rets = []
    for i in range(1, len(states)):
        r = states[i]['tqqq_close'] / states[i-1]['tqqq_close'] - 1
        rets.append((i, r))
    rets.sort(key=lambda x: x[1], reverse=True)
    skip_indices = set(idx for idx, _ in rets[:n])
    
    # Build a copy of states with those days neutralized
    new_states = [dict(s) for s in states]
    for idx in skip_indices:
        new_states[idx]['tqqq_close'] = new_states[idx-1]['tqqq_close']
    
    results = {}
    for name, fn in bt.STRATEGIES.items():
        if name == '1_BH_QQQ':
            results[name] = bt.run_bh_qqq(new_states)
        else:
            results[name] = bt.run_strategy(name, fn, new_states)
    return results, skip_indices


def run_skip_worst_n(states, n):
    rets = []
    for i in range(1, len(states)):
        r = states[i]['tqqq_close'] / states[i-1]['tqqq_close'] - 1
        rets.append((i, r))
    rets.sort(key=lambda x: x[1])  # ascending — worst first
    skip_indices = set(idx for idx, _ in rets[:n])
    
    new_states = [dict(s) for s in states]
    for idx in skip_indices:
        new_states[idx]['tqqq_close'] = new_states[idx-1]['tqqq_close']
    
    results = {}
    for name, fn in bt.STRATEGIES.items():
        if name == '1_BH_QQQ':
            results[name] = bt.run_bh_qqq(new_states)
        else:
            results[name] = bt.run_strategy(name, fn, new_states)
    return results, skip_indices


def run_remove_year(states, year_to_remove):
    """Remove the specified year — re-stitch by skipping over."""
    year_str = str(year_to_remove)
    new_states = []
    for s in states:
        if s['date'][:4] == year_str:
            continue
        new_states.append(dict(s))
    
    results = {}
    for name, fn in bt.STRATEGIES.items():
        if name == '1_BH_QQQ':
            results[name] = bt.run_bh_qqq(new_states)
        else:
            results[name] = bt.run_strategy(name, fn, new_states)
    return results


def run_with_signal_delay(states, delay_days):
    """Delay signal execution by N additional trading days.
    
    Implementation: monkey-patch run_strategy to use longer pending delay.
    Simpler: re-implement with extra delay inline.
    """
    # We'll do a simpler version: shift all signals N days forward.
    # That means strategy looks at state[i] but acts on state[i+delay].
    
    def run_delayed_strategy(strategy_name, strategy_fn, states):
        n = len(states)
        nav = 1.0
        curr_alloc = 0.0
        pending_alloc = 0.0
        pending_exec_idx = None  # When to actually execute
        equity_curve = []
        trade_log = []
        
        for i, s in enumerate(states):
            # Execute pending if we've reached execution day
            if pending_exec_idx is not None and i >= pending_exec_idx:
                delta = abs(pending_alloc - curr_alloc)
                cost = delta * (bt.COMMISSION_BPS + bt.SLIPPAGE_BPS) / 10000.0
                nav *= (1 - cost)
                curr_alloc = pending_alloc
                pending_exec_idx = None
            
            # Daily mark-to-market
            if i > 0:
                tqqq_ret = (s['tqqq_close'] - states[i-1]['tqqq_close']) / states[i-1]['tqqq_close']
                sgov_daily = (1 + bt.SGOV_ANNUAL_YIELD) ** (1/252) - 1
                pret = curr_alloc * tqqq_ret + (1 - curr_alloc) * sgov_daily
                nav *= (1 + pret)
            
            if bt.is_friday(s['date']):
                if strategy_name == '2_BH_TQQQ':
                    if curr_alloc < 1.0:
                        pending_alloc = 1.0
                        pending_exec_idx = i + 1 + delay_days
                elif strategy_name != '1_BH_QQQ':
                    reason, target = strategy_fn(s)
                    if target is None:
                        target = curr_alloc
                    if abs(target - curr_alloc) > 0.01:
                        pending_alloc = target
                        pending_exec_idx = i + 1 + delay_days
                        trade_log.append({
                            'date': s['date'], 'signal_idx': i,
                            'old_alloc': curr_alloc, 'new_alloc': target,
                            'reason': reason,
                        })
            
            equity_curve.append({
                'date': s['date'], 'nav': nav, 'alloc': curr_alloc,
                'qqq_close': s['qqq_close'], 'tqqq_close': s['tqqq_close'],
            })
        return equity_curve, trade_log
    
    results = {}
    for name, fn in bt.STRATEGIES.items():
        if name == '1_BH_QQQ':
            results[name] = bt.run_bh_qqq(states)
        else:
            results[name] = run_delayed_strategy(name, fn, states)
    return results


def summarize(results, period_start='2010-01-01', period_end='2024-12-31'):
    """Compute aggregate metrics across non-2025 period."""
    summary = {}
    for name, (equity, trades) in results.items():
        summary[name] = bt.compute_metrics(equity, trades, period_start, period_end)
    return summary


def diff_vs_baseline(test_summary, baseline_summary):
    """For each variant, compute CAGR delta and DD delta vs its baseline run."""
    diffs = {}
    for name in test_summary:
        if test_summary[name] is None or baseline_summary[name] is None:
            diffs[name] = None
            continue
        diffs[name] = {
            'cagr_test': test_summary[name]['cagr_pct'],
            'cagr_baseline': baseline_summary[name]['cagr_pct'],
            'cagr_delta_pp': test_summary[name]['cagr_pct'] - baseline_summary[name]['cagr_pct'],
            'dd_test': test_summary[name]['max_dd_pct'],
            'dd_baseline': baseline_summary[name]['max_dd_pct'],
            'dd_delta_pp': test_summary[name]['max_dd_pct'] - baseline_summary[name]['max_dd_pct'],
            'sharpe_test': test_summary[name]['sharpe'],
            'sharpe_baseline': baseline_summary[name]['sharpe'],
            'sharpe_delta': test_summary[name]['sharpe'] - baseline_summary[name]['sharpe'],
        }
    return diffs


def main():
    print("=" * 70)
    print("TQQQ FAILURE TESTS — FALSIFICATION FRAMEWORK")
    print("=" * 70)
    
    # Load data
    data_dir = None
    for cand in ['./data', '../data', '../../data', 'data']:
        if os.path.exists(os.path.join(cand, 'TQQQ.csv')):
            data_dir = cand; break
    if not data_dir:
        print("ERROR: Cannot find data/TQQQ.csv"); sys.exit(1)
    
    tqqq_raw = bt.load_csv(os.path.join(data_dir, 'TQQQ.csv'))
    qqq_raw = bt.load_csv(os.path.join(data_dir, 'QQQ.csv'))
    tqqq_dict = dict(tqqq_raw); qqq_dict = dict(qqq_raw)
    common_dates = sorted(set(tqqq_dict.keys()) & set(qqq_dict.keys()))
    qqq_closes = [qqq_dict[d] for d in common_dates]
    tqqq_closes = [tqqq_dict[d] for d in common_dates]
    states = bt.build_state_series(qqq_closes, tqqq_closes, common_dates)
    
    # Baseline run (with default slippage, full data, no delay)
    print("\n[Test 0] Baseline run (full data, normal slippage)...")
    baseline_results = {}
    for name, fn in bt.STRATEGIES.items():
        if name == '1_BH_QQQ':
            baseline_results[name] = bt.run_bh_qqq(states)
        else:
            baseline_results[name] = bt.run_strategy(name, fn, states)
    baseline_summary = summarize(baseline_results)
    
    failure_results = {'baseline': baseline_summary}
    
    print("\n[Test 1] +20bps extra slippage...")
    r1 = run_with_extra_slippage(states, 20)
    failure_results['slip_+20bp'] = diff_vs_baseline(summarize(r1), baseline_summary)
    
    print("[Test 2] Skip best 5 TQQQ days...")
    r2, skip_b = run_skip_best_n(states, 5)
    failure_results['skip_best_5'] = diff_vs_baseline(summarize(r2), baseline_summary)
    
    print("[Test 3] Skip worst 5 TQQQ days...")
    r3, skip_w = run_skip_worst_n(states, 5)
    failure_results['skip_worst_5'] = diff_vs_baseline(summarize(r3), baseline_summary)
    
    print("[Test 4] Remove 2020...")
    r4 = run_remove_year(states, 2020)
    failure_results['remove_2020'] = diff_vs_baseline(summarize(r4), baseline_summary)
    
    print("[Test 5] Remove 2022...")
    r5 = run_remove_year(states, 2022)
    failure_results['remove_2022'] = diff_vs_baseline(summarize(r5), baseline_summary)
    
    print("[Test 6] Delay signals by 1 week (5 trading days)...")
    r6 = run_with_signal_delay(states, 5)
    failure_results['signal_delay_1w'] = diff_vs_baseline(summarize(r6), baseline_summary)
    
    # Write failure_tests.csv
    out_dir = './backtest_output'
    os.makedirs(out_dir, exist_ok=True)
    
    with open(f'{out_dir}/failure_tests.csv', 'w', newline='') as f:
        cols = ['test', 'variant', 'cagr_test', 'cagr_baseline', 'cagr_delta_pp',
                'dd_test', 'dd_baseline', 'dd_delta_pp',
                'sharpe_test', 'sharpe_baseline', 'sharpe_delta']
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for test_name, diffs in failure_results.items():
            if test_name == 'baseline':
                continue
            for variant, d in diffs.items():
                if d is None: continue
                row = {'test': test_name, 'variant': variant}
                row.update({k: f'{v:.3f}' if isinstance(v, float) else v for k, v in d.items()})
                w.writerow(row)
    
    # Print summary table
    print(f"\n{'='*70}")
    print("FAILURE TEST SUMMARY — Δ vs Baseline run")
    print(f"{'='*70}")
    print(f"{'Variant':<22} {'Test':<20} {'CAGR Δ':>10} {'DD Δ':>10} {'Sharpe Δ':>10}")
    print('-' * 75)
    for variant in bt.STRATEGIES.keys():
        for test_name in ['slip_+20bp', 'skip_best_5', 'skip_worst_5', 'remove_2020', 'remove_2022', 'signal_delay_1w']:
            d = failure_results.get(test_name, {}).get(variant)
            if d is None: continue
            cagr_d = f"{d['cagr_delta_pp']:+.2f}"
            dd_d = f"{d['dd_delta_pp']:+.2f}"
            sh_d = f"{d['sharpe_delta']:+.3f}"
            print(f"{variant:<22} {test_name:<20} {cagr_d:>10} {dd_d:>10} {sh_d:>10}")
    print(f"\nWrote {out_dir}/failure_tests.csv")


if __name__ == '__main__':
    main()
