from datetime import date, datetime

from mus_booking.domain import CHINA, Booking, Outcome, Schedule, Segment, Span
from mus_booking.web import views

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=CHINA)  # 星期二
TUE = NOW.date()


def at(day: int, hour: int) -> datetime:
    return datetime(2026, 11, day, hour, 0, tzinfo=CHINA)


def test_day_names():
    assert [views.day_name(date(2026, 11, d), TUE) for d in (24, 25, 26, 30)] == ["今天", "明天", "周四", "周一"]
    assert views.long_date(date(2026, 11, 26), TUE) == "周四 · 11 月 26 日"
    assert views.short_date(date(2026, 12, 1)) == "12/1"


def test_moment_names():
    assert views.moment_name(at(24, 21), NOW) == "今晚 21:00"
    assert views.moment_name(at(25, 21), NOW) == "明晚 21:00"
    assert views.moment_name(at(25, 9), NOW) == "明天 09:00"
    assert views.moment_name(at(26, 21), NOW) == "周四 21:00"


def booking(id: int, when: str, first: date, **extra) -> Booking:
    start, end = when.split("-")
    return Booking(1, "r1", Span.parse(start, end), first, id=id, **extra)


def test_badges_follow_the_state():
    schedule = Schedule(
        bookings=(
            booking(1, "09:00-10:00", TUE),
            booking(2, "11:00-12:00", TUE),
            booking(3, "13:00-14:00", date(2026, 11, 25), weekly=True, paused=True),
            booking(4, "15:00-16:00", date(2026, 11, 26)),
        ),
        segments=(Segment(1, TUE, Span.parse("09:00", "10:00"), Outcome.BOOKED),),
    )
    days = dict((d.day, d) for d in views.week(schedule, None, NOW, login_ok=True))
    today = [(i.code, i.badge) for i in days[TUE].items]
    assert today == [("r1", views.Badge("预定成功", "success")), ("r1", views.Badge("预定中", "waiting"))]
    assert days[date(2026, 11, 25)].items[0].badge == views.Badge("已暂停", "quiet")
    assert days[date(2026, 11, 26)].items[0].badge is None
    assert views.busy(list(days.values()))
    broken = dict((d.day, d) for d in views.week(schedule, None, NOW, login_ok=False))
    assert broken[TUE].items[1].badge == views.Badge("登录失效", "fail")


def test_next_run_points_at_the_first_plan_still_to_open():
    schedule = Schedule(
        bookings=(booking(1, "19:00-21:00", date(2026, 11, 27)), booking(2, "08:00-09:00", date(2026, 11, 27)))
    )
    days = views.week(schedule, None, NOW, login_ok=True)
    nxt = views.next_run(days, NOW)
    assert (nxt.when, nxt.what) == ("周四 21:00", "周五 08:00–09:00 · r1 等 2 条")
    assert views.next_run(views.week(Schedule(), None, NOW, login_ok=True), NOW) is None


def test_initials():
    assert views.initial("林知夏", "123090421") == "林"
    assert views.initial("", "123090421") == "21"
