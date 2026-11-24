"""时间：一律按中国时间计算，一天内的时段以半小时为一格。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

CHINA = timezone(timedelta(hours=8), "CST")
SLOT = 30
DAY = 24 * 60
OPEN_TIME = time(21, 0)
"""学校在前一天的这个时刻开放预定。"""


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_hhmm(text: str) -> int:
    hours, sep, minutes = text.strip().partition(":")
    if not sep or len(minutes) != 2 or not hours.isdigit() or not minutes.isdigit():
        raise ValueError(f"不是 HH:MM 格式的时间：{text!r}")
    value = int(hours) * 60 + int(minutes)
    if int(minutes) >= 60 or value > DAY:
        raise ValueError(f"不是有效的时间：{text!r}")
    return value


@dataclass(frozen=True, order=True, slots=True)
class Span:
    """一天之内的一段时间，以分钟计，两端都落在半小时的格线上。"""

    start: int
    end: int

    def __post_init__(self) -> None:
        if not (0 <= self.start < self.end <= DAY) or self.start % SLOT or self.end % SLOT:
            raise ValueError(f"不是有效的时段：{self.start}–{self.end}")

    @classmethod
    def parse(cls, start: str, end: str) -> Span:
        return cls(parse_hhmm(start), parse_hhmm(end))

    @property
    def minutes(self) -> int:
        return self.end - self.start

    def overlaps(self, other: Span) -> bool:
        return self.start < other.end and other.start < self.end

    def split(self, limit: int) -> tuple[Span, ...]:
        """按单次上限从头切开，例如上限 2 小时时 13:00–18:00 切成 2、2、1 小时三段。"""
        if limit < SLOT:
            raise ValueError(f"单次上限太小：{limit}")
        limit -= limit % SLOT
        parts = []
        start = self.start
        while start < self.end:
            end = min(start + limit, self.end)
            parts.append(Span(start, end))
            start = end
        return tuple(parts)

    def starts_on(self, day: date) -> datetime:
        return at(day, self.start)

    def ends_on(self, day: date) -> datetime:
        return at(day, self.end)

    def __str__(self) -> str:
        return f"{hhmm(self.start)}–{hhmm(self.end)}"


def at(day: date, minutes: int) -> datetime:
    return datetime.combine(day, time(), CHINA) + timedelta(minutes=minutes)


def open_at(day: date) -> datetime:
    """这一天的时段从什么时候起可以预定。"""
    return datetime.combine(day - timedelta(days=1), OPEN_TIME, CHINA)


def next_opening(now: datetime) -> datetime:
    """此刻之后（含此刻）最近的一次开放。"""
    candidate = datetime.combine(now.astimezone(CHINA).date(), OPEN_TIME, CHINA)
    return candidate if candidate >= now else candidate + timedelta(days=1)


def monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def days(start: date, count: int) -> list[date]:
    return [start + timedelta(days=offset) for offset in range(count)]
