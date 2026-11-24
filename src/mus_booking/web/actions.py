"""网页上的每个操作：先校验，再保存，出问题时抛出一句可以直接显示的话。"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import date, datetime, timedelta

from ..domain import HORIZON, Booking, Frozen, Span, State, parse_hhmm, rehomed_skips, retire, revise
from ..portal import LoginRejected, NeedsUser, PortalError, Profile, School, Unreachable
from ..store import Store

log = logging.getLogger("mus_booking.web")

Connect = Callable[[Iterable[dict[str, str]]], School]


class Refused(Exception):
    """操作没有完成。消息直接显示在页面上。"""


# ---- 账号 ---------------------------------------------------------------------


def clean_student_id(text: str) -> str:
    value = re.sub(r"\s+", "", text or "")
    if not value or len(value) > 40:
        raise Refused("请填写学号")
    return value


def clean_phone(text: str) -> str:
    value = re.sub(r"[\s-]+", "", text or "")
    if value and not re.fullmatch(r"\+?\d{5,20}", value):
        raise Refused("电话只能填数字")
    return value


async def verify(connect: Connect, student_id: str, password: str) -> School:
    """用一个空的会话核对学号和密码。成功时返回登录好的会话，调用方负责关闭。"""
    if not password:
        raise Refused("请填写密码")
    school = connect([])
    try:
        await school.sign_in(student_id, password, verify_password=True)
    except LoginRejected as error:
        await school.aclose()
        raise Refused("学号或密码不对") from error
    except NeedsUser as error:
        await school.aclose()
        raise Refused("学校要求额外的验证，暂时无法使用") from error
    except Unreachable as error:
        await school.aclose()
        raise Refused("学校系统暂时连不上，请稍后再试") from error
    except PortalError as error:
        await school.aclose()
        log.warning("核对学号 %s 时出错：%s", student_id, error)
        raise Refused("学校系统暂时连不上，请稍后再试") from error
    return school


async def bind(
    store: Store, connect: Connect, now: datetime, code: str, student_id: str, password: str, phone: str
) -> int:
    """用邀请码绑定学校账号，返回新成员的编号。"""
    student_id, phone = clean_student_id(student_id), clean_phone(phone)
    invite = store.invite(code)
    if invite is None or not invite.usable:
        raise Refused("邀请码无效")
    if store.member_by_student(student_id):
        raise Refused("这个学号已经绑定过，请直接登录")
    school = await verify(connect, student_id, password)
    try:
        profile = await school.read_profile()
    except PortalError as error:
        raise Refused(str(error) if "琴房" in str(error) else "没能读取可预定的琴房，请稍后再试") from error
    finally:
        cookies = school.cookies()
        await school.aclose()
    with store.transaction():
        if store.member_by_student(student_id):
            raise Refused("这个学号已经绑定过，请直接登录")
        member = store.add_member(student_id, profile.name, phone, password, cookies, now)
        if not store.bind_invite(code, member, now):
            raise Refused("邀请码无效")
        store.save_profile(member, profile, now)
    log.info("成员 %s 绑定成功", member)
    return member


async def login(store: Store, connect: Connect, now: datetime, student_id: str, password: str) -> int:
    """换设备登录：学校核对密码之后，更新保存的密码和凭据。"""
    student_id = clean_student_id(student_id)
    member = store.member_by_student(student_id)
    if member is None:
        raise Refused("这个学号还没有绑定")
    await refresh_credentials(store, connect, now, member.id, password)
    return member.id


async def refresh_credentials(store: Store, connect: Connect, now: datetime, member_id: int, password: str) -> None:
    member = store.member(member_id)
    if member is None:
        raise Refused("这个账号已经不存在")
    school = await verify(connect, member.student_id, password)
    try:
        cookies = school.cookies()
    finally:
        await school.aclose()
    store.replace_credentials(member_id, password, cookies)


# ---- 预定 ---------------------------------------------------------------------


def _span(profile: Profile, start: str, end: str) -> Span:
    try:
        span = Span.parse(start, end)
    except ValueError as error:
        if start and end and parse_hhmm(end) <= parse_hhmm(start):
            raise Refused("结束要晚于开始") from error
        raise Refused("请选择时间") from error
    if span.start < profile.day_start or span.end > profile.day_end:
        raise Refused("不在可预定的时间范围内")
    return span


def parse_day(text: str, today: date) -> date:
    try:
        day = date.fromisoformat(text)
    except ValueError as error:
        raise Refused("请选择日期") from error
    if not today <= day < today + timedelta(days=HORIZON):
        raise Refused(f"只能选未来 {HORIZON} 天")
    return day


def _room(profile: Profile, room: str) -> str:
    if profile.room(room) is None:
        raise Refused("请选择琴房")
    return room


def _check(store: Store, member_id: int, candidate: Booking, now: datetime, *, replacing: Iterable[Booking] = ()):
    if candidate.first == now.date() and candidate.span.starts_on(candidate.first) <= now:
        raise Refused("开始时间已过")
    trial = store.schedule(member_id).with_bookings(*replacing, replace(candidate, id=candidate.id or -1))
    problem = trial.problem(candidate.id or -1, now)
    if problem:
        raise Refused(problem)


def add_booking(
    store: Store,
    member_id: int,
    profile: Profile,
    now: datetime,
    day: str,
    start: str,
    end: str,
    room: str,
    weekly: bool,
) -> int:
    first = parse_day(day, now.date())
    candidate = Booking(member_id, _room(profile, room), _span(profile, start, end), first, weekly=weekly)
    _check(store, member_id, candidate, now)
    return store.put_booking(candidate, now)


def edit_booking(
    store: Store,
    member_id: int,
    profile: Profile,
    now: datetime,
    booking_id: int,
    day: str,
    start: str,
    end: str,
    room: str,
    weekly: bool,
) -> None:
    schedule = store.schedule(member_id)
    old = schedule.booking(booking_id)
    if old is None:
        raise Refused("这条预定已经不存在")
    wanted = Booking(
        member_id, _room(profile, room), _span(profile, start, end), parse_day(day, now.date()), weekly=weekly
    )
    try:
        kept, new = revise(schedule, old, wanted)
    except Frozen as error:
        raise Refused("这一次已经提交，不能再改") from error
    _check(store, member_id, new, now, replacing=[kept] if kept else [])
    stale, moved = rehomed_skips(schedule, old, kept, new)
    with store.transaction():
        if kept:
            store.put_booking(kept, now)
        new_id = store.put_booking(new, now)
        store.unskip(old.id, stale)
        for skipped in moved:
            store.skip(new_id, skipped)


def delete_booking(store: Store, member_id: int, now: datetime, booking_id: int) -> None:
    schedule = store.schedule(member_id)
    old = schedule.booking(booking_id)
    if old is None:
        return
    try:
        kept = retire(schedule, old)
    except Frozen as error:
        raise Refused("这一次已经提交，不能删除") from error
    if kept is None:
        store.delete_booking(old.id)
    else:
        store.put_booking(kept, now)


def skip(store: Store, member_id: int, booking_id: int, day: date) -> None:
    occurrence = store.schedule(member_id).find(booking_id, day)
    if occurrence is None or occurrence.state is not State.PLANNED or not occurrence.booking.weekly:
        raise Refused("这一次不能跳过")
    store.skip(booking_id, day)


def unskip(store: Store, member_id: int, now: datetime, booking_id: int, day: date) -> None:
    schedule = store.schedule(member_id)
    occurrence = schedule.find(booking_id, day)
    if occurrence is None or occurrence.state is not State.SKIPPED:
        return
    if occurrence.span.ends_on(day) <= now:
        raise Refused("这一次已经过去了")
    trial = schedule.with_skips(schedule.skips - {(booking_id, day)})
    if problem := trial.problem(booking_id, now):
        raise Refused(problem)
    store.unskip(booking_id, [day])


def set_paused(store: Store, member_id: int, now: datetime, booking_id: int, paused: bool) -> None:
    schedule = store.schedule(member_id)
    booking = schedule.booking(booking_id)
    if booking is None or not booking.weekly or booking.paused == paused:
        return
    updated = replace(booking, paused=paused)
    if not paused and (problem := schedule.with_bookings(updated).problem(booking_id, now)):
        raise Refused(problem)
    store.put_booking(updated, now)
