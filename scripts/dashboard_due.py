#!/usr/bin/env python3
"""Decide whether a command should regenerate dashboard/index.html.

Rule: regenerate only when (a) now is a NYSE trading day at/after that day's
close (13:00 America/Los_Angeles; 10:00 on early-close days), and (b) the
dashboard has not been committed to main since that close. "Last regen" is
read from git history: the newest commit on main touching dashboard/index.html.

Prints one line — `regenerate: <reason>` or `skip: <reason>` — and exits 0
for regenerate, 1 for skip. Stdlib only.

  python3 scripts/dashboard_due.py [--ref main] [--now 2026-10-26T14:05]
"""
import argparse
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
CLOSE = time(13, 0)
EARLY_CLOSE = time(10, 0)


def nth_weekday(year, month, weekday, n):
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def last_weekday(year, month, weekday):
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def easter(year):
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(year, month, (h + l - 7 * m + 114) % 31 + 1)


def observed(d):
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year):
    days = {
        nth_weekday(year, 1, 0, 3),            # MLK Day
        nth_weekday(year, 2, 0, 3),            # Presidents' Day
        easter(year) - timedelta(days=2),      # Good Friday
        last_weekday(year, 5, 0),              # Memorial Day
        observed(date(year, 6, 19)),           # Juneteenth
        observed(date(year, 7, 4)),            # Independence Day
        nth_weekday(year, 9, 0, 1),            # Labor Day
        nth_weekday(year, 11, 3, 4),           # Thanksgiving
        observed(date(year, 12, 25)),          # Christmas
    }
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:                # NYSE skips the Friday observance
        days.add(observed(new_year))
    return days


def early_closes(year):
    days = {nth_weekday(year, 11, 3, 4) + timedelta(days=1)}  # day after Thanksgiving
    for d in (date(year, 7, 3), date(year, 12, 24)):
        if d.weekday() < 5:
            days.add(d)
    return days


def is_trading_day(d):
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def last_dashboard_commit(ref):
    out = subprocess.run(
        ["git", "log", "-1", "--format=%ct", ref, "--", "dashboard/index.html"],
        capture_output=True, text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return datetime.fromtimestamp(int(out.stdout.strip()), tz=PT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="main")
    ap.add_argument("--now", help="override current time (ISO, America/Los_Angeles)")
    args = ap.parse_args()

    now = datetime.fromisoformat(args.now).replace(tzinfo=PT) if args.now else datetime.now(PT)
    today = now.date()
    stamp = now.strftime("%a %Y-%m-%d %H:%M PT")

    if not is_trading_day(today):
        print(f"skip: {stamp} is not a trading day")
        return 1
    close_t = EARLY_CLOSE if today in early_closes(today.year) else CLOSE
    close = datetime.combine(today, close_t, tzinfo=PT)
    if now < close:
        print(f"skip: {stamp} is before today's {close_t.strftime('%H:%M')} PT close")
        return 1

    last = last_dashboard_commit(args.ref)
    if last is not None and last >= close:
        print(f"skip: dashboard already regenerated since today's close ({last.strftime('%H:%M PT')})")
        return 1
    when = last.strftime("%Y-%m-%d %H:%M PT") if last else "never"
    print(f"regenerate: {stamp} is after the close; last dashboard commit on {args.ref}: {when}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
