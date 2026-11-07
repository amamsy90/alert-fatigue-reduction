"""How many load-generator replicas a company should run at a clock time.

steady: one replica.
daily: quiet overnight, busier through the day, a peak late morning and evening.
bursty: one replica, then four replicas for ten minutes every third hour.
"""
from __future__ import annotations

from datetime import datetime, timedelta


def replicas_for(profile: str, when: datetime, burst_until: datetime | None) -> tuple[int, datetime | None]:
    if profile == "steady":
        return 1, None
    if profile == "daily":
        hour = when.hour
        if 11 <= hour < 13 or 18 <= hour < 20:
            return 3, None
        if 8 <= hour < 22:
            return 2, None
        return 1, None
    if profile == "bursty":
        if burst_until is not None and when < burst_until:
            return 4, burst_until
        if when.minute < 5 and when.hour % 3 == 0:
            return 4, when + timedelta(minutes=10)
        return 1, None
    raise ValueError(f"unknown load profile {profile}")
