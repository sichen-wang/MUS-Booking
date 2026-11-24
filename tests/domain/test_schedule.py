from datetime import date, datetime, timedelta

import pytest

from mus_booking.domain import (
    CHINA,
    Booking,
    Frozen,
    Outcome,
    Schedule,
    Segment,
    Span,
    State,
    rehomed_skips,
    retire,
    revise,
)

TUE = date(2026, 11, 24)  # 星期二
WED = TUE + timedelta(days=1)
THU = TUE + timedelta(days=2)
NEXT_TUE = TUE + timedelta(days=7)
START = datetime(2026, 11, 24, 0, 0, tzinfo=CHINA)


def span(text: str) -> Span:
    start, end = text.split("-")
    return Span.parse(start, end)


def booking(id: int, when: str, first: date, *, weekly=False, room="r1", **extra) -> Booking:
    return Booking(member_id=1, room=room, span=span(when), first=first, weekly=weekly, id=id, **extra)


def segment(booking_id: int, day: date, when: str, outcome=Outcome.BOOKED) -> Segment:
    return Segment(booking_id=booking_id, day=day, span=span(when), outcome=outcome)


def china(day: date, hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=CHINA)


# ---- 展开 ----------------------------------------------------------------------


def test_weekly_and_one_off_occurrences():
    weekly = booking(1, "19:00-21:00", THU, weekly=True)
    once = booking(2, "09:00-11:00", WED)
    schedule = Schedule(bookings=(weekly, once))
    week = dict(schedule.upcoming(TUE, 14))
    assert [d for d, found in week.items() if found] == [WED, THU, THU + timedelta(days=7)]


def test_weekly_series_respects_its_last_day():
    series = booking(1, "19:00-21:00", TUE, weekly=True, last=NEXT_TUE)
    assert series.occurs_on(NEXT_TUE)
    assert not series.occurs_on(NEXT_TUE + timedelta(days=7))
    assert not series.occurs_on(TUE - timedelta(days=7))


def test_states_follow_segments_skips_and_pause():
    series = booking(1, "19:00-21:00", TUE, weekly=True)
    paused = booking(2, "09:00-10:00", TUE, weekly=True, paused=True)
    schedule = Schedule(
        bookings=(series, paused),
        skips=frozenset({(1, NEXT_TUE)}),
        segments=(segment(1, TUE, "19:00-21:00"),),
    )
    assert [o.state for o in schedule.on(TUE)] == [State.PAUSED, State.SUBMITTED]
    assert [o.state for o in schedule.on(NEXT_TUE)] == [State.PAUSED, State.SKIPPED]
    assert [o.state for o in schedule.on(NEXT_TUE + timedelta(days=7))] == [State.PAUSED, State.PLANNED]


def test_a_submitted_day_keeps_its_result_even_when_paused_later():
    series = booking(1, "13:00-19:00", TUE, weekly=True, paused=True)
    schedule = Schedule(bookings=(series,), segments=(segment(1, TUE, "13:00-15:00"),))
    assert schedule.on(TUE)[0].state is State.SUBMITTED


@pytest.mark.parametrize(
    ("outcomes", "label", "tone"),
    [
        ((Outcome.BOOKED, Outcome.BOOKED, Outcome.BOOKED), "预定成功", "success"),
        ((Outcome.BOOKED, Outcome.BOOKED, Outcome.FAILED), "2/3 成功", "partial"),
        ((Outcome.FAILED, Outcome.FAILED), "预定失败", "fail"),
        ((Outcome.BOOKED, Outcome.SENT, Outcome.QUEUED), "预定中", "waiting"),
    ],
)
def test_result_labels(outcomes, label, tone):
    spans = ["13:00-15:00", "15:00-17:00", "17:00-19:00"]
    segments = tuple(segment(1, TUE, spans[i], outcome) for i, outcome in enumerate(outcomes))
    schedule = Schedule(bookings=(booking(1, "13:00-19:00", TUE),), segments=segments)
    result = schedule.on(TUE)[0].result
    assert (result.label, result.tone) == (label, tone)


# ---- 待提交 --------------------------------------------------------------------


def test_tomorrow_becomes_due_at_21_tonight():
    schedule = Schedule(bookings=(booking(1, "09:00-11:00", WED),))
    assert schedule.due(china(TUE, "20:59")) == []
    assert [o.day for o in schedule.due(china(TUE, "21:00"))] == [WED]


def test_today_is_due_until_it_ends():
    schedule = Schedule(bookings=(booking(1, "09:00-11:00", TUE), booking(2, "13:00-15:00", TUE)))
    assert [o.booking.id for o in schedule.due(china(TUE, "10:30"))] == [1, 2]
    assert [o.booking.id for o in schedule.due(china(TUE, "11:00"))] == [2]


