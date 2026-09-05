#!/usr/bin/env python3
"""
freshness_gate.py — TQQQ-PUB-001.

WHY THIS EXISTS
---------------
On 2026-09-04 the scheduled run (cron '10 1 * * 6' = 5:10pm PT Friday) fetched Yahoo bars that
ENDED THURSDAY 09-03, computed Thursday's signal, appended history RunDate 2026-09-03, PUT it to
RTDB, deployed Pages and sent a success notification. Every step "succeeded". Nothing in the
pipeline asked whether the data was the data it was supposed to be. The workflow comment claimed
Yahoo is "definitely updated" by 5:10pm PT; that claim was disproved by the data it produced.

This gate turns that silent wrong answer into a loud absence of an answer:

    A RED RUN BEATS A GREEN RUN THAT LIED.

CONTRACT
--------
Runs AFTER fetch_tqqq.py and BEFORE weekly_report.py. For an OFFICIAL run it computes the expected
as-of date (the most recent COMPLETED trading day) and compares it against the newest bar in BOTH
data/TQQQ.csv and data/QQQ.csv. If either is behind, it re-fetches up to MAX_RETRIES times,
RETRY_SLEEP_S apart. If it is still behind, it exits NONZERO having written nothing — so every
publish step downstream (history commit, RTDB PUT, Pages, notification) never runs.

  - EITHER symbol stale ⇒ stale. A signal computed from a fresh TQQQ and a stale QQQ is a signal
    computed from two different days, which is worse than no signal.
  - ASOF_DATE set ⇒ that date IS the expectation. A deliberate backfill knows its own date, and
    this gate must not out-vote it.
  - MODE != official ⇒ skipped. Debug runs publish nothing, so there is nothing to protect.

WHY THE GATE, NOT THE SCHEDULE, IS THE FIX
------------------------------------------
The companion schedule change (01:10Z → 02:10Z) buys an hour of settlement margin. It is NOT the
fix: this gate would have caught the 09-04 incident at the OLD time too, because the expectation
(Friday's close, complete by 21:00Z Friday) was already met at 01:10Z Saturday. The margin only
reduces how often the retry loop has to work for its living.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
TQQQ_CSV = BASE / "data" / "TQQQ.csv"
QQQ_CSV = BASE / "data" / "QQQ.csv"

# Overridable ONLY so the gate can be exercised without waiting an hour; the workflow sets neither,
# so production always gets the real budget. 6 retries x 10 min = a 1h worst case, well inside the
# 6h Actions job limit (and inside the ~4h gap before the GT-platform Saturday watchdog fires).
MAX_RETRIES = int(os.environ.get("GATE_MAX_RETRIES") or 6)
RETRY_SLEEP_S = int(os.environ.get("GATE_RETRY_SLEEP_S") or 600)
# 21:00Z is 5pm EDT / 4pm EST: the first instant at which a US cash session has closed under EITHER
# DST regime. Deliberately conservative — being an hour late to expect a bar costs nothing, while
# being an hour early would make the gate demand a bar that does not exist yet and fail a good run.
SETTLE_HOUR_UTC = 21


# ---------------------------------------------------------------------------
# NYSE calendar — computed BY RULE, not from a hardcoded table.
#
# A table has a coverage cliff: the year it runs out, the gate either starts false-alarming or
# starts silently guessing. Every recurring NYSE closure is expressible as a rule, so there is no
# cliff and no maintenance. The residual limit is stated in the report: AD-HOC closures (a national
# day of mourning, a hurricane) are not rule-expressible and are not modelled — on such a day the
# gate expects a bar that will never come and fails the run loudly, which is the safe direction.
# ---------------------------------------------------------------------------
def easter(year: int) -> date:
    """Anonymous Gregorian algorithm. Good Friday = Easter − 2 days, and it IS an NYSE holiday
    even though it is not a US federal holiday — which is exactly why pandas'
    USFederalHolidayCalendar is the wrong calendar for this job."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th `weekday` (Mon=0) of a month."""
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """NYSE observance: a Saturday holiday moves to the preceding Friday, a Sunday one to the
    following Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year: int) -> set[date]:
    hol = {
        _observed(date(year, 1, 1)),                 # New Year's Day
        _nth_weekday(year, 1, 0, 3),                 # MLK Jr. Day — 3rd Monday of January
        _nth_weekday(year, 2, 0, 3),                 # Washington's Birthday — 3rd Monday of February
        easter(year) - timedelta(days=2),            # Good Friday
        _last_weekday(year, 5, 0),                   # Memorial Day — last Monday of May
        _observed(date(year, 6, 19)),                # Juneteenth
        _observed(date(year, 7, 4)),                 # Independence Day
        _nth_weekday(year, 9, 0, 1),                 # Labor Day — 1st Monday of September
        _nth_weekday(year, 11, 3, 4),                # Thanksgiving — 4th Thursday of November
        _observed(date(year, 12, 25)),               # Christmas Day
    }
    # New Year's Day falling on a Saturday is NOT observed on the preceding Friday (Dec 31) — the
    # NYSE stays open. _observed() would move it back into the prior year, so drop that case.
    if date(year, 1, 1).weekday() == 5:
        hol.discard(date(year - 1, 12, 31))
        hol.discard(date(year, 1, 1) - timedelta(days=1))
    return hol


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def expected_asof(now_utc: datetime) -> date:
    """The most recent COMPLETED trading day: the latest trading day whose close (SETTLE_HOUR_UTC)
    is already in the past. On a holiday Friday this correctly returns Thursday, so Good Friday
    does not raise a false alarm."""
    d = now_utc.date()
    for _ in range(30):
        if is_trading_day(d):
            close = datetime(d.year, d.month, d.day, SETTLE_HOUR_UTC, tzinfo=timezone.utc)
            if close <= now_utc:
                return d
        d -= timedelta(days=1)
    raise RuntimeError("no completed trading day found in the last 30 days — calendar is wrong")


# ---------------------------------------------------------------------------
def newest_bar(path: Path) -> date | None:
    """Last Date in a fetched CSV. Returns None when the file is missing/empty/unparseable —
    which the gate treats as STALE, never as 'probably fine'."""
    try:
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if len(lines) < 2:
            return None
        return date.fromisoformat(lines[-1].split(",")[0].strip()[:10])
    except Exception:
        return None


def check(expected: date) -> tuple[bool, dict]:
    seen = {"TQQQ": newest_bar(TQQQ_CSV), "QQQ": newest_bar(QQQ_CSV)}
    # BOTH must reach the expectation. One fresh symbol and one stale symbol is not a partial
    # success; it is a signal computed from two different days.
    fresh = all(v is not None and v >= expected for v in seen.values())
    return fresh, seen


def _fmt(seen: dict) -> str:
    return ", ".join(f"{k}={v.isoformat() if v else 'MISSING'}" for k, v in sorted(seen.items()))


def run(now_utc: datetime | None = None, sleeper=time.sleep, refetch=None) -> int:
    now_utc = now_utc or datetime.now(timezone.utc)
    mode = (os.environ.get("MODE") or "debug").strip().lower()
    asof_raw = (os.environ.get("ASOF_DATE") or "").strip()

    if mode != "official":
        print(f"[gate] MODE={mode} — skipped. Debug runs publish nothing, so there is nothing to protect.")
        return 0

    if asof_raw:
        try:
            expected = date.fromisoformat(asof_raw[:10])
        except ValueError:
            print(f"[gate] FAIL — ASOF_DATE={asof_raw!r} is not YYYY-MM-DD.")
            return 1
        print(f"[gate] ASOF_DATE={expected} overrides the computed expectation (a deliberate backfill knows its own date).")
    else:
        expected = expected_asof(now_utc)
        print(f"[gate] expected as-of {expected} — the most recent COMPLETED trading day as of {now_utc.isoformat()}.")

    def _refetch():
        # Imported LAZILY, inside the retry path: the happy path must not depend on being able to
        # import the fetcher at all. (Found by running it — an import failure here crashed the gate
        # with a traceback instead of reporting, on a run whose data was fine.)
        if refetch is not None:
            return refetch()
        import fetch_tqqq
        return fetch_tqqq.main()

    for attempt in range(MAX_RETRIES + 1):
        fresh, seen = check(expected)
        if fresh:
            print(f"[gate] PASS — {_fmt(seen)} (expected >= {expected}).")
            return 0
        print(f"[gate] STALE on attempt {attempt + 1}/{MAX_RETRIES + 1} — {_fmt(seen)} (expected >= {expected}).")
        if attempt == MAX_RETRIES:
            break
        print(f"[gate] re-fetching in {RETRY_SLEEP_S}s…")
        sleeper(RETRY_SLEEP_S)
        try:
            _refetch()
        except Exception as e:                      # a failed re-fetch is a stale attempt, not a crash
            print(f"[gate] re-fetch raised: {e}")

    fresh, seen = check(expected)
    print(
        "[gate] FAIL — data never reached the expected as-of after "
        f"{MAX_RETRIES + 1} attempts over ~{MAX_RETRIES * RETRY_SLEEP_S // 60} minutes.\n"
        f"[gate] expected >= {expected}; newest {_fmt(seen)}.\n"
        "[gate] PUBLISHING NOTHING: no history row, no RTDB PUT, no Pages deploy, no success notification.\n"
        "[gate] A red run beats a green run that lied. Re-run from the Actions tab once the data settles."
    )
    return 1


# ---------------------------------------------------------------------------
def self_test() -> int:
    """The minimal honest self-check this repo did not have. No network, no sleeping."""
    n = 0
    fails = []

    def ok(cond, msg):
        nonlocal n
        n += 1
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    print("[1] the NYSE calendar is computed by rule")
    ok(easter(2026) == date(2026, 4, 5), f"Easter 2026 = {easter(2026)} (expected 2026-04-05)")
    ok(date(2026, 4, 3) in nyse_holidays(2026), "Good Friday 2026-04-03 is a holiday (it is NOT a federal one)")
    ok(not is_trading_day(date(2026, 4, 3)), "…so it is not a trading day")
    ok(date(2026, 9, 7) in nyse_holidays(2026), "Labor Day 2026-09-07")
    ok(date(2026, 11, 26) in nyse_holidays(2026), "Thanksgiving 2026-11-26")
    ok(date(2026, 7, 3) in nyse_holidays(2026), "July 4 2026 falls on a Saturday → observed Friday July 3")
    ok(is_trading_day(date(2026, 9, 4)), "2026-09-04 (the incident Friday) IS a trading day")
    ok(not is_trading_day(date(2026, 9, 5)), "…and Saturday is not")

    print("\n[2] the expectation is the most recent COMPLETED trading day")
    utc = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    ok(expected_asof(utc("2026-09-05T02:10:00")) == date(2026, 9, 4),
       "the NEW cron time (Sat 02:10Z) expects Friday 09-04")
    ok(expected_asof(utc("2026-09-05T01:10:00")) == date(2026, 9, 4),
       "and so did the OLD one (Sat 01:10Z) — the GATE is the fix, the schedule shift is only margin")
    ok(expected_asof(utc("2026-09-04T15:00:00")) == date(2026, 9, 3),
       "mid-session Friday expects Thursday — an unclosed day is not a completed one")
    ok(expected_asof(utc("2026-09-04T21:00:00")) == date(2026, 9, 4),
       "at the settle hour exactly, Friday counts")
    ok(expected_asof(utc("2026-04-04T02:10:00")) == date(2026, 4, 2),
       "GOOD FRIDAY: the Saturday run expects THURSDAY 04-02, not the holiday Friday — no false alarm")
    ok(expected_asof(utc("2026-09-08T02:00:00")) == date(2026, 9, 4),
       "after Labor Day Monday, the expectation is still Friday 09-04")

    print("\n[3] the gate itself — stale fixture exits nonzero and writes nothing")
    import tempfile, shutil
    global TQQQ_CSV, QQQ_CSV
    keep = (TQQQ_CSV, QQQ_CSV)
    tmp = Path(tempfile.mkdtemp())
    try:
        TQQQ_CSV, QQQ_CSV = tmp / "TQQQ.csv", tmp / "QQQ.csv"
        os.environ["MODE"] = "official"
        os.environ.pop("ASOF_DATE", None)
        now = utc("2026-09-05T02:10:00")
        calls = {"n": 0}

        def write(tq, qq):
            TQQQ_CSV.write_text("Date,Close\n2020-01-02,100\n%s,101\n" % tq)
            QQQ_CSV.write_text("Date,Close\n2020-01-02,200\n%s,201\n" % qq)

        # THE INCIDENT, REPRODUCED: both symbols end Thursday when Friday was expected.
        write("2026-09-03", "2026-09-03")
        rc = run(now, sleeper=lambda s: None, refetch=lambda: calls.__setitem__("n", calls["n"] + 1))
        ok(rc == 1, "the 09-04 incident data (both ending Thu 09-03) EXITS NONZERO — it would have been caught")
        ok(calls["n"] == MAX_RETRIES, f"after {MAX_RETRIES} re-fetch attempts (got {calls['n']})")

        # One fresh, one stale is still stale.
        write("2026-09-04", "2026-09-03")
        ok(run(now, sleeper=lambda s: None, refetch=lambda: None) == 1,
           "a FRESH TQQQ with a STALE QQQ is stale — a signal from two different days is worse than none")
        write("2026-09-03", "2026-09-04")
        ok(run(now, sleeper=lambda s: None, refetch=lambda: None) == 1, "and the same the other way round")

        # Fresh passes, first time, with no retries.
        write("2026-09-04", "2026-09-04")
        calls["n"] = 0
        ok(run(now, sleeper=lambda s: None, refetch=lambda: calls.__setitem__("n", calls["n"] + 1)) == 0,
           "FRESH data (both ending Fri 09-04) PASSES")
        ok(calls["n"] == 0, "on the first check, with no re-fetch")

        # A retry that succeeds mid-loop passes.
        write("2026-09-03", "2026-09-03")
        state = {"n": 0}

        def heal():
            state["n"] += 1
            if state["n"] >= 2:
                write("2026-09-04", "2026-09-04")

        ok(run(now, sleeper=lambda s: None, refetch=heal) == 0, "data that arrives late PASSES once it arrives")
        ok(state["n"] == 2, "after exactly the retries it needed (got %d)" % state["n"])

        # Missing / unparseable files are stale, never 'probably fine'.
        TQQQ_CSV.unlink()
        ok(run(now, sleeper=lambda s: None, refetch=lambda: None) == 1, "a MISSING data file is stale, not assumed fine")
        write("2026-09-04", "2026-09-04")
        TQQQ_CSV.write_text("Date,Close\ngarbage\n")
        ok(run(now, sleeper=lambda s: None, refetch=lambda: None) == 1, "and an unparseable one likewise")

        # Overrides and skips.
        write("2026-09-03", "2026-09-03")
        os.environ["ASOF_DATE"] = "2026-09-03"
        ok(run(now, sleeper=lambda s: None, refetch=lambda: None) == 0,
           "an explicit ASOF_DATE overrides the expectation — a deliberate backfill knows its own date")
        os.environ["ASOF_DATE"] = "not-a-date"
        ok(run(now, sleeper=lambda s: None, refetch=lambda: None) == 1, "a malformed ASOF_DATE fails loudly rather than being ignored")
        os.environ.pop("ASOF_DATE", None)
        os.environ["MODE"] = "debug"
        ok(run(now, sleeper=lambda s: None, refetch=lambda: None) == 0, "a DEBUG run is skipped — it publishes nothing to protect")
        os.environ["MODE"] = "official"
    finally:
        TQQQ_CSV, QQQ_CSV = keep
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{n - len(fails)}/{n} checks passed" + ("" if not fails else f" — {len(fails)} FAILED"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else run())
