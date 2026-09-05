# SESSION REPORT — TQQQ-PUB-001 (freshness gate, publish-on-official, re-run survival)

## 1. Outcome
**TQQQ-PUB-001 — SHIPPED to branch `fix/pub-001-freshness-gate`.** All six tasks done. **Committed on
the branch only — not merged, not pushed** (this repo's flow: Steven merges/pushes, then performs the
repair run).

Files: **new `freshness_gate.py`** (the gate + its self-check), **`.github/workflows/friday_report.yml`**
(+99/−16), **this report**. `weekly_report.py` and `fetch_tqqq.py` are **untouched** — see §6.

## 2. The incident, confirmed from this repo before any code changed
```
logs/history.csv newest row : 2026-09-03,,0.4028286956022835,0.35,0.85,...   ← a THURSDAY
prior rows                  : 2026-08-14 / 08-21 / 08-28  — all Fridays
commit 476454c              : 2026-09-05T01:26:33Z "Official TQQQ vol report (2026-09-05)"
data/QQQ.csv (committed)    : newest bar 2026-09-03
```
The Friday-evening run committed Thursday's bar as the week's signal, and every step reported success.
`8ccbac1` — the SHA the ticket names as the re-run's checkout — is indeed 13 commits back on `main`,
which is exactly why that re-run's push was rejected.

## 3. What each task did
1. **FRESHNESS GATE** — new `freshness_gate.py`, run between `fetch_tqqq.py` and `weekly_report.py`.
   Computes the expected as-of, compares the newest bar in **both** CSVs, re-fetches 6× at 10-minute
   intervals, and **exits nonzero having written nothing** if still behind. Every publish step is
   downstream, so a red gate publishes nothing.
2. **SCHEDULE MARGIN** — cron `10 1 * * 6` → `10 2 * * 6`.
3. **LOUD FAILURE** — `if: failure() && env.MODE == 'official'` Pushover step, priority 1, naming both
   newest bars and linking the run.
4. **MANUAL OFFICIAL RUNS PUBLISH** — all **8** publish steps re-keyed from
   `github.event_name == 'schedule'` to `env.MODE == 'official'`. The non-Friday `force_official`
   guardrail is untouched; debug runs still publish nothing.
5. **RE-RUN / RACE SURVIVAL** — checkout now pins `ref: main` with `fetch-depth: 0` (a re-run no longer
   checks out the run's original SHA), and the commit step does `git pull --rebase origin main` with up
   to 3 push attempts.
6. **RUNDATE SEMANTICS** — one comment block above the commit step.

## 4. Pre-mortem answers
**Holiday Fridays.** `expected_asof(now)` = the most recent **completed trading day**: walk back from
today, skip weekends and NYSE holidays, and require that day's close (21:00Z) to be in the past.
Holidays are computed **by rule, not from a table** — a table has a coverage cliff where it silently
starts guessing. Good Friday is derived from Easter (anonymous Gregorian), which matters because Good
Friday is an NYSE holiday and *not* a federal one, so pandas' `USFederalHolidayCalendar` is the wrong
calendar for this job. Asserted: a Saturday run on Good Friday weekend expects **Thursday**, no false
alarm. **Residual limit:** ad-hoc closures (a national day of mourning, a hurricane) are not
rule-expressible and are not modelled — on such a day the gate expects a bar that never comes and fails
the run loudly. That is the safe direction.

**One symbol fresh, one stale.** Stale. `check()` requires **both**; a signal computed from a fresh TQQQ
and a stale QQQ is a signal computed from two different days, which is worse than no signal. Asserted in
both directions.

**Retry loop vs job limits.** 6 × 10 min = a 1-hour worst case, inside the 6-hour Actions job limit — and
inside the gap before the GT-platform Saturday watchdog fires, so the two do not race.

**Re-entrancy / double-append.** The history row is written only after the gate passes, and
`upsert_history_row` dedupes on `RunDate` (`keep="last"`). **Proven, not asserted:** running the real
`weekly_report.py` twice over the same as-of left `logs/history.csv` **byte-identical** (§5C).

**Rebase conflict on history.csv.** **Fail loudly and stop** — chosen deliberately over any automatic
resolution. `history.csv` is append-mostly, so a conflict means two runs appended over overlapping dates,
and every automatic choice silently drops a row: "ours" drops theirs, "theirs" drops ours, a union can
interleave dates. A human must look. The gate has already guaranteed nothing false was computed, so
stopping costs a publish, not correctness. The step aborts the rebase and emits `::error::` lines saying
exactly that.

## 5. Verification — this repo had no self-check; it has one now
**`python freshness_gate.py --self-test` → 27/27**, no network, no sleeping. Covers the rule-based
calendar (Good Friday, observed-Saturday July 4, Labor Day, Thanksgiving), the expectation
(mid-session Friday → Thursday; Good Friday weekend → Thursday), and the gate itself.

**A) STALE fixture** — expectation 2026-09-04, real data ending 2026-04-10:
```
[gate] STALE on attempt 1/3 — QQQ=2026-04-10, TQQQ=2026-04-10 (expected >= 2026-09-04)
[gate] re-fetch raised: No module named 'yfinance'          ← a failed re-fetch is a stale attempt, not a crash
[gate] FAIL — data never reached the expected as-of after 3 attempts
[gate] PUBLISHING NOTHING: no history row, no RTDB PUT, no Pages deploy, no success notification.
EXIT CODE: 1        history.csv UNCHANGED ✓        no signal file written ✓
```
The self-test also reproduces **the incident itself** — both symbols ending Thu 09-03 with Friday
expected — and it exits nonzero. **The 09-04 run would have been caught.**