def test_skipped_paused_and_submitted_are_not_due():
    schedule = Schedule(
        bookings=(
            booking(1, "09:00-10:00", WED, weekly=True),
            booking(2, "10:00-11:00", WED, paused=True, weekly=True),
            booking(3, "11:00-12:00", WED),
            booking(4, "12:00-13:00", WED),
        ),
        skips=frozenset({(1, WED)}),
        segments=(segment(3, WED, "11:00-12:00"),),
    )
    assert [o.booking.id for o in schedule.due(china(TUE, "21:00"))] == [4]


def test_the_day_after_tomorrow_is_never_due():
    schedule = Schedule(bookings=(booking(1, "09:00-10:00", THU),))
    assert schedule.due(china(TUE, "23:00")) == []


# ---- 校验 ----------------------------------------------------------------------


def test_overlap_with_a_planned_booking():
    trial = Schedule(bookings=(booking(1, "13:00-15:00", WED), booking(2, "14:00-16:00", WED)))
    assert trial.problem(2, START) == "时间与已有的预定重叠"


def test_back_to_back_bookings_do_not_overlap():
    trial = Schedule(bookings=(booking(1, "13:00-15:00", WED), booking(2, "15:00-17:00", WED)))
    assert trial.problem(2, START) is None


def test_failed_segments_free_their_time_and_booked_ones_hold_it():
    base = booking(1, "13:00-17:00", TUE)
    segments = (segment(1, TUE, "13:00-15:00", Outcome.FAILED), segment(1, TUE, "15:00-17:00"))
    retry = booking(2, "13:00-15:00", TUE, room="r2")
    clash = booking(3, "16:00-17:00", TUE, room="r2")
    assert Schedule(bookings=(base, retry), segments=segments).problem(2, START) is None
    assert Schedule(bookings=(base, clash), segments=segments).problem(3, START) == "时间与已有的预定重叠"


def test_a_weekly_series_clashes_with_a_one_off_on_its_weekday():
    trial = Schedule(bookings=(booking(1, "09:00-11:00", NEXT_TUE), booking(2, "10:00-12:00", TUE, weekly=True)))
    assert trial.problem(2, START) == "时间与已有的预定重叠"


def test_paused_and_skipped_occurrences_do_not_block():
    paused = booking(1, "09:00-11:00", TUE, weekly=True, paused=True)
    skipped = booking(2, "13:00-15:00", WED)
    trial = Schedule(
        bookings=(paused, skipped, booking(3, "09:00-11:00", TUE), booking(4, "13:00-15:00", WED)),
        skips=frozenset({(2, WED)}),
    )
    assert trial.problem(3, START) is None
    assert trial.problem(4, START) is None


def test_hours_already_used_earlier_in_the_week_count():
    # 周五想加 3 小时；周一到周四已经订成功 58 小时
    friday = date(2026, 11, 27)
    used = []
    for offset in range(4):
        day = date(2026, 11, 23) + timedelta(days=offset)
        used.append(booking(10 + offset, "00:00-14:30", day))
    segments = tuple(
        segment(b.id, b.first, f"{h:02d}:00-{h + 2:02d}:00") for b in used for h in range(0, 14, 2)
    ) + tuple(segment(b.id, b.first, "14:00-14:30") for b in used)
    candidate = booking(99, "09:00-12:00", friday, room="r9")
    trial = Schedule(bookings=(*used, candidate), segments=segments)
    assert trial.week_minutes(friday, china(friday, "00:00")) == 58 * 60 + 180
    assert trial.problem(99, china(friday, "00:00")) == "超过每周 60 小时"
    assert trial.with_bookings(booking(99, "09:00-11:00", friday)).problem(99, china(friday, "00:00")) is None


def test_weekly_series_that_add_up_past_60_hours_are_rejected():
    full_days = [booking(i, "00:00-23:30", TUE + timedelta(days=i), weekly=True) for i in range(2)]
    extra = booking(9, "08:00-22:00", TUE + timedelta(days=3), weekly=True)  # 47 + 14 = 61
    trial = Schedule(bookings=(*full_days, extra))
    assert trial.problem(9, START) == "超过每周 60 小时"
    assert trial.with_bookings(booking(9, "08:00-21:00", extra.first, weekly=True)).problem(9, START) is None


def test_missed_plans_before_today_do_not_count():
    trial = Schedule(bookings=(booking(1, "00:00-23:30", TUE - timedelta(days=1)),))
    assert trial.week_minutes(TUE, START) == 0


# ---- 修改与删除 ----------------------------------------------------------------


