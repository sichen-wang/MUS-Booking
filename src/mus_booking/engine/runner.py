"""每晚的编排，以及随时的补交。

- 20:55 为第二天有预定的成员登录学校系统；登录失败要区分“密码被拒”（等用户处理）和“暂时连不上”（稍后重试）。
- 20:59:30 重读日历页，并为每一段预先取好表单。
- 21:00 起每个成员按时间先后逐段提交，段与段之间隔 1 秒；成员之间互不等待。
- 平时有任何变动（新增、恢复、开机）时做一次补交：已经开放、还没提交、也还没结束的预定立即提交。

提交前先把这一次的各段“认领”为排队中，此后这一次就不再随用户的修改而变。每段发出前标为“已发出”，
得到答复后记下结果；服务中途停下时，排队的段下次接着提交，已发出的去“我的预定”里核对。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..domain import Outcome, State, next_opening, open_at
from ..portal import (
    DEFAULT_PHONE,
    Form,
    LoginRejected,
    NeedsUser,
    PortalError,
    Profile,
    School,
    Uncertain,
    Unreachable,
    Verdict,
)
from ..store import Claimed, Member, Store
from .clock import Clock

log = logging.getLogger("mus_booking.engine")

LOGIN_LEAD = timedelta(minutes=5)
PREPARE_LEAD = timedelta(seconds=30)
FORM_AGE = 2.2
"""取到表单后至少等这么多秒再提交（学校要求 2 秒，多留一点余量）。"""
GAP = 1.0
"""同一个成员两段之间的间隔。"""
NOT_OPEN_WINDOW = 30.0
"""收到“尚未开放”后，每秒重试，最多持续这么久。"""
NETWORK_RETRIES = 3
TOO_FAST_RETRIES = 3
SWEEP_EVERY = 600.0
BACKOFF_FIRST = 120.0
BACKOFF_MAX = 3600.0

Connect = Callable[[Iterable[dict[str, str]]], School]


@dataclass
class _Account:
    """一个已经登录的成员及其学校会话。"""

    member: Member
    school: School
    profile: Profile
    version: int

    @property
    def phone(self) -> str:
        return self.member.phone or DEFAULT_PHONE


@dataclass
class Status:
    running: bool = False
    last_sweep: datetime | None = None
    last_error: str = ""
    nights: dict[datetime, str] = field(default_factory=dict)


class Engine:
    def __init__(self, store: Store, clock: Clock, connect: Connect) -> None:
        self.store = store
        self.clock = clock
        self.connect = connect
        self.status = Status()
        self._locks: dict[int, asyncio.Lock] = {}
        self._wake = asyncio.Event()
        self._backoff: dict[int, tuple[float, float]] = {}

    # ---- 主循环 ---------------------------------------------------------------

    async def run(self) -> None:
        """服务的后台任务：随时补交，每晚开抢。"""
        self.status.running = True
        sweeper = asyncio.create_task(self._sweep_forever())
        try:
            while True:
                opening = next_opening(self.clock.now())
                await self.night(opening)
                await self.clock.sleep_until(opening + timedelta(seconds=1))
        finally:
            self.status.running = False
            sweeper.cancel()

    def poke(self) -> None:
        """有了变动，尽快检查一遍有没有需要立即提交的。"""
        self._wake.set()

    async def _sweep_forever(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.sweep()
            except Exception as error:  # 后台任务不能因为一次意外而停下
                log.exception("补交时出错")
                self.status.last_error = f"{type(error).__name__}: {error}"
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=SWEEP_EVERY)

    # ---- 每晚 -----------------------------------------------------------------

    async def night(self, opening: datetime) -> None:
        """一次开抢的全过程。opening 是开放时刻，提交的是它第二天的预定。"""
        await self.clock.sleep_until(opening - LOGIN_LEAD)
        day = opening.date() + timedelta(days=1)
        members = [m for m in self.store.members() if m.login_ok and self._has_plans(m.id, day)]
        if not members:
            return
        self.store.start_run(opening, self.clock.now())
        log.info("%s 开抢：%d 位成员", opening.strftime("%m-%d %H:%M"), len(members))
        work = asyncio.gather(*(self._night_for(m, opening) for m in members))
        await self.clock.sleep_until(opening)
        self.poke()  # 20:55 以后才添加预定的成员，到点交给补交处理
        skews = await work
        measured = [skew for skew in skews if skew is not None]
        self.store.finish_run(opening, self.clock.now(), round(sum(measured) / len(measured)) if measured else None)

    def _has_plans(self, member_id: int, day: date) -> bool:
        return any(o.state is State.PLANNED for o in self.store.schedule(member_id).on(day))

    async def _night_for(self, member: Member, opening: datetime) -> int | None:
        """一个成员的一晚：登录、准备、开抢。返回与学校服务器的时间差（毫秒）。"""
        async with self._lock(member.id):
            account = None
            for attempt in range(NETWORK_RETRIES):
                account, retry = await self._sign_in(member.id, ignore_backoff=True)
                if account or not retry or self.clock.now() >= opening:
                    break
                await self.clock.sleep(60.0 if attempt == 0 else 30.0)
            if account is None:
                return None
            try:
                await self.clock.sleep_until(opening - PREPARE_LEAD)
                skew = await self._refresh(account)
                prepared = await self._prepare(account, opening)
                await self.clock.sleep_until(opening)
                await self._work(account, prepared)
                return skew
            finally:
                await self._sign_out(account)

    async def _prepare(self, account: _Account, opening: datetime) -> dict[tuple, tuple[Form, float]]:
        """为开放时刻要提交的各段预先取好表单。取不到的到时候再取。"""
        prepared: dict[tuple, tuple[Form, float]] = {}
        day = opening.date() + timedelta(days=1)
        for occurrence in self.store.schedule(account.member.id).on(day):
            if occurrence.state is not State.PLANNED:
                continue
            for span in occurrence.span.split(account.profile.time_limit):
                try:
                    form = await account.school.form(account.profile, occurrence.room, day, span)
                except PortalError as error:
                    log.warning("成员 %s 预取表单失败：%s", account.member.id, error)
                    continue
                prepared[(occurrence.booking.id, day, occurrence.room, span)] = (form, self.clock.monotonic())
        return prepared

    # ---- 补交 -----------------------------------------------------------------

    async def sweep(self) -> None:
        """立即提交所有已经开放、还没提交的预定，并处理上次没有完成的段。"""
        now = self.clock.now()
        self.status.last_sweep = now
        tasks = []
        for member in self.store.members():
            if not member.login_ok or self._lock(member.id).locked():
                continue
            if not self.store.schedule(member.id).due(now) and not self.store.unsettled(member.id):
                continue
            tasks.append(self._sweep_for(member.id))
        await asyncio.gather(*tasks)

    async def _sweep_for(self, member_id: int) -> None:
        async with self._lock(member_id):
            account, _ = await self._sign_in(member_id)
            if account is None:
                return
            try:
                await self._refresh(account)
                await self._work(account)
            finally:
                await self._sign_out(account)

    # ---- 提交 -----------------------------------------------------------------

    async def _work(self, account: _Account, prepared: dict[tuple, tuple[Form, float]] | None = None) -> None:
        """认领所有到期的预定，然后按时间先后逐段提交。"""
        member_id = account.member.id
        now = self.clock.now()
        for occurrence in self.store.schedule(member_id).due(now):
            room = account.profile.room(occurrence.room)
            self.store.claim(
                member_id,
                occurrence.booking.id,
                occurrence.day,
                occurrence.span.split(account.profile.time_limit),
                occurrence.room,
                room.name if room else occurrence.room,
                now,
            )
        first = True
        for segment in self.store.unsettled(member_id):
            if segment.outcome is Outcome.SENT:
                await self._reconcile(account, segment)
                continue
            if segment.span.starts_on(segment.day) <= self.clock.now():
                self.store.settle(segment.id, Outcome.FAILED, "时间已过", self.clock.now())
                continue
            if not first:
                await self.clock.sleep(GAP)
            first = False
            await self._submit(account, segment, (prepared or {}).pop(_key(segment), None))

    async def _submit(self, account: _Account, segment: Claimed, ready: tuple[Form, float] | None) -> None:
        school, profile = account.school, account.profile
        form, fetched = ready if ready else (None, 0.0)
        not_open_since: float | None = None
        refetched = False
        network_failures = too_fast = 0
        while True:
            if form is None:
                try:
                    form = await school.form(profile, segment.room, segment.day, segment.span)
                except Unreachable as error:
                    network_failures += 1
                    if network_failures >= NETWORK_RETRIES:
                        return self._fail(segment, str(error))
                    await self.clock.sleep(2.0)
                    continue
                except PortalError as error:
                    return self._fail(segment, str(error))
                fetched = self.clock.monotonic()
            await self.clock.sleep(FORM_AGE - (self.clock.monotonic() - fetched))
            await self.clock.sleep_until(open_at(segment.day))
            self.store.mark_sent(segment.id, form.booking_id, self.clock.now())
            try:
                reply = await school.submit(profile, form, account.phone)
            except Uncertain:
                return await self._reconcile(account, segment, form.booking_id)
            except Unreachable as error:
                network_failures += 1
                if network_failures >= NETWORK_RETRIES:
                    return self._fail(segment, str(error))
                await self.clock.sleep(2.0)
                continue
            if reply.verdict is Verdict.BOOKED:
                self.store.settle(segment.id, Outcome.BOOKED, reply.message, self.clock.now())
                log.info("成员 %s 订到 %s %s", account.member.id, segment.day, segment.span)
                return None
            if reply.verdict is Verdict.TOO_FAST:
                too_fast += 1
                if too_fast > TOO_FAST_RETRIES:
                    return self._fail(segment, reply.message)
                await self.clock.sleep(2.0)
            elif reply.verdict is Verdict.NOT_OPEN:
                not_open_since = self.clock.monotonic() if not_open_since is None else not_open_since
                if self.clock.monotonic() - not_open_since >= NOT_OPEN_WINDOW:
                    return self._fail(segment, reply.message)
                await self.clock.sleep(1.0)
            elif reply.verdict is Verdict.STALE and not refetched:
                refetched, form = True, None
            else:
                return self._fail(segment, reply.message)

    async def _reconcile(self, account: _Account, segment: Claimed, school_id: str = "") -> None:
        """提交后没收到答复：去“我的预定”里看这一段有没有订上。查不到时保持原状，下次再查。"""
        school_id = school_id or segment.school_id
        try:
            records = await account.school.my_bookings()
        except PortalError as error:
            log.warning("成员 %s 核对“我的预定”失败：%s", account.member.id, error)
            return
        found = any(
            record.status == "2"
            and ((school_id and record.id == school_id) or record.matches(segment.room_name, segment.day, segment.span))
            for record in records
        )
        if found:
            self.store.settle(segment.id, Outcome.BOOKED, "已在“我的预定”中确认", self.clock.now())
        else:
            self._fail(segment, "提交后没有收到答复，“我的预定”里也没有这一段")

    def _fail(self, segment: Claimed, message: str) -> None:
        self.store.settle(segment.id, Outcome.FAILED, message, self.clock.now())
        log.info("段 %s 没有订上：%s", segment.id, message)

    # ---- 登录 -----------------------------------------------------------------

    async def _sign_in(self, member_id: int, *, ignore_backoff: bool = False) -> tuple[_Account | None, bool]:
        """登录一个成员。返回（登录好的账号，失败时是否值得稍后重试）。"""
        member = self.store.member(member_id)
        if member is None or not member.login_ok:
            return None, False
        retry_at = self._backoff.get(member_id, (0.0, 0.0))[0]
        if not ignore_backoff and self.clock.monotonic() < retry_at:
            return None, True
        credentials = self.store.credentials(member_id)
        school = self.connect(credentials.cookies)
        try:
            await school.sign_in(member.student_id, credentials.password)
            profile = self.store.profile(member_id) or await school.read_profile()
        except (LoginRejected, NeedsUser) as error:
            await school.aclose()
            self.store.mark_login(member_id, False, str(error), credentials.version)
            log.warning("成员 %s 登录失效：%s", member_id, error)
            return None, False
        except PortalError as error:
            await school.aclose()
            self._back_off(member_id)
            log.warning("成员 %s 暂时无法登录：%s", member_id, error)
            return None, True
        self._backoff.pop(member_id, None)
        self.store.save_cookies(member_id, school.cookies(), credentials.version)
        return _Account(member, school, profile, credentials.version), False

    async def _refresh(self, account: _Account) -> int | None:
        """重读日历页，更新规则编号和琴房清单。返回学校服务器与本机的时间差（毫秒）。"""
        try:
            profile, server_time = await account.school.read_calendar(account.profile)
        except PortalError as error:
            log.warning("成员 %s 重读日历页失败，沿用上次的内容：%s", account.member.id, error)
            return None
        account.profile = profile
        self.store.save_profile(account.member.id, profile, self.clock.now())
        if server_time is None:
            return None
        return round((server_time - self.clock.now()).total_seconds() * 1000)

    async def _sign_out(self, account: _Account) -> None:
        self.store.save_cookies(account.member.id, account.school.cookies(), account.version)
        await account.school.aclose()

    def _back_off(self, member_id: int) -> None:
        _, delay = self._backoff.get(member_id, (0.0, 0.0))
        delay = min(BACKOFF_MAX, delay * 2 if delay else BACKOFF_FIRST)
        self._backoff[member_id] = (self.clock.monotonic() + delay, delay)

    def _lock(self, member_id: int) -> asyncio.Lock:
        return self._locks.setdefault(member_id, asyncio.Lock())


def _key(segment: Claimed) -> tuple:
    return (segment.booking_id, segment.day, segment.room, segment.span)
