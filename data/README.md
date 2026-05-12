# TQQQ Strategy Falsification Backtest

**Status:** Locked design. NO parameter tuning after results.

## Purpose

Test whether 6 hedge-fund-style variants of your Vol(35%)+200MA strategy
improve performance under a strict 4-condition decision rule, across ≥4
of 6 historical regimes. Falsification framework — designed to KILL
candidate strategies, not promote them.

## Files

- `backtest.py` — main engine. Runs 9 strategies, outputs metrics + decision rule.
- `failure_tests.py` — runs 6 falsification stress tests.

## How to run

From your `tqqq-vol-target` repo root (where `data/TQQQ.csv` and `data/QQQ.csv` live):

```bash
python3 backtest.py
python3 failure_tests.py
```

Outputs go to `./backtest_output/`.

## Strategies tested

| ID | Strategy | Class |
|---|---|---|
| 1 | Buy & Hold QQQ | Baseline |
| 2 | Buy & Hold TQQQ | Baseline |
| 3 | Vol(35%) + 200MA | **Current — baseline for comparison** |
| 4 | A: 100MA Partial Re-Entry | Single-axis |
| 5 | B: Momentum Thrust Re-Entry | Single-axis |
| 6 | C: Vol Acceleration Exit Overlay | Single-axis |
| 7 | D: Dual-Speed Trend (50MA, NOT 20MA per pre-work) | Single-axis |
| 8 | E: Combo1 (B + C) | Combo |
| 9 | F: Combo2 (A + C) | Combo |

## Locked decision rule

A variant ADOPTS only if **all four** are true vs `3_Vol35_200MA`:

1. CAGR not worse by more than 2pp
2. Max DD improved OR not worse by more than 3pp
3. Ulcer Index improved OR Time-Underwater improved
4. Whipsaws/yr not more than 1.5× baseline

**AND** these pass in **≥4 of 6 historical regimes**:

- 2010-2014 recovery
- 2015-2016 chop
- 2018 Q4 crash
- 2020 COVID
- 2022 rate-hike bear
- 2023-2024 AI bull

**2025-2026 YTD is reported but excluded from decision.**

A "miss by ≤50% of slack" on any condition counts as BORDERLINE, not FAIL.
Strict-but-noise-tolerant per Steven's clarification.

## Failure tests

Each variant's CAGR/DD/Sharpe delta vs its own baseline run is measured
under:

1. +20bp extra slippage
2. Skip best 5 TQQQ days
3. Skip worst 5 TQQQ days
4. Remove 2020
5. Remove 2022
6. Delay signal execution by 1 trading week (5 days)

A variant that loses substantial CAGR (>5pp) under any one of these is
not robust enough to ship.

## Known limitations

- **No simulated pre-2010 TQQQ.** Backtest starts at TQQQ inception (Feb 2010).
  No 2008 GFC stress test. If you want one, we'd need to derive synthetic
  TQQQ from QQQ returns × 3 with daily reset and financing drag.
- **No real breadth data.** Variant B uses Steven's "momentum thrust" proxy
  (QQQ down >15% + 10d return >8% + close above 20MA). True Zweig Breadth
  Thrust requires advance/decline data we don't have locally.
- **No tax modeling.** "Taxable events/year" is a proxy (= allocation changes/year).
  Real tax drag varies by holding period and account type.
- **TENX_M5 correlation = "pending_data"** until you wire in monthly TENX_M5
  returns from the gt-platform RTDB.
- **No survivorship adjustment for TQQQ ETF specifically.** TQQQ has existed
  continuously since 2010 so this isn't an issue here.
- **No volatility decay simulation.** Backtest uses ACTUAL TQQQ closes, so
  daily-reset compounding decay is already baked in.

## Outputs

After both scripts run, `backtest_output/` contains:

- `results_summary.csv` — all variants, all metrics, full period
- `results_per_regime.csv` — all variants × 7 regimes
- `decision_rule.csv` — adoption verdict per variant
- `equity_curves.csv` — daily NAV per variant
- `trade_log_<variant>.csv` — full signal/allocation history per variant
- `failure_tests.csv` — robustness stress test results

## Interpreting results

The decision rule is binary by design. Don't argue with it after results
come in. If a variant adopts:

→ Path B from the earlier session: ship the entry-side modifications to
weekly_report.py. Update strategy logic, version bump, send-to-Steven.

If no variant adopts:

→ Path C: leave Vol(35%)+200MA alone. Current strategy is good enough.
Move on to other workstreams.

If multiple variants adopt:

→ Pick the SIMPLEST one (fewest indicators, fewest rules). Operational
simplicity is a real factor. Combo strategies have more failure surface.

## What I learned from this

If the decision rule is too strict and nothing adopts, the lesson is:
your current strategy is already on the efficient frontier for your
specific objectives. Adding rules makes it worse in expectation.

If the decision rule is too loose and everything adopts, the framework
was wrong, not the strategies.

If exactly 1-2 variants adopt, that's a meaningful signal — investigate
WHY they worked, and consider whether the WHY is robust to forward-period
data (different market regimes, different vol environments).
