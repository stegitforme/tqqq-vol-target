#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import pathlib
import subprocess
from datetime import datetime

import pandas as pd

# ----------------------------
# Paths
# ----------------------------
BASE = pathlib.Path("/Users/sggmpb13/Library/Mobile Documents/com~apple~CloudDocs/Trading")
DATA_DIR = BASE / "data"
OUT_DIR = BASE / "output"
STATE_DIR = BASE / "state"
LOG_DIR = BASE / "logs"

TICKER = "TQQQ"
CSV_PATH = DATA_DIR / f"{TICKER}.csv"
HTML_PATH = OUT_DIR / "weekly_report.html"
STATE_PATH = STATE_DIR / "last_allocation.json"

FETCH_SCRIPT = BASE / "scripts" / "fetch_tqqq.py"
VENV_PY = BASE / ".venv" / "bin" / "python3"

# ----------------------------
# Strategy constants (set-and-forget)
# ----------------------------
LOOKBACK_DAYS = 20
TRADING_DAYS_PER_YEAR = 252
TARGET_VOL_ANNUAL = 0.20
ROUND_STEP = 0.05
CHANGE_THRESHOLD = 0.05

# ----------------------------
# Helpers
# ----------------------------
def notify(title: str, message: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
            check=False,
        )
    except Exception:
        pass


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def round_step(x: float, step: float) -> float:
    return round(x / step) * step


def read_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            return {}
    return {}


def write_state(prev_alloc: float, curr_alloc: float) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "prev_alloc": prev_alloc,
                "curr_alloc": curr_alloc,
            },
            indent=2,
        )
    )


def load_prices() -> pd.Series:
    df = pd.read_csv(CSV_PATH)
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.dropna(subset=["Close"]).sort_values("Date")
    return df.set_index("Date")["Close"].astype(float)


def compute_alloc(prices: pd.Series) -> tuple[float, float, float]:
    rets = prices.pct_change().dropna()
    curr = rets.iloc[-LOOKBACK_DAYS:]
    prev = rets.iloc[-(LOOKBACK_DAYS + 1):-1]

    curr_vol = curr.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)
    prev_vol = prev.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)

    curr_alloc = TARGET_VOL_ANNUAL / curr_vol if curr_vol > 0 else 1.0
    prev_alloc = TARGET_VOL_ANNUAL / prev_vol if prev_vol > 0 else 1.0

    curr_alloc = round_step(clamp(curr_alloc, 0, 1), ROUND_STEP)
    prev_alloc = round_step(clamp(prev_alloc, 0, 1), ROUND_STEP)

    return prev_alloc, curr_alloc, float(curr_vol)


def alloc_for_vol(vol_annual: float) -> float:
    """Implied allocation before rounding/clamp (then apply same rules)."""
    if vol_annual <= 0:
        a = 1.0
    else:
        a = TARGET_VOL_ANNUAL / vol_annual
    a = clamp(a, 0, 1)
    a = round_step(a, ROUND_STEP)
    return float(a)


def vol_table_html(curr_vol: float) -> str:
    # Common levels that make sense for interpretation
    levels = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60]
    # Add current vol so it always appears (even if not on our grid)
    levels = sorted(set(levels + [round(curr_vol, 3)]))

    rows = []
    for v in levels:
        a = alloc_for_vol(v)
        cash = 1 - a
        highlight = " hl" if abs(v - curr_vol) < 1e-6 else ""
        rows.append(
            f"""
            <tr class="{highlight.strip()}">
              <td>{v:.1%}</td>
              <td><b>{a:.0%}</b></td>
              <td>{cash:.0%}</td>
            </tr>
            """
        )

    return f"""
    <div class="card">
      <div class="h2">Volatility → Allocation (quick guide)</div>
      <div class="muted2">Rule of thumb: when realized vol rises, TQQQ allocation falls. (TargetVol={TARGET_VOL_ANNUAL:.0%}, rounding={int(ROUND_STEP*100)}% steps)</div>
      <div style="height:10px"></div>
      <table class="tbl">
        <thead>
          <tr>
            <th>Realized vol (20d, annualized)</th>
            <th>Target TQQQ</th>
            <th>Cash</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows)}
        </tbody>
      </table>
      <div class="muted2" style="margin-top:10px">
        Today’s realized vol is highlighted.
      </div>
    </div>
    """


