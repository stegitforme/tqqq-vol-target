import pandas as pd
from pathlib import Path
from datetime import datetime

# -----------------------------
# Paths
# -----------------------------
HISTORY_PATH = Path("logs/history.csv")
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

REPORT_PATH = OUTPUT_DIR / "weekly_report.html"

# -----------------------------
# Load history
# -----------------------------
df = pd.read_csv(HISTORY_PATH, parse_dates=["RunDate"])
df = df.sort_values("RunDate").reset_index(drop=True)

if len(df) < 2:
    raise ValueError("Not enough history to compute previous allocation")

# -----------------------------
# Current row (latest run)
# -----------------------------
current_row = df.iloc[-1]

current_date = current_row["RunDate"].date()
current_close = current_row["Close"]
current_vol = current_row["RealizedVol20d"]
current_alloc = current_row["AllocTQQQ"]

# -----------------------------
# Find PREVIOUS DISTINCT allocation
# -----------------------------
previous_alloc = None

for i in range(len(df) - 2, -1, -1):
    if df.iloc[i]["AllocTQQQ"] != current_alloc:
        previous_alloc = df.iloc[i]["AllocTQQQ"]
        break

# Fallback (should never happen, but safe)
if previous_alloc is None:
    previous_alloc = df.iloc[-2]["AllocTQQQ"]

# -----------------------------
# Percent formatting
# -----------------------------
def pct(x):
    return f"{round(x * 100):.0f}%"

# -----------------------------
# Build HTML
# -----------------------------
html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>TQQQ Volatility Target</title>
<style>
body {{
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    background: #f5f6f7;
    margin: 0;
    padding: 32px;
}}
h1 {{ margin-bottom: 4px; }}
.subtitle {{ color: #666; margin-bottom: 16px; }}

.card {{
    background: white;
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 24px;
    box-shadow: 0 4px 10px rgba(0,0,0,0.05);
}}

.grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 24px;
}}

table {{
    width: 100%;
    border-collapse: collapse;
}}

td {{
    padding: 6px 0;
}}

td:first-child {{
    color: #666;
}}
</style>
</head>
<body>

<h1>TQQQ Volatility Target</h1>
<div class="subtitle">Weekly sizing based on realized volatility</div>

<div class="grid">
    <div class="card">
        <h2>This week</h2>
        <table>
            <tr><td>Date</td><td>{current_date}</td></tr>
            <tr><td>TQQQ Close</td><td>${current_close:.2f}</td></tr>
            <tr><td>Realized Vol (20d)</td><td>{pct(current_vol)}</td></tr>
            <tr><td>Previous Allocation</td><td>{pct(previous_alloc)} TQQQ</td></tr>
            <tr><td>Current Allocation</td><td>{pct(current_alloc)} TQQQ</td></tr>
            <tr><td>Cash / BIL</td><td>{pct(1 - current_alloc)}</td></tr>
        </table>
    </div>

    <div class="card">
        <h2>How it works</h2>
        <ul>
            <li>Compute 20-day realized volatility (annualized).</li>
            <li>Allocation ≈ TargetVol ÷ RealizedVol.</li>
            <li>Rounded to 5% steps to reduce churn.</li>
            <li>Run after Friday close; execute Monday.</li>
            <li>Cash sleeve = BIL / SGOV.</li>
        </ul>
    </div>
</div>

<p style="color:#777;margin-top:32px;">
Data source: logs/history.csv (auto-updated by GitHub Actions)
</p>

</body>
</html>
"""

# -----------------------------
# Write output
# -----------------------------
REPORT_PATH.write_text(html, encoding="utf-8")

print("✅ weekly_report.html generated successfully")