def test_an_unsubmitted_booking_is_edited_in_place():
    old = booking(5, "19:00-21:00", THU, weekly=True, paused=True)
    kept, new = revise(Schedule(bookings=(old,)), old, booking(0, "18:00-20:00", THU, weekly=True))
    assert kept is None
    assert (new.id, str(new.span), new.paused) == (5, "18:00–20:00", True)


def test_editing_a_submitted_series_splits_it_after_the_last_submitted_day():
    old = booking(5, "13:00-19:00", TUE, weekly=True)
    schedule = Schedule(bookings=(old,), segments=(segment(5, TUE, "13:00-15:00"),))
    kept, new = revise(schedule, old, booking(0, "13:00-19:00", TUE, weekly=True, room="r2"))
    assert kept.last == TUE
    assert (new.id, new.first, new.room) == (0, NEXT_TUE, "r2")


def test_a_series_moved_to_another_weekday_starts_on_the_chosen_day():
    old = booking(5, "13:00-15:00", TUE, weekly=True)
    schedule = Schedule(bookings=(old,), segments=(segment(5, TUE, "13:00-15:00"),))
    _, new = revise(schedule, old, booking(0, "13:00-15:00", THU, weekly=True))
    assert new.first == THU


def test_a_submitted_one_off_cannot_be_edited_or_deleted():
    old = booking(5, "13:00-15:00", TUE)
    schedule = Schedule(bookings=(old,), segments=(segment(5, TUE, "13:00-15:00", Outcome.FAILED),))
    with pytest.raises(Frozen):
        revise(schedule, old, booking(0, "15:00-17:00", TUE))
    with pytest.raises(Frozen):
        retire(schedule, old)


def test_turning_a_submitted_series_into_a_one_off_on_a_submitted_day_is_refused():
    old = booking(5, "13:00-15:00", TUE, weekly=True)
    schedule = Schedule(bookings=(old,), segments=(segment(5, TUE, "13:00-15:00"),))
    with pytest.raises(Frozen):
        revise(schedule, old, booking(0, "16:00-17:00", TUE))
    kept, new = revise(schedule, old, booking(0, "16:00-17:00", WED))
    assert (kept.last, new.first, new.weekly) == (TUE, WED, False)


def test_deleting_keeps_submitted_days_of_a_series():
    old = booking(5, "13:00-15:00", TUE, weekly=True)
    assert retire(Schedule(bookings=(old,)), old) is None
    schedule = Schedule(bookings=(old,), segments=(segment(5, TUE, "13:00-15:00"),))
    assert retire(schedule, old).last == TUE


def test_skips_follow_the_edit():
    old = booking(5, "13:00-15:00", TUE, weekly=True)
    later = NEXT_TUE + timedelta(days=7)
    schedule = Schedule(
        bookings=(old,),
        skips=frozenset({(5, NEXT_TUE), (5, later)}),
        segments=(segment(5, TUE, "13:00-15:00"),),
    )
    kept, new = revise(schedule, old, booking(0, "14:00-16:00", TUE, weekly=True))
    assert rehomed_skips(schedule, old, kept, new) == ({NEXT_TUE, later}, {NEXT_TUE, later})

    unsubmitted = Schedule(bookings=(old,), skips=frozenset({(5, NEXT_TUE)}))
    kept, moved = revise(unsubmitted, old, booking(0, "14:00-16:00", WED, weekly=True))
    assert rehomed_skips(unsubmitted, old, kept, moved) == ({NEXT_TUE}, set())


def test_an_ended_plan_that_was_never_submitted_frees_its_hours():
    ended = booking(1, "09:00-10:00", TUE)
    schedule = Schedule(bookings=(ended,))
    assert schedule.week_minutes(TUE, china(TUE, "09:30")) == 60
    assert schedule.week_minutes(TUE, china(TUE, "12:00")) == 0


def test_turning_a_series_into_a_one_off_on_a_skipped_day_drops_the_skip():
    old = booking(5, "13:00-15:00", TUE, weekly=True)
    schedule = Schedule(bookings=(old,), skips=frozenset({(5, NEXT_TUE)}))
    kept, new = revise(schedule, old, booking(0, "13:00-15:00", NEXT_TUE))
    assert rehomed_skips(schedule, old, kept, new) == ({NEXT_TUE}, set())


def test_a_series_starting_beyond_three_weeks_is_still_checked():
    far = TUE + timedelta(days=28)
    first = booking(1, "10:00-12:00", far, weekly=True)
    second = booking(2, "11:00-13:00", far, weekly=True)
    assert Schedule(bookings=(first, second)).problem(2, START) == "时间与已有的预定重叠"