def html_report(prices, prev_alloc, curr_alloc, curr_vol):
    last_close = float(prices.iloc[-1])
    cash_alloc = 1 - curr_alloc
    today = prices.index[-1].date().isoformat()

    action = "HOLD" if abs(curr_alloc - prev_alloc) < 1e-12 else "REBALANCE"

    monday_line = (
        "No action needed (target unchanged)."
        if action == "HOLD"
        else "Rebalance toward the target allocation."
    )

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>TQQQ Weekly Allocation</title>
<style>
:root {{
  --bg: #0b0f1a;
  --card: rgba(255,255,255,0.06);
  --card2: rgba(255,255,255,0.09);
  --text: rgba(255,255,255,0.92);
  --muted: rgba(255,255,255,0.72);
  --muted2: rgba(255,255,255,0.58);
  --line: rgba(255,255,255,0.10);
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, system-ui, Segoe UI, Roboto, Helvetica, Arial;
  background: radial-gradient(1100px 600px at 15% 0%, rgba(90,120,255,.22), transparent 55%),
              radial-gradient(900px 520px at 85% 10%, rgba(0,200,255,.16), transparent 60%),
              var(--bg);
  color: var(--text);
  margin: 0;
  padding: 24px 16px 56px;
}}
.wrap {{
  max-width: 980px;
  margin: 0 auto;
}}
.top {{
  display:flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 12px;
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--line);
}}
.title {{
  font-size: 18px;
  font-weight: 700;
  margin: 0;
}}
.sub {{
  color: var(--muted);
  font-size: 12px;
  margin-top: 4px;
}}
.pill {{
  background: var(--card2);
  border: 1px solid var(--line);
  padding: 10px 12px;
  border-radius: 999px;
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
}}
.card {{
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 18px;
  padding: 16px 16px;
  box-shadow: 0 14px 36px rgba(0,0,0,.26);
  margin-bottom: 14px;
}}
.grid {{
  display:grid;
  grid-template-columns: 1.2fr .8fr;
  gap: 14px;
}}
@media (max-width: 900px) {{
  .grid {{ grid-template-columns: 1fr; }}
}}
.big {{
  font-size: 44px;
  font-weight: 800;
  letter-spacing: .3px;
  margin: 6px 0 0;
}}
.muted {{
  color: var(--muted);
  font-size: 13px;
  margin-top: 4px;
}}
.kv {{
  display:grid;
  grid-template-columns: repeat(2, minmax(0,1fr));
  gap: 10px 14px;
  margin-top: 12px;
}}
.k {{
  display:block;
  font-size: 12px;
  color: var(--muted2);
}}
.v {{
  display:block;
  font-size: 14px;
  color: var(--text);
  margin-top: 2px;
}}
.callout {{
  background: rgba(255,255,255,0.08);
  border: 1px dashed rgba(255,255,255,0.20);
  padding: 12px 12px;
  border-radius: 16px;
  margin-top: 12px;
  line-height: 1.35;
}}
.tag {{
  display:inline-block;
  padding: 6px 10px;
  border-radius: 999px;
  background: rgba(255,255,255,0.10);
  border: 1px solid rgba(255,255,255,0.12);
  color: var(--text);
  font-size: 12px;
  margin-left: 8px;
  vertical-align: middle;
}}
.h2 {{
  font-size: 14px;
  font-weight: 700;
  margin: 0 0 4px;
}}
.muted2 {{
  color: var(--muted2);
  font-size: 12px;
}}
.tbl {{
  width: 100%;
  border-collapse: collapse;
  overflow: hidden;
  border-radius: 14px;
}}
.tbl th, .tbl td {{
  padding: 10px 10px;
  border-bottom: 1px solid rgba(255,255,255,0.08);
  text-align: left;
  font-size: 13px;
}}
.tbl th {{
  color: var(--muted);
  font-weight: 600;
  background: rgba(255,255,255,0.06);
}}
.tbl tr.hl {{
  background: rgba(0,200,255,0.10);
  outline: 1px solid rgba(0,200,255,0.22);
}}
ul {{
  margin: 8px 0 0 18px;
  color: var(--muted);
  font-size: 13px;
}}
li {{ margin: 6px 0; }}
</style>
</head>
<body>
<div class="wrap">

  <div class="top">
    <div>
      <div class="title">TQQQ Realized-Vol Target — Weekly Report</div>
      <div class="sub">As of {today} · uses your local CSV data</div>
    </div>
    <div class="pill">Run: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="h2">Recommendation <span class="tag">{action}</span></div>
      <div class="big">{int(curr_alloc*100)}% {TICKER}</div>
      <div class="muted">{int(cash_alloc*100)}% CASH</div>

      <div class="kv">
        <div><span class="k">Last close</span><span class="v">${last_close:,.2f}</span></div>
        <div><span class="k">Realized vol ({LOOKBACK_DAYS}d, annualized)</span><span class="v">{curr_vol:.1%}</span></div>
        <div><span class="k">Previous target</span><span class="v">{int(prev_alloc*100)}%</span></div>
        <div><span class="k">Current target</span><span class="v">{int(curr_alloc*100)}%</span></div>
      </div>

      <div class="callout">
        <b>What to do Monday (30–60 min after open):</b><br/>
        Target: <b>{int(curr_alloc*100)}% {TICKER} / {int(cash_alloc*100)}% CASH</b><br/>
        {monday_line}
      </div>
    </div>

    {vol_table_html(curr_vol)}
  </div>

  <div class="card">
    <div class="h2">Strategy reminder (read only if needed)</div>
    <ul>
      <li>Adjusts how much {TICKER} to own based on recent volatility</li>
      <li>Higher volatility → own less {TICKER}</li>
      <li>Lower volatility → own more {TICKER}</li>
      <li>Remaining allocation stays in cash</li>
      <li>Compute after Friday close · trade Monday (30–60 min after open)</li>
    </ul>
  </div>

</div>
</body>
</html>
"""


def main():
    # fetch latest data (use venv python, important under launchd)
    subprocess.run([str(VENV_PY), str(FETCH_SCRIPT)], check=True)

    prices = load_prices()
    prev_alloc, curr_alloc, curr_vol = compute_alloc(prices)

    state = read_state()
    last = float(state.get("curr_alloc", curr_alloc))
    first = not STATE_PATH.exists()

    write_state(prev_alloc, curr_alloc)
    OUT_DIR.mkdir(exist_ok=True)

    HTML_PATH.write_text(html_report(prices, prev_alloc, curr_alloc, curr_vol), encoding="utf-8")
    print(f"Wrote: {HTML_PATH}")
    print(f"Prev: {prev_alloc:.2%}  Curr: {curr_alloc:.2%}  Cash: {(1-curr_alloc):.2%}")

    if first or abs(curr_alloc - last) >= CHANGE_THRESHOLD:
        notify("TQQQ Vol Target", f"{int(curr_alloc*100)}% TQQQ / {int((1-curr_alloc)*100)}% CASH")


if __name__ == "__main__":
    main()