**B) FRESH fixture** — real bars only (QQQ truncated to TQQQ's newest real date, 2026-04-10; that row
first removed from history):
```
[gate] PASS — QQQ=2026-04-10, TQQQ=2026-04-10 (expected >= 2026-04-10)     GATE EXIT: 0
[SIGNAL] wrote signals/tqqq_vol_latest.json (mode=official, allocTQQQ=0.45, runDate=2026-04-10)
rows 31 → 32 · rows stamped 2026-04-10: 1 · 2026-04-10,,0.7650188603178404,0.35,0.45
```
**Exactly one row, stamped with the bar date.**

**C) RE-ENTRANCY** — the same run again: `rows: 32`, `rows stamped 2026-04-10: 1`, `history.csv
byte-identical ✓`.

## 6. ⚠ Finding reported, NOT fixed: `weekly_report.py` contains the entire script TWICE
Not in this ticket's scope, and deleting ~620 lines of strategy code inside a freshness ticket would be
reckless — but it sits directly on the re-entrancy question, so it is evidenced here.

- The banner `# TQQQ Vol(35%) + 200MA Strategy — v6` appears at **line 2 and line 621**. Copy 1 is 620
  lines, copy 2 is 809; they are **86% similar but not identical** (copy 2 writes
  `signals/tqqq_vol_latest.json`; copy 1 has no such line and prints nothing at all).
- There is **no `if __name__ == "__main__"` guard** anywhere in the file, so both copies execute at
  import. Copy 1 reaches `history = upsert_history_row(HISTORY_PATH, row)` at its line 340.
- **Measured, not inferred:** a probe on every `hist.to_csv` site recorded **4 history writes in one
  invocation** (2 files × 2 copies).

It is currently harmless — copy 2 runs last and `drop_duplicates(keep="last")` collapses the rest — which
is precisely why it has gone unnoticed. The risk is that copy 1 is an **older strategy version**: if the
two ever disagree, copy 1's row is written and silently overwritten, and nothing surfaces the
disagreement. Recommended as its own ticket: delete copy 1, or keep one and re-verify outputs byte-for-byte.

## 7. For the verifier
- **Not merged, not pushed.** Branch `fix/pub-001-freshness-gate`; this repo's flow is Steven's.
- **THE REPAIR RUN** (after merge): Actions → "Friday TQQQ Vol Report" → Run workflow, `mode=official`,
  `force_official=true`, `asof` blank. With TASK 4 this now publishes for real. Expect history newest row
  **RunDate 2026-09-04, alloc 0.90, vol ≈ 0.386**, RTDB updated, Pages deployed, normal Pushover.
- **The gate, not the schedule, is the fix.** Asserted in the self-test: the expectation was already
  "Friday" at the *old* 01:10Z cron, so the gate would have caught the incident then too. The extra hour
  only reduces how often the retry loop has to work.
- **Two limits worth knowing.** (a) If the run fails *before* the "Decide run mode" step, `env.MODE` is
  unset and the failure notification does not fire — the GT-platform Saturday watchdog remains the
  backstop for that window. (b) `concurrency: group: "pages"` with `cancel-in-progress: true` is
  pre-existing and unchanged: a manual official run started while a scheduled one is in flight will
  cancel it.
- **`GATE_MAX_RETRIES` / `GATE_RETRY_SLEEP_S`** exist only so the gate can be exercised without waiting an
  hour. The workflow sets neither, so production always gets the real 6 × 10 min budget.
