"""Recurring-trip rules (a safe subset of iCalendar RRULE).

We never hand a user-supplied rule straight to the parser: exotic rules (BYSECOND, huge COUNTs)
can make it burn CPU. Only the parts a transport company needs are allowed.
"""

import re
from datetime import date, datetime, time

from dateutil.rrule import rrulestr

ALLOWED_PARTS = {"FREQ", "INTERVAL", "BYDAY", "COUNT"}
ALLOWED_FREQ = {"DAILY", "WEEKLY", "MONTHLY"}
_DAY = re.compile(r"^[+-]?[1-5]?(MO|TU|WE|TH|FR|SA|SU)$")


def validate_rrule(rule: str) -> str:
    """Return the rule normalised, or raise ValueError with a human-readable reason."""
    parts: dict[str, str] = {}
    for chunk in rule.strip().upper().split(";"):
        key, sep, value = chunk.partition("=")
        if not sep or not value:
            raise ValueError("malformed rule; example: FREQ=WEEKLY;BYDAY=MO,WE,FR")
        if key not in ALLOWED_PARTS or key in parts:
            raise ValueError(f"unsupported or repeated part '{key}' (allowed: FREQ, INTERVAL, BYDAY, COUNT)")
        parts[key] = value
    if parts.get("FREQ") not in ALLOWED_FREQ:
        raise ValueError("FREQ must be DAILY, WEEKLY or MONTHLY")
    if "INTERVAL" in parts and not (parts["INTERVAL"].isdigit() and 1 <= int(parts["INTERVAL"]) <= 52):
        raise ValueError("INTERVAL must be 1-52")
    if "COUNT" in parts and not (parts["COUNT"].isdigit() and 1 <= int(parts["COUNT"]) <= 1000):
        raise ValueError("COUNT must be 1-1000")
    if "BYDAY" in parts:
        days = parts["BYDAY"].split(",")
        if len(days) > 7 or not all(_DAY.match(d) for d in days):
            raise ValueError("BYDAY must look like MO,WE,FR")
    return ";".join(f"{k}={v}" for k, v in parts.items())


def occurrences(rule: str, start: date, lo: date, hi: date) -> list[date]:
    """Dates (inclusive) from `lo` to `hi` on which the rule fires. `start` anchors the pattern."""
    r = rrulestr(rule, dtstart=datetime.combine(start, time.min))
    return [d.date() for d in r.between(datetime.combine(lo, time.min), datetime.combine(hi, time.max), inc=True)]