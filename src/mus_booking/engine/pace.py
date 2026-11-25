"""开抢时的节奏和先后：像一个手快的人那样一段一段地订，而不是在开放的一瞬间把所有段同时发出。

- 每个成员的第一段在开放后 3–6 秒发出，之后每段在上一段收到答复后再隔 2–5 秒，都随机。
- 先后按时段的中点分三档：晚上（18:00 起）先于下午（12:00 起），下午先于早上；
  同一档里越抢手越先，抢手程度差不多的随机排，免得别人摸出固定的顺序。
"""

from __future__ import annotations

import random
from collections.abc import Callable, Hashable, Iterable
from typing import TypeVar

from ..domain import Span

FIRST = (3.0, 6.0)
"""每个成员的第一段在开放后多少秒发出。"""
GAP = (2.0, 5.0)
"""同一个成员相邻两段之间隔多少秒，从上一段收到答复算起。"""
EVENING = 18 * 60
AFTERNOON = 12 * 60
KEEP = 1000
"""最多记住多少段的随机数。"""

T = TypeVar("T")


def tier(span: Span) -> int:
    """按中点分档：晚上 2，下午 1，早上 0。17:00–19:00 这样跨在 18:00 上的算晚上。"""
    middle = (span.start + span.end) / 2
    return 2 if middle >= EVENING else 1 if middle >= AFTERNOON else 0


def priority(span: Span, hot: float, jitter: float) -> float:
    """档位每差一级相差 2，抢手程度在 0 到 1 之间，随机数只占 0.5：档位定大顺序，其次抢手程度，最后随机。"""
    return 2 * tier(span) + hot + 0.5 * jitter


class Pace:
    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self._jitter: dict[Hashable, float] = {}

    def first(self) -> float:
        return self._between(FIRST)

    def gap(self) -> float:
        return self._between(GAP)

    def rank(
        self,
        items: Iterable[T],
        *,
        key: Callable[[T], Hashable],
        span: Callable[[T], Span],
        hot: Callable[[T], float],
    ) -> list[T]:
        """从先到后排好。同一段的随机数在一晚之内不变，预取表单和提交时排出来的先后一致。"""
        return sorted(items, key=lambda item: -priority(span(item), hot(item), self._jitter_of(key(item))))

    def _jitter_of(self, key: Hashable) -> float:
        if key not in self._jitter:
            while len(self._jitter) >= KEEP:  # 字典按放入的先后排列，先淘汰最早的，当晚的段不受影响
                del self._jitter[next(iter(self._jitter))]
            self._jitter[key] = self._rng.random()
        return self._jitter[key]

    def _between(self, bounds: tuple[float, float]) -> float:
        low, high = bounds
        return low + (high - low) * self._rng.random()
