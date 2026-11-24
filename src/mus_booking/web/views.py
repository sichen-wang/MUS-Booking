"""把排程整理成页面要显示的样子：日子怎么称呼、状态写什么、下一次自动预定是哪一条。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..domain import Occurrence, Outcome, Room, Schedule, Segment, State, open_at
from ..portal import Profile

WEEKDAYS = "一二三四五六日"


def weekday(day: date) -> str:
    return f"周{WEEKDAYS[day.weekday()]}"


def day_name(day: date, today: date) -> str:
    offset = (day - today).days
    return {0: "今天", 1: "明天"}.get(offset, weekday(day))


def short_date(day: date) -> str:
    return f"{day.month}/{day.day}"


def long_date(day: date, today: date) -> str:
    return f"{day_name(day, today)} · {day.month} 月 {day.day} 日"


def moment_name(moment: datetime, now: datetime) -> str:
    """开放时刻的叫法：今晚 21:00、明晚 21:00、周三 21:00。"""
    offset = (moment.date() - now.date()).days
    clock = f"{moment:%H:%M}"
    if offset == 0:
        return f"今晚 {clock}" if moment.hour >= 18 else f"今天 {clock}"
    if offset == 1:
        return f"明晚 {clock}" if moment.hour >= 18 else f"明天 {clock}"
    return f"{weekday(moment.date())} {clock}"


@dataclass(frozen=True)
class Badge:
    label: str
    tone: str  # success / partial / fail / waiting / quiet


@dataclass(frozen=True)
class Item:
    occurrence: Occurrence
    code: str
    piano: str
    badge: Badge | None
    struck: bool

    @property
    def booking_id(self) -> int:
        return self.occurrence.booking.id

    @property
    def weekly(self) -> bool:
        return self.occurrence.booking.weekly


@dataclass(frozen=True)
class Day:
    day: date
    name: str
    date: str
    items: list[Item]


@dataclass(frozen=True)
class Next:
    when: str
    what: str


def room_of(profile: Profile | None, room_id: str) -> Room:
    found = profile.room(room_id) if profile else None
    return found or Room(room_id, room_id)


def badge(occurrence: Occurrence, now: datetime, login_ok: bool) -> Badge | None:
    if occurrence.state is State.SKIPPED:
        return Badge("已跳过", "quiet")
    if occurrence.state is State.PAUSED:
        return Badge("已暂停", "quiet")
    if occurrence.state is State.SUBMITTED:
        result = occurrence.result
        assert result is not None
        return Badge(result.label, result.tone)
    if now >= open_at(occurrence.day) and occurrence.span.ends_on(occurrence.day) > now:
        return Badge("预定中", "waiting") if login_ok else Badge("登录失效", "fail")
    return None


def item(occurrence: Occurrence, profile: Profile | None, now: datetime, login_ok: bool) -> Item:
    room = room_of(profile, occurrence.room)
    struck = occurrence.state in (State.SKIPPED, State.PAUSED)
    return Item(occurrence, room.code, room.piano, badge(occurrence, now, login_ok), struck)


def week(schedule: Schedule, profile: Profile | None, now: datetime, login_ok: bool) -> list[Day]:
    today = now.date()
    return [
        Day(day, day_name(day, today), short_date(day), [item(o, profile, now, login_ok) for o in occurrences])
        for day, occurrences in schedule.upcoming(today)
    ]


def busy(days: list[Day]) -> bool:
    """有正在提交的，首页就隔几秒刷新一次。"""
    return any(i.badge and i.badge.tone == "waiting" for d in days for i in d.items)


def next_run(days: list[Day], now: datetime) -> Next | None:
    """首页顶部：下一次自动预定的时刻，以及那时要提交的第一条。"""
    for day in days:
        planned = [i for i in day.items if i.occurrence.state is State.PLANNED and now < open_at(day.day)]
        if planned:
            first = planned[0]
            more = f" 等 {len(planned)} 条" if len(planned) > 1 else ""
            what = f"{day.name} {first.occurrence.span} · {first.code}{more}"
            return Next(moment_name(open_at(day.day), now), what)
    return None


@dataclass(frozen=True)
class SegmentRow:
    span: str
    label: str
    tone: str


def segment_rows(segments: tuple[Segment, ...]) -> list[SegmentRow]:
    labels = {
        Outcome.BOOKED: ("预定成功", "success"),
        Outcome.FAILED: ("预定失败", "fail"),
        Outcome.QUEUED: ("预定中", "waiting"),
        Outcome.SENT: ("预定中", "waiting"),
    }
    return [SegmentRow(str(s.span), *labels[s.outcome]) for s in segments]


def initial(name: str, fallback: str) -> str:
    """头像上显示的字：姓名的第一个字，没有姓名时用学号末两位。"""
    name = (name or "").strip()
    return name[:1] if name else fallback[-2:]


def date_choices(today: date) -> list[tuple[date, str, int]]:
    return [
        (day, "今天" if day == today else weekday(day), day.day) for day in (today + timedelta(n) for n in range(7))
    ]
