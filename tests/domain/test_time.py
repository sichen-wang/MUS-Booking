from datetime import date, datetime

import pytest

from mus_booking.domain import CHINA, Span, hhmm, monday, next_opening, open_at, parse_hhmm


def test_hhmm_round_trip():
    assert hhmm(0) == "00:00"
    assert hhmm(23 * 60 + 30) == "23:30"
    assert parse_hhmm("09:30") == 570
    assert parse_hhmm("24:00") == 1440


@pytest.mark.parametrize("text", ["9", "9:3", "09:60", "24:30", "ab:cd", ""])
def test_parse_hhmm_rejects_malformed_text(text):
    with pytest.raises(ValueError):
        parse_hhmm(text)


@pytest.mark.parametrize(("start", "end"), [(60, 60), (120, 60), (-30, 60), (1410, 1470), (15, 60), (0, 45)])
def test_span_must_be_a_forward_range_on_the_half_hour_grid(start, end):
    with pytest.raises(ValueError):
        Span(start, end)


def test_span_text_and_overlap():
    afternoon = Span.parse("13:00", "15:00")
    assert str(afternoon) == "13:00–15:00"
    assert afternoon.minutes == 120
    assert afternoon.overlaps(Span.parse("14:30", "16:00"))
    assert not afternoon.overlaps(Span.parse("15:00", "17:00"))
    assert not afternoon.overlaps(Span.parse("11:00", "13:00"))


@pytest.mark.parametrize(
    ("start", "end", "parts"),
    [
        ("13:00", "19:00", ["13:00–15:00", "15:00–17:00", "17:00–19:00"]),
        ("13:00", "18:00", ["13:00–15:00", "15:00–17:00", "17:00–18:00"]),
        ("13:00", "15:00", ["13:00–15:00"]),
        ("23:00", "23:30", ["23:00–23:30"]),
        ("09:00", "14:30", ["09:00–11:00", "11:00–13:00", "13:00–14:30"]),
    ],
)
def test_split_cuts_from_the_start_by_the_limit(start, end, parts):
    assert [str(part) for part in Span.parse(start, end).split(120)] == parts


def test_split_rounds_an_odd_limit_down_to_the_grid():
    assert [str(p) for p in Span.parse("10:00", "12:00").split(100)] == ["10:00–11:30", "11:30–12:00"]


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 11, 26), datetime(2026, 11, 25, 21, 0, tzinfo=CHINA)),
        (date(2026, 12, 1), datetime(2026, 11, 30, 21, 0, tzinfo=CHINA)),
        (date(2027, 1, 1), datetime(2026, 12, 31, 21, 0, tzinfo=CHINA)),
    ],
)
def test_a_day_opens_at_21_the_evening_before(day, expected):
    assert open_at(day) == expected


def test_next_opening():
    def at(hour, minute=0, second=0):
        return datetime(2026, 11, 25, hour, minute, second, tzinfo=CHINA)

    assert next_opening(at(9)) == at(21)
    assert next_opening(at(21)) == at(21)
    assert next_opening(at(21, 0, 1)) == datetime(2026, 11, 26, 21, 0, tzinfo=CHINA)


def test_next_opening_uses_china_time_whatever_the_input_zone():
    from datetime import timedelta, timezone

    pacific = timezone(timedelta(hours=-8))
    # 太平洋时间 11 月 25 日 06:00 = 中国时间 11 月 25 日 22:00，已过当天的 21:00
    assert next_opening(datetime(2026, 11, 25, 6, 0, tzinfo=pacific)) == datetime(2026, 11, 26, 21, 0, tzinfo=CHINA)


def test_monday_crosses_months():
    assert monday(date(2026, 12, 3)) == date(2026, 11, 30)
    assert monday(date(2026, 11, 30)) == date(2026, 11, 30)
