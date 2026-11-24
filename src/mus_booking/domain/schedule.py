"""预定、段与排程：首页的“未来 7 天”、重叠与每周时长的校验、修改每周预定时的拆分。

一条**预定**是用户选定的连续时间范围，可以每周重复。提交时它被切成若干**段**，每段单独记录结果。
一次**出现**是某条预定落在某一天的那一次。某一次只要有了段的记录，就算已经提交，此后不再改动。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import StrEnum

from .time import CHINA, Span, days, monday, open_at

WEEKLY_LIMIT = 60 * 60
"""学校规定每人每周最多预定 60 小时。"""

HORIZON = 7
"""首页显示的天数；单次预定只能选在这几天里。"""

_CHECK_DAYS = 21
"""校验的范围：三周足以覆盖未来 7 天和之后照常重复的一整周。"""


class Outcome(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    BOOKED = "booked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Segment:
    booking_id: int
    day: date
    span: Span
    outcome: Outcome = Outcome.QUEUED
    message: str = ""
    at: datetime | None = None
    id: int = 0

    @property
    def holds_time(self) -> bool:
        return self.outcome is not Outcome.FAILED

    @property
    def settled(self) -> bool:
        return self.outcome in (Outcome.BOOKED, Outcome.FAILED)


@dataclass(frozen=True, slots=True)
class Booking:
    member_id: int
    room: str
    span: Span
    first: date
    weekly: bool = False
    last: date | None = None
    paused: bool = False
    id: int = 0

    def occurs_on(self, day: date) -> bool:
        if day < self.first or (self.last is not None and day > self.last):
            return False
        return day.weekday() == self.first.weekday() if self.weekly else day == self.first


class State(StrEnum):
    PLANNED = "planned"
    SKIPPED = "skipped"
    PAUSED = "paused"
    SUBMITTED = "submitted"


@dataclass(frozen=True, slots=True)
class Result:
    booked: int
    failed: int
    waiting: int

    @property
    def total(self) -> int:
        return self.booked + self.failed + self.waiting

    @property
    def tone(self) -> str:
        if self.waiting:
            return "waiting"
        if not self.failed:
            return "success"
        return "partial" if self.booked else "fail"

    @property
    def label(self) -> str:
        return {
            "waiting": "预定中",
            "success": "预定成功",
            "fail": "预定失败",
            "partial": f"{self.booked}/{self.total} 成功",
        }[self.tone]


@dataclass(frozen=True, slots=True)
class Occurrence:
    booking: Booking
    day: date
    state: State
    segments: tuple[Segment, ...] = ()

    @property
    def span(self) -> Span:
        return self.booking.span

    @property
    def room(self) -> str:
        return self.booking.room

    @property
    def submitted(self) -> bool:
        return self.state is State.SUBMITTED

    @property
    def result(self) -> Result | None:
        if not self.segments:
            return None
        outcomes = [segment.outcome for segment in self.segments]
        return Result(
            booked=outcomes.count(Outcome.BOOKED),
            failed=outcomes.count(Outcome.FAILED),
            waiting=outcomes.count(Outcome.QUEUED) + outcomes.count(Outcome.SENT),
        )

    def holds(self) -> tuple[Span, ...]:
        """这一次占着哪些时间：计划中的整段，或已提交而没有失败的各段。"""
        if self.state is State.PLANNED:
            return (self.span,)
        if self.state is State.SUBMITTED:
            return tuple(segment.span for segment in self.segments if segment.holds_time)
        return ()

    def minutes(self, today: date) -> int:
        """计入每周时长的分钟数。今天以前没提交的计划已经错过，不计。"""
        if self.state is State.PLANNED:
            return self.span.minutes if self.day >= today else 0
        return sum(span.minutes for span in self.holds())


class Frozen(Exception):
    """要改动的那一次已经提交过了。"""


@dataclass(frozen=True)
class Schedule:
    """一个成员的全部预定、跳过和已提交的段。各方法都是纯计算。"""

    bookings: tuple[Booking, ...] = ()
    skips: frozenset[tuple[int, date]] = frozenset()
    segments: tuple[Segment, ...] = ()
    _index: dict[tuple[int, date], tuple[Segment, ...]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        index: dict[tuple[int, date], list[Segment]] = defaultdict(list)
        for segment in self.segments:
            index[segment.booking_id, segment.day].append(segment)
        sorted_index = {key: tuple(sorted(value, key=lambda s: s.span)) for key, value in index.items()}
        object.__setattr__(self, "_index", sorted_index)

    # ---- 展开 -----------------------------------------------------------------

    def occurrence(self, booking: Booking, day: date) -> Occurrence:
        segments = self._index.get((booking.id, day), ())
        if segments:
            state = State.SUBMITTED
        elif (booking.id, day) in self.skips:
            state = State.SKIPPED
        elif booking.paused:
            state = State.PAUSED
        else:
            state = State.PLANNED
        return Occurrence(booking, day, state, segments)

    def on(self, day: date) -> list[Occurrence]:
        found = [self.occurrence(booking, day) for booking in self.bookings if booking.occurs_on(day)]
        return sorted(found, key=lambda occurrence: (occurrence.span, occurrence.booking.id))

    def upcoming(self, today: date, count: int = HORIZON) -> list[tuple[date, list[Occurrence]]]:
        return [(day, self.on(day)) for day in days(today, count)]

    def find(self, booking_id: int, day: date) -> Occurrence | None:
        booking = self.booking(booking_id)
        if booking is None or not booking.occurs_on(day):
            return None
        return self.occurrence(booking, day)

    def booking(self, booking_id: int) -> Booking | None:
        return next((booking for booking in self.bookings if booking.id == booking_id), None)

    def submitted_days(self, booking_id: int) -> list[date]:
        return sorted({day for (owner, day) in self._index if owner == booking_id})

    def due(self, now: datetime) -> list[Occurrence]:
        """已经开放、还没提交、也还没结束的各次，按时间先后排列。"""
        today = now.astimezone(CHINA).date()
        found = []
        for day in (today, today + timedelta(days=1)):
            if now < open_at(day):
                continue
            found += [o for o in self.on(day) if o.state is State.PLANNED and o.span.ends_on(day) > now]
        return found

    # ---- 校验 -----------------------------------------------------------------

    def week_minutes(self, week: date, today: date) -> int:
        return sum(o.minutes(today) for day in days(monday(week), 7) for o in self.on(day))

    def problem(self, booking_id: int, today: date) -> str | None:
        """这条预定在接下来几周里会不会与别的预定重叠，或让某一周超过 60 小时。"""
        weeks: set[date] = set()
        for day in days(today, _CHECK_DAYS):
            occurrences = self.on(day)
            for mine in occurrences:
                if mine.booking.id != booking_id or mine.state is not State.PLANNED:
                    continue
                weeks.add(monday(day))
                for other in occurrences:
                    if other is not mine and any(span.overlaps(mine.span) for span in other.holds()):
                        return "时间与已有的预定重叠"
        if any(self.week_minutes(week, today) > WEEKLY_LIMIT for week in sorted(weeks)):
            return "超过每周 60 小时"
        return None

    # ---- 变换 -----------------------------------------------------------------

    def with_bookings(self, *bookings: Booking, without: Iterable[int] = ()) -> Schedule:
        """换掉或加入若干条预定后的排程，用于先校验再保存。"""
        replaced = {booking.id for booking in bookings if booking.id} | set(without)
        kept = tuple(booking for booking in self.bookings if booking.id not in replaced)
        return replace(self, bookings=kept + bookings)

    def with_skips(self, skips: Iterable[tuple[int, date]]) -> Schedule:
        return replace(self, skips=frozenset(skips))


def revise(schedule: Schedule, old: Booking, new: Booking) -> tuple[Booking | None, Booking]:
    """把 old 改成 new 的内容，返回（截短后保留的旧预定，要写入的预定）。

    没有提交过的预定原地修改。每周重复的预定如果已经提交过某几次，旧的那条截止到最后提交的那一天，
    改动从之后第一个对应的日子起生效。单次预定提交过就不能再改。
    """
    frozen = schedule.submitted_days(old.id)
    if not frozen:
        return None, replace(new, id=old.id, paused=old.paused if new.weekly else False)
    if not old.weekly:
        raise Frozen
    last = frozen[-1]
    kept = replace(old, last=last)
    first = new.first
    if new.weekly:
        while first <= last:
            first += timedelta(days=7)
    elif first in frozen:
        raise Frozen
    return kept, replace(new, id=0, first=first, paused=old.paused if new.weekly else False)


def retire(schedule: Schedule, old: Booking) -> Booking | None:
    """删除一条预定。提交过的每周预定保留已提交的那几次，返回截短后的预定；否则返回 None 表示整条删除。"""
    frozen = schedule.submitted_days(old.id)
    if not frozen:
        return None
    if not old.weekly:
        raise Frozen
    return replace(old, last=frozen[-1])


def rehomed_skips(
    schedule: Schedule, old: Booking, kept: Booking | None, new: Booking
) -> tuple[set[date], set[date]]:
    """修改之后，旧预定上要删掉的跳过日期，以及要转给新预定的跳过日期。"""
    mine = {day for owner, day in schedule.skips if owner == old.id}
    if kept is None:
        return {day for day in mine if not new.occurs_on(day)}, set()
    assert kept.last is not None
    later = {day for day in mine if day > kept.last}
    return later, {day for day in later if new.occurs_on(day)}
