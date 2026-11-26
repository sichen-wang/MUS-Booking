"""每晚的编排，以及随时的补交。

- 20:55 为第二天有预定的成员登录学校系统；登录失败要区分“密码被拒”（等用户处理）和“暂时连不上”（稍后重试）。
- 20:59:30 重读日历页，按先后一张接一张地为各段取好表单。
- 21:00 起每个成员像一个手快的人那样逐段提交：第一段在开放后几秒，之后每段在上一段收到答复后再隔一两秒。
  学校不允许同一个账号同时有两笔预定在处理中，所以同一个成员必须等答复；先后和间隔见 `pace`。
  成员之间互不等待。
- 21:02 看一眼第二天的占用情况，记下被别人抢先订走的时段，以后排先后时参考。
- 平时有任何变动（新增、恢复、开机）时做一次补交：已经开放、还没提交、也还没结束的预定立即提交。

提交前先把这一次的各段“认领”为排队中，此后这一次就不再随用户的修改而变。每段发出前标为“已发出”，
得到答复后记下结果；服务中途停下时，排队的段下次接着提交，已发出的去“我的预定”里核对。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from ..domain import Outcome, State, next_opening, open_at
from ..portal import (
    DEFAULT_PHONE,
    DEFAULT_THEME,
    Form,
    LoginRejected,
    NeedsUser,
    PortalError,
    Profile,
    School,
    SessionLost,
    Uncertain,
    Unreachable,
    Verdict,
)
from ..store import Claimed, Member, Store
from .clock import Clock
from .pace import FIRST, Pace

log = logging.getLogger("mus_booking.engine")

LOGIN_LEAD = timedelta(minutes=5)
PREPARE_LEAD = timedelta(seconds=30)
LOGIN_GRACE = timedelta(minutes=2)
"""学校在开放前后一直连不上时，登录重试到开放之后这么久。"""
FORM_AGE = 2.2
"""取到表单后至少等这么多秒再提交（学校要求 2 秒，多留一点余量）。"""
OBSERVE_AFTER = timedelta(minutes=2)
"""开放后多久看一眼占用情况。这时已被别人订走的，就是抢手的时段。"""
NOT_OPEN_WINDOW = 30.0
"""收到“尚未开放”后，每秒重试，最多持续这么久。"""
NETWORK_RETRIES = 3
TOO_FAST_RETRIES = 3
BUSY_RETRIES = 5
BUSY_WAIT = 2.0
"""收到“您有预订正在处理中”后隔多久再试。正常情况下逐段提交不会遇到，答复超时之后才可能。"""
SETTLE_AFTER = timedelta(minutes=10)
"""提交后没收到答复、“我的预定”里也查不到，过了这么久才算失败。"""
SWEEP_EVERY = 600.0
BACKOFF_FIRST = 120.0
BACKOFF_MAX = 3600.0

Connect = Callable[[Iterable[dict[str, str]]], School]
Prepared = dict[tuple, tuple[Form, float]]


@dataclass
class _Account:
    """一个已经登录的成员及其学校会话。"""

    member: Member
    school: School
    profile: Profile
    version: int
    posts: int = 0
    """这次会话里真正发出的提交次数。"""


@dataclass
class Status:
    running: bool = False
    last_sweep: datetime | None = None
    last_error: str = ""


@dataclass
class _Workers:
    """正在为各成员补交的任务。同一个成员同时只有一个。"""

    tasks: dict[int, asyncio.Task[None]] = field(default_factory=dict)

    def busy(self, member_id: int) -> bool:
        task = self.tasks.get(member_id)
        return task is not None and not task.done()


class Engine:
    def __init__(self, store: Store, clock: Clock, connect: Connect, *, pace: Pace | None = None) -> None:
        self.store = store
        self.clock = clock
        self.connect = connect
        self.pace = pace or Pace()
        self.status = Status()
        self._locks: dict[int, asyncio.Lock] = {}
        self._wake = asyncio.Event()
        self._backoff: dict[int, tuple[float, float]] = {}
        self._workers = _Workers()
        self._missed: set[int] = set()

    # ---- 主循环 ---------------------------------------------------------------

    async def run(self) -> None:
        """服务的后台任务：随时补交，每晚开抢。"""
        self.status.running = True
        sweeper = asyncio.create_task(self._sweep_forever())
        try:
            while True:
                opening = next_opening(self.clock.now())
                try:
                    await self.night(opening)
                except Exception as error:  # 一晚出了意外，不能让整个后台任务停下
                    log.exception("开抢时出错")
                    self.status.last_error = f"{type(error).__name__}: {error}"
                await self.clock.sleep_until(opening + timedelta(seconds=1))
        finally:
            self.status.running = False
            workers = list(self._workers.tasks.values())
            for task in (sweeper, *workers):
                task.cancel()
            await asyncio.gather(sweeper, *workers, return_exceptions=True)  # 等它们关掉各自的会话

    def poke(self) -> None:
        """有了变动，尽快检查一遍有没有需要立即提交的。"""
        self._wake.set()

    async def _sweep_forever(self) -> None:
        while True:
            self._wake.clear()
            try:
                self._dispatch()
            except Exception as error:
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
            await self.clock.sleep_until(opening)
            self.poke()  # 20:55 以后才添加的预定，到点交给补交
            return
        self.store.start_run(opening, self.clock.now())
        log.info("%s 开抢：%d 位成员", opening.strftime("%m-%d %H:%M"), len(members))
        work = asyncio.gather(*(self._night_for(m.id, opening) for m in members), return_exceptions=True)
        try:
            await self.clock.sleep_until(opening)
            self.poke()
            results = await work
        except BaseException:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            raise
        skews = []
        for result in results:
            if isinstance(result, BaseException):
                log.error("开抢时一个成员出错：%r", result)
            elif result is not None:
                skews.append(result)
        self.store.finish_run(opening, self.clock.now(), round(sum(skews) / len(skews)) if skews else None)
        self.poke()  # 开抢期间被搁下的补交
        await self._observe(opening, [m.id for m in members])

    def _has_plans(self, member_id: int, day: date) -> bool:
        return any(o.state is State.PLANNED for o in self.store.schedule(member_id).on(day))

    async def _night_for(self, member_id: int, opening: datetime) -> int | None:
        """一个成员的一晚：登录、准备、开抢。返回与学校服务器的时间差（毫秒）。

        登录之后到准备之前不占着这个成员，期间新加的当天预定照样能立即提交。
        """
        prepare_at = opening - PREPARE_LEAD
        account, retry = await self._sign_in(member_id, ignore_backoff=True)
        while account is None and retry and self.clock.now() < opening + LOGIN_GRACE:
            now = self.clock.now()
            pause = min(60.0, (prepare_at - now).total_seconds()) if now < prepare_at else 10.0
            await self.clock.sleep(max(1.0, pause))
            account, retry = await self._sign_in(member_id, ignore_backoff=True)
        if account is None:
            return None
        try:
            await self.clock.sleep_until(prepare_at)
            async with self._lock(member_id):
                if self.store.member(member_id) is None or not await self._resign(account):
                    return None
                skew = await self._refresh(account)
                prepared = await self._prepare(account, opening)
                await self.clock.sleep_until(opening)
                await self._work(account, prepared, at_opening=True)
                return skew
        finally:
            await self._sign_out(account)

    async def _prepare(self, account: _Account, opening: datetime) -> Prepared:
        """按提交的先后，一张接一张地为各段取好表单，不同时发出。

        第一段最早在开放后 3 秒才交，所以取表单可以等到那时；还没取到的，到时候在提交前再取。
        """
        day = opening.date() + timedelta(days=1)
        wanted = self.pace.rank(
            (
                (occurrence, span)
                for occurrence in self.store.schedule(account.member.id).on(day)
                if occurrence.state is State.PLANNED
                for span in occurrence.span.split(account.profile.time_limit)
            ),
            key=lambda item: (item[0].booking.id, day, item[0].room, item[1]),
            span=lambda item: item[1],
            hot=lambda item: self.store.hotness(item[0].room, item[1], day),
        )
        prepared: Prepared = {}
        if not wanted:
            return prepared

        async def fetch() -> None:
            renewed = False
            for occurrence, span in wanted:
                while self.store.member(account.member.id) is not None:
                    try:
                        form = await account.school.form(account.profile, occurrence.room, day, span)
                    except SessionLost as error:
                        if renewed or not await self._resign(account):
                            log.warning("成员 %s 预取表单失败：%s", account.member.id, error)
                            return
                        renewed = True
                        continue
                    except PortalError as error:
                        log.warning("成员 %s 预取表单失败：%s", account.member.id, error)
                        break
                    prepared[(occurrence.booking.id, day, occurrence.room, span)] = (form, self.clock.monotonic())
                    break

        fetching = asyncio.ensure_future(fetch())
        deadline = asyncio.ensure_future(self.clock.sleep_until(opening + timedelta(seconds=FIRST[0])))
        try:
            await asyncio.wait([fetching, deadline], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (fetching, deadline):
                task.cancel()
            await asyncio.gather(fetching, deadline, return_exceptions=True)  # 关掉会话之前，确保不再有请求
        return prepared

    async def _observe(self, opening: datetime, member_ids: list[int]) -> None:
        """开放两分钟后看一眼第二天的占用情况，记下被别人订走的时段。只记琴房和起止时间。"""
        await self.clock.sleep_until(opening + OBSERVE_AFTER)
        day = opening.date() + timedelta(days=1)
        for member_id in member_ids:
            try:
                async with self._lock(member_id):
                    account, _ = await self._sign_in(member_id)
                    if account is None:
                        continue  # 登录不上就换一个成员
                    try:
                        taken = await account.school.occupancy(account.profile, day)
                    except PortalError as error:
                        log.warning("读取 %s 的占用情况失败：%s", day, error)
                        return  # 已经读过一次，今晚不再换人重读
                    finally:
                        await self._sign_out(account)
            finally:
                self._resume(member_id)
            ours = self.store.our_spans(day)
            others = [(t.room, t.start, t.end) for t in taken if (t.room, t.start, t.end) not in ours]
            self.store.record_taken(day, others, self.clock.now())
            log.info("%s 的时段开放两分钟后，别人已订走 %d 段", day, len(others))
            return

    # ---- 补交 -----------------------------------------------------------------

    def _candidates(self) -> list[int]:
        now = self.clock.now()
        found = []
        for member in self.store.members():
            if not member.login_ok:
                continue
            if not (self.store.schedule(member.id).due(now) or self.store.unsettled(member.id)):
                continue
            if self._workers.busy(member.id) or self._lock(member.id).locked():
                # 这次扫不到；等它手上的活做完再扫一遍，免得新加的预定要等到下一轮
                self._missed.add(member.id)
                continue
            found.append(member.id)
        return found

    def _dispatch(self) -> list[asyncio.Task[None]]:
        """为每个需要补交的成员各起一个任务，彼此不等待。"""
        self.status.last_sweep = self.clock.now()
        started = []
        for member_id in self._candidates():
            task = asyncio.create_task(self._sweep_for(member_id))
            self._workers.tasks[member_id] = task
            started.append(task)
        return started

    async def sweep(self) -> None:
        """立即补交一遍，并等它做完。开机和测试时用。"""
        tasks = self._dispatch()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _sweep_for(self, member_id: int) -> None:
        try:
            async with self._lock(member_id):
                account, _ = await self._sign_in(member_id)
                if account is None:
                    return
                try:
                    await self._refresh(account)
                    await self._work(account)
                finally:
                    await self._sign_out(account)
        except Exception:
            log.exception("为成员 %s 补交时出错", member_id)
        finally:
            self._resume(member_id)

    def _resume(self, member_id: int) -> None:
        """占着这个成员的期间有补交被搁下了，放开之后马上再扫一遍。"""
        if member_id in self._missed:
            self._missed.discard(member_id)
            self.poke()

    # ---- 提交 -----------------------------------------------------------------

    async def _work(self, account: _Account, prepared: Prepared | None = None, *, at_opening: bool = False) -> None:
        """认领所有到期的预定，然后逐段提交：每段等上一段的答复回来，再隔一两秒。

        开抢时按 `pace` 排先后；平时补交按时间先后，快要开始的先交，免得排在后面过了时间。
        """
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
        segments = self.store.unsettled(member_id)
        for segment in segments:
            if segment.outcome is Outcome.SENT:
                if self.store.member(member_id) is None:
                    return
                await self._reconcile(account, segment)
        queued = [segment for segment in segments if segment.outcome is Outcome.QUEUED]
        if at_opening:
            queued = self.pace.rank(
                queued,
                key=_key,
                span=lambda segment: segment.span,
                hot=lambda segment: self.store.hotness(segment.room, segment.span, segment.day),
            )
        not_before: datetime | None = None
        for segment in queued:
            if self.store.member(member_id) is None:
                return
            if self._started(segment):
                self._fail(segment, "时间已过")
                continue
            if not_before is None:  # 第一段：开放后几秒；白天补交时开放早已过去，不必等
                not_before = open_at(segment.day) + timedelta(seconds=self.pace.first())
            posts = account.posts
            try:
                await self._submit(account, segment, (prepared or {}).pop(_key(segment), None), not_before)
            except Exception:  # 一段出了意外，不能连累后面的段；发出过的留着“已发出”，以后核对
                log.exception("成员 %s 提交 %s %s 时出错", member_id, segment.day, segment.span)
            if account.posts != posts:  # 真正交出去了，下一段从答复回来时起算；没交出去就沿用原来的时刻
                not_before = self.clock.now() + timedelta(seconds=self.pace.gap())

    async def _submit(
        self, account: _Account, segment: Claimed, ready: tuple[Form, float] | None, not_before: datetime
    ) -> None:
        """提交一段，直到有结果。前一段的答复回来之前不会调用，同一个成员同时只有一笔在学校那里处理。"""
        school, profile = account.school, account.profile
        form, fetched = ready if ready else (None, 0.0)
        if form is None:  # 没有预先取好的表单：到点前几秒再取，满 2 秒正好提交
            await self.clock.sleep_until(not_before - timedelta(seconds=FORM_AGE + 1))
            if self.store.member(account.member.id) is None:
                return None
        not_open_since: float | None = None
        refetched = resigned = False
        network_failures = too_fast = busy = 0
        while True:
            if form is None:
                try:
                    form = await school.form(profile, segment.room, segment.day, segment.span)
                except SessionLost as error:
                    if resigned or not await self._resign(account):
                        return self._fail(segment, str(error))
                    resigned = True
                    continue
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
            await self.clock.sleep_until(max(open_at(segment.day), not_before))
            if self._started(segment):
                return self._fail(segment, "时间已过")
            member = self.store.member(account.member.id)  # 现读，晚上刚在设置里改的电话和主题也算数
            if member is None or not self.store.mark_sent(segment.id, form.booking_id, self.clock.now()):
                return None  # 这一段已经不在了（成员被移除），不再提交
            account.posts += 1
            try:
                reply = await school.submit(profile, form, member.phone or DEFAULT_PHONE, member.theme or DEFAULT_THEME)
            except SessionLost as error:  # 请求被挡在登录这一步，没有到达学校，可以放心重来
                self.store.requeue(segment.id)
                if resigned or not await self._resign(account):
                    return self._fail(segment, str(error))
                resigned, form = True, None
                continue
            except Uncertain:
                sent = replace(segment, school_id=form.booking_id, sent_at=self.clock.now())
                await self.clock.sleep(5.0)  # 给学校一点时间把这条记下来
                return await self._reconcile(account, sent)
            except Unreachable as error:
                self.store.requeue(segment.id)
                network_failures += 1
                if network_failures >= NETWORK_RETRIES:
                    return self._fail(segment, str(error))
                await self.clock.sleep(2.0)
                continue
            if reply.verdict is Verdict.BOOKED:
                self.store.settle(segment.id, Outcome.BOOKED, reply.message, self.clock.now())
                log.info("成员 %s 订到 %s %s", account.member.id, segment.day, segment.span)
                return None
            if reply.verdict in (Verdict.TAKEN, Verdict.REJECTED):
                return self._fail(segment, reply.message)
            self.store.requeue(segment.id)
            if reply.verdict is Verdict.TOO_FAST:
                too_fast += 1
                if too_fast > TOO_FAST_RETRIES:
                    return self._fail(segment, reply.message)
                await self.clock.sleep(2.0)
            elif reply.verdict is Verdict.BUSY:  # 前一笔还在学校那里处理（例如答复超时的那一笔）
                busy += 1
                if busy > BUSY_RETRIES:
                    return self._fail(segment, reply.message)
                refetched = False  # 学校这时可能已经作废了这张表单，再给一次重新取表单的机会
                await self.clock.sleep(BUSY_WAIT)
            elif reply.verdict is Verdict.NOT_OPEN:
                not_open_since = self.clock.monotonic() if not_open_since is None else not_open_since
                if self.clock.monotonic() - not_open_since >= NOT_OPEN_WINDOW:
                    return self._fail(segment, reply.message)
                await self.clock.sleep(1.0)
            elif not refetched:  # 表单失效：重新取一张，只重试一次
                refetched, form = True, None
            else:
                return self._fail(segment, reply.message)

    async def _reconcile(self, account: _Account, segment: Claimed) -> None:
        """提交后没收到答复：去“我的预定”里看这一段有没有订上。

        查到就算成功。查不到时先保持“已发出”，过一阵再查；超过一定时间仍查不到才算失败。
        """
        try:
            records = await account.school.my_bookings(100)
        except PortalError as error:
            log.warning("成员 %s 核对“我的预定”失败：%s", account.member.id, error)
            return
        if segment.school_id:
            found = any(record.id == segment.school_id and record.status == "2" for record in records)
        else:
            found = any(record.matches(segment.room_name, segment.day, segment.span) for record in records)
        now = self.clock.now()
        if found:
            self.store.settle(segment.id, Outcome.BOOKED, "已在“我的预定”中确认", now)
        elif segment.sent_at is None or now - segment.sent_at >= SETTLE_AFTER or self._started(segment):
            self._fail(segment, "提交后没有收到答复，“我的预定”里也没有这一段")

    def _started(self, segment: Claimed) -> bool:
        return segment.span.starts_on(segment.day) <= self.clock.now()

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
        try:
            credentials = self.store.credentials(member_id)
        except KeyError:
            return None, False
        school = self.connect(credentials.cookies)
        try:
            await school.sign_in(member.student_id, credentials.password)
            profile = self.store.profile(member_id) or await school.read_profile()
        except (LoginRejected, NeedsUser) as error:
            self.store.mark_login(member_id, False, str(error), credentials.version)
            log.warning("成员 %s 登录失效：%s", member_id, error)
            await school.aclose()
            return None, False
        except PortalError as error:
            self._back_off(member_id)
            log.warning("成员 %s 暂时无法登录：%s", member_id, error)
            await school.aclose()
            return None, True
        except BaseException:
            await school.aclose()
            raise
        self._backoff.pop(member_id, None)
        self.store.save_cookies(member_id, school.cookies(), credentials.version)
        return _Account(member, school, profile, credentials.version), False

    async def _resign(self, account: _Account) -> bool:
        """在已有的会话上再确认一次登录；会话还有效时只是一次很快的请求。

        学校拒绝了密码之后，不再拿同一个密码去试，免得账号被锁。
        """
        member = self.store.member(account.member.id)
        if member is None or not member.login_ok:
            return False
        renewal = asyncio.ensure_future(self._resign_now(account))
        try:
            return await asyncio.shield(renewal)
        except asyncio.CancelledError:
            # 被取消的只是等待：让这次登录做完，结果（尤其是密码被拒）照常记下，再响应取消
            await asyncio.gather(renewal, return_exceptions=True)
            raise

    async def _resign_now(self, account: _Account) -> bool:
        try:
            credentials = self.store.credentials(account.member.id)
        except KeyError:
            return False
        try:
            await account.school.sign_in(account.member.student_id, credentials.password)
        except (LoginRejected, NeedsUser) as error:
            self.store.mark_login(account.member.id, False, str(error), credentials.version)
            log.warning("成员 %s 登录失效：%s", account.member.id, error)
            return False
        except PortalError as error:
            log.warning("成员 %s 重新登录失败：%s", account.member.id, error)
            return False
        return True

    async def _refresh(self, account: _Account) -> int | None:
        """重读日历页，更新规则编号和琴房清单。返回学校服务器与本机的时间差（毫秒）。"""
        try:
            profile, server_time = await account.school.read_calendar(account.profile)
        except PortalError as error:
            log.warning("成员 %s 重读日历页失败，沿用上次的内容：%s", account.member.id, error)
            return None
        account.profile = profile
        if self.store.member(account.member.id) is not None:
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
