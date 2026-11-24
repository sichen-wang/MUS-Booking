"""领域规则：时段、琴房、预定与排程。只做计算，不做任何读写。"""

from .rooms import Room, ranked
from .schedule import (
    HORIZON,
    WEEKLY_LIMIT,
    Booking,
    Frozen,
    Occurrence,
    Outcome,
    Result,
    Schedule,
    Segment,
    State,
    rehomed_skips,
    retire,
    revise,
)
from .time import CHINA, SLOT, Span, hhmm, monday, next_opening, open_at, parse_hhmm

__all__ = [
    "CHINA",
    "HORIZON",
    "SLOT",
    "WEEKLY_LIMIT",
    "Booking",
    "Frozen",
    "Occurrence",
    "Outcome",
    "Result",
    "Room",
    "Schedule",
    "Segment",
    "Span",
    "State",
    "hhmm",
    "monday",
    "next_opening",
    "open_at",
    "parse_hhmm",
    "ranked",
    "rehomed_skips",
    "retire",
    "revise",
]
