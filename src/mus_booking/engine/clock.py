"""时钟。生产环境用系统时钟；测试用虚拟时钟，时间只在所有任务都在等待时才前进。"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from collections.abc import Awaitable
from datetime import datetime, timedelta
from typing import Protocol, TypeVar

from ..domain import CHINA

T = TypeVar("T")


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...

    async def sleep_until(self, moment: datetime) -> None: ...


class SystemClock:
    """开抢时刻按经 NTP 同步的系统时间判断；两次请求之间的间隔用单调时钟计。"""

    def now(self) -> datetime:
        return datetime.now(CHINA)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))

    async def sleep_until(self, moment: datetime) -> None:
        # 分段睡，系统时间被校正时也不会睡过头；临近目标时刻时看得更勤
        while (remaining := (moment - self.now()).total_seconds()) > 0:  # noqa: ASYNC110 等的是时刻，不是事件
            await asyncio.sleep(min(remaining, 30.0 if remaining > 90 else 0.5))


class VirtualClock:
    """测试用的时钟：`run` 驱动一个协程，所有任务都在等待时，把时间拨到最早的那个唤醒点。"""

    def __init__(self, start: datetime) -> None:
        self._start = start
        self._elapsed = 0.0
        self._waiters: list[tuple[float, int, asyncio.Future[None]]] = []
        self._order = itertools.count()

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self._elapsed)

    def monotonic(self) -> float:
        return self._elapsed

    def set(self, moment: datetime) -> None:
        self._elapsed = max(self._elapsed, (moment - self._start).total_seconds())

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (self._elapsed + seconds, next(self._order), future))
        await future

    async def sleep_until(self, moment: datetime) -> None:
        await self.sleep((moment - self.now()).total_seconds())

    async def run(self, work: Awaitable[T]) -> T:
        task = asyncio.ensure_future(work)
        while True:
            await _settle()
            if task.done():
                return task.result()
            while self._waiters and self._waiters[0][2].cancelled():
                heapq.heappop(self._waiters)
            if not self._waiters:
                raise RuntimeError("所有任务都在等待，但没有任何唤醒点")
            deadline, _, future = heapq.heappop(self._waiters)
            self._elapsed = max(self._elapsed, deadline)
            future.set_result(None)


async def _settle() -> None:
    for _ in range(200):
        await asyncio.sleep(0)
