from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import Settings
from app.services.freshness import MarketCalendar, evaluate_freshness

CAIRO = ZoneInfo("Africa/Cairo")
S = Settings()
CAL = MarketCalendar(S)


def cairo(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=CAIRO).astimezone(timezone.utc)


def ev(src, fetched, now, has_price=True, settings=S):
    return evaluate_freshness(source_timestamp=src, fetched_at=fetched, has_price=has_price, now=now,
                              settings=settings, calendar=MarketCalendar(settings))


# 2026-10-22 is a Thursday, 2026-10-23 a Friday, 2026-10-25 a Sunday.
def test_calendar_open_closed():
    assert CAL.is_open(cairo(2026, 10, 22, 11, 0))
    assert not CAL.is_open(cairo(2026, 10, 22, 14, 30))   # close is exclusive
    assert not CAL.is_open(cairo(2026, 10, 22, 9, 59))
    assert not CAL.is_open(cairo(2026, 10, 23, 11, 0))     # Friday
    assert not CAL.is_open(cairo(2026, 10, 24, 11, 0))     # Saturday
    assert CAL.is_open(cairo(2026, 10, 25, 11, 0))         # Sunday trades


def test_last_close_and_next_open_skip_weekend():
    assert CAL.last_session_close(cairo(2026, 10, 25, 9, 0)) == cairo(2026, 10, 22, 14, 30)
    assert CAL.next_open(cairo(2026, 10, 22, 15, 0)) == cairo(2026, 10, 25, 10, 0)


def test_fresh_during_session_when_timestamp_recent():
    now = cairo(2026, 10, 22, 11, 0)
    f = ev(now - timedelta(seconds=120), now - timedelta(seconds=30), now)
    assert f.status == "fresh" and f.market_open


def test_stale_during_session_when_timestamp_old_even_if_fetch_succeeded():
    now = cairo(2026, 10, 22, 11, 0)
    f = ev(now - timedelta(minutes=30), now - timedelta(seconds=5), now)    # request just succeeded
    assert f.status == "stale" and f.reason == "source_timestamp_old_during_session"


def test_stale_when_collection_not_recent_even_if_timestamp_recent():
    now = cairo(2026, 10, 22, 11, 0)
    f = ev(now - timedelta(seconds=10), now - timedelta(minutes=20), now)
    assert f.status == "stale" and f.reason == "collection_not_recent"


def test_unavailable_without_price():
    now = cairo(2026, 10, 22, 11, 0)
    assert ev(now, now, now, has_price=False).status == "unavailable"


def test_never_fresh_without_source_timestamp():
    now = cairo(2026, 10, 22, 11, 0)
    assert ev(None, now, now).status == "stale"


def test_future_timestamp_is_stale():
    now = cairo(2026, 10, 22, 11, 0)
    assert ev(now + timedelta(hours=2), now, now).reason == "source_timestamp_in_future"


def test_friday_with_thursday_close_data_is_fresh():
    now = cairo(2026, 10, 23, 12, 0)
    src = datetime(2026, 10, 22, 15, 38, tzinfo=CAIRO)       # the real sample's writeTime
    f = ev(src, now - timedelta(minutes=5), now)
    assert f.status == "fresh" and not f.market_open


def test_friday_with_wednesday_data_is_stale():
    now = cairo(2026, 10, 23, 12, 0)
    f = ev(cairo(2026, 10, 21, 15, 0), now - timedelta(minutes=5), now)
    assert f.status == "stale" and f.reason == "older_than_last_session_close"


def test_midsession_snapshot_is_not_final_after_close():
    now = cairo(2026, 10, 22, 16, 0)
    f = ev(cairo(2026, 10, 22, 12, 0), now - timedelta(minutes=2), now)
    assert f.status == "stale"


def test_holiday_setting_moves_last_session():
    s = dataclasses.replace(S, holidays=(date(2026, 10, 22),))
    cal = MarketCalendar(s)
    assert not cal.is_open(cairo(2026, 10, 22, 11, 0))
    assert cal.last_session_close(cairo(2026, 10, 23, 12, 0)) == cairo(2026, 10, 21, 14, 30)
