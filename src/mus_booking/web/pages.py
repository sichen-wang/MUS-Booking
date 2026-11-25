"""成员使用的页面：绑定、登录、首页、添加与修改、详情和账号。"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ..domain import HORIZON, State, hhmm, open_at, ranked
from ..portal import Profile
from ..store import Member, Session, password_matches
from . import actions, views
from .actions import Refused
from .app import go, render, services
from .security import COOKIE, base, clear_session, set_session

router = APIRouter()


@dataclass
class Visitor:
    token: str
    session: Session
    member: Member | None
    profile: Profile | None


def now_of(request: Request) -> datetime:
    return services(request).clock.now()


def visitor(request: Request) -> Visitor | None:
    store = services(request).store
    token = request.cookies.get(COOKIE, "")
    session = store.session(token, now_of(request))
    if session is None:
        return None
    member = store.member(session.member_id) if session.member_id else None
    return Visitor(token, session, member, store.profile(member.id) if member else None)


def member_of(request: Request) -> Visitor:
    found = visitor(request)
    if found is None or found.member is None:
        raise SignedOut()
    return found


class SignedOut(Exception):
    """没有登录的访问，送回首页。"""


async def _form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: str(value) for key, value in form.items()}


def _client(request: Request) -> str:
    return request.client.host if request.client else "?"


def _start_session(
    request: Request, member_id: int | None, response: Response, current: Visitor | None, *, admin: bool = False
) -> Response:
    """登录或绑定之后换发一个新的会话，旧的作废，以免事先拿到旧令牌的人跟着登录。

    管理权限跟着会话走：已经以管理账号登录的浏览器再登录或绑定成员，仍是管理员，
    而管理账号还没有对应成员时，就对应到这一位，以后用管理账号登录直接进入他的首页。
    """
    store, now = services(request).store, now_of(request)
    admin = admin or (current is not None and current.session.admin)
    if current is not None:
        store.drop_session(current.token)
    if admin and member_id is not None:
        store.claim_admin(member_id)
    set_session(request, response, store.create_session(member_id, now, admin=admin))
    return response


# ---- 首页 ---------------------------------------------------------------------


@router.get("/")
async def home(request: Request, invite: str = "") -> Response:
    current = visitor(request)
    if current is None or current.member is None:
        hint = _preview_hint(request)
        code = invite.strip().upper()[:8] or (hint["invite"] if hint else "")
        return _welcome(request, current, code=code, preview=hint)
    return _home(request, current)


def _welcome(request: Request, current: Visitor | None, *, code: str, values=None, error="", preview=None) -> Response:
    admin = current is not None and current.session.admin
    return render(request, "welcome.html", code=code, values=values or {}, error=error, preview=preview, admin=admin)


def _preview_hint(request: Request) -> dict[str, str] | None:
    """预览模式下在绑定页上给出入口：示例成员、管理页，以及一个还能用的邀请码。"""
    svc = services(request)
    if not svc.preview:
        return None
    store, now = svc.store, now_of(request)
    usable = [invite.code for invite in store.invites() if invite.usable]
    return {**svc.preview, "invite": usable[0] if usable else store.create_invite(now)}


def _home(request: Request, current: Visitor, sheet: dict | None = None) -> Response:
    store, now = services(request).store, now_of(request)
    member = current.member
    assert member is not None
    days = views.week(store.schedule(member.id), current.profile, now, member.login_ok)
    return render(
        request,
        "home.html",
        member=member,
        initial=views.initial(member.name, member.student_id),
        days=days,
        next=views.next_run(days, now) if member.login_ok else None,  # 登录失效时不会自动预定
        busy=views.busy(days),
        sheet=sheet,
    )


# ---- 绑定与登录 ---------------------------------------------------------------


@router.post("/bind")
async def bind(request: Request) -> Response:
    values = await _form(request)
    svc, now = services(request), now_of(request)
    try:
        student_id = actions.clean_student_id(values.get("student_id", ""))
        if not request.app.state.login_limit.allow(f"ip:{_client(request)}", f"sid:{student_id}"):
            raise Refused("尝试次数过多，请稍后再试")
        member = await actions.bind(
            svc.store,
            svc.connect,
            now,
            values.get("code", ""),
            student_id,
            values.get("password", ""),
            values.get("phone", ""),
        )
    except Refused as refused:
        values.pop("password", None)
        return _welcome(request, visitor(request), code=values.get("code", ""), values=values, error=str(refused))
    svc.engine.poke()
    return _start_session(request, member, go(request, "/"), visitor(request))


@router.get("/login")
async def login_page(request: Request) -> Response:
    return render(request, "login.html", values={}, error="")


@router.post("/login")
async def login(request: Request) -> Response:
    values = await _form(request)
    name = values.get("student_id", "").strip()
    if name and not re.sub(r"\s+", "", name).isdigit():  # 学号都是数字，其余的当作管理账号
        return await _admin_login(request, name, values.get("password", ""))
    svc, now = services(request), now_of(request)
    try:
        student_id = actions.clean_student_id(values.get("student_id", ""))
        if not request.app.state.login_limit.allow(f"ip:{_client(request)}", f"sid:{student_id}"):
            raise Refused("尝试次数过多，请稍后再试")
        member = await actions.login(svc.store, svc.connect, now, student_id, values.get("password", ""))
    except Refused as refused:
        return render(request, "login.html", values={"student_id": values.get("student_id", "")}, error=str(refused))
    svc.engine.poke()
    return _start_session(request, member, go(request, "/"), visitor(request))


async def _admin_login(request: Request, name: str, password: str) -> Response:
    """管理账号登录。只进入管理账号自己对应的成员；还没有对应时只进管理页，不顺带对应当前浏览器里的成员。"""
    store = services(request).store
    found = store.admin_secret(name)
    keys = (f"ip:{_client(request)}", "admin") if found else (f"ip:{_client(request)}",)
    if not request.app.state.login_limit.allow(*keys):
        error = "尝试次数过多，请稍后再试"
    elif found is None or not await asyncio.to_thread(password_matches, password, found[1]):
        error = "账号或密码不对"
    else:
        member_id = found[0].member_id
        response = go(request, "/" if member_id else "/admin")
        return _start_session(request, member_id, response, visitor(request), admin=True)
    return render(request, "login.html", values={"student_id": name}, error=error)


@router.post("/logout")
async def logout(request: Request) -> Response:
    current = visitor(request)
    if current:
        services(request).store.drop_session(current.token)
    response = go(request, "/")
    clear_session(request, response)
    return response


# ---- 添加与修改 ---------------------------------------------------------------


def _editor(
    request: Request,
    current: Visitor,
    *,
    day: date,
    start: int,
    end: int,
    room: str,
    weekly: bool,
    editing: tuple[int, date] | None = None,
    error: str = "",
) -> Response:
    store, now = services(request).store, now_of(request)
    member, profile = current.member, current.profile
    assert member is not None
    schedule = store.schedule(member.id)
    rooms = ranked(profile.rooms if profile else (), [b.room for b in schedule.bookings])
    used = {b.room for b in schedule.bookings}
    quick = [r for r in rooms if r.id in used][:4]
    quick = quick if len(quick) > 1 else []
    day_start = profile.day_start if profile else 0
    day_end = profile.day_end if profile else 23 * 60 + 30
    action = f"/b/{editing[0]}/{editing[1]}/edit" if editing else "/add"
    back = f"/b/{editing[0]}/{editing[1]}" if editing else "/"
    return render(
        request,
        "edit.html",
        editing=editing is not None,
        action=action,
        back=back,
        dates=views.date_choices(now.date()),
        day=day,
        starts=[hhmm(m) for m in range(day_start, day_end, 30)],
        ends=[hhmm(m) for m in range(day_start + 30, day_end + 1, 30)],
        start=hhmm(start),
        end=hhmm(end),
        rooms=rooms,
        room=room,
        quick=quick,
        weekly=weekly,
        error=error,
    )


@router.get("/add")
async def add_page(request: Request, day: str = "") -> Response:
    current = member_of(request)
    now = now_of(request)
    schedule = services(request).store.schedule(current.member.id)
    latest = max(schedule.bookings, key=lambda b: b.id, default=None)
    start, end = (latest.span.start, latest.span.end) if latest else (19 * 60, 21 * 60)
    try:
        chosen = actions.parse_day(day, now.date()) if day else now.date()
    except Refused:
        chosen = now.date()
    if not day and chosen == now.date() and start <= now.hour * 60 + now.minute:
        chosen += timedelta(days=1)
    profile = current.profile
    room = latest.room if latest else (ranked(profile.rooms, [])[0].id if profile and profile.rooms else "")
    return _editor(request, current, day=chosen, start=start, end=end, room=room, weekly=False)


@router.post("/add")
async def add(request: Request) -> Response:
    current = member_of(request)
    values = await _form(request)
    svc, now = services(request), now_of(request)
    try:
        actions.add_booking(
            svc.store,
            current.member.id,
            _profile(current),
            now,
            values.get("day", ""),
            values.get("start", ""),
            values.get("end", ""),
            values.get("room", ""),
            values.get("weekly") == "on",
        )
    except Refused as refused:
        return _editor_again(request, current, values, error=str(refused))
    svc.engine.poke()
    return go(request, "/")


def _editor_again(request: Request, current: Visitor, values: dict[str, str], *, error: str, editing=None) -> Response:
    now = now_of(request)
    try:
        day = date.fromisoformat(values.get("day", ""))
    except ValueError:
        day = now.date()

    def minutes(text: str, fallback: int) -> int:
        try:
            hours, _, mins = text.partition(":")
            return int(hours) * 60 + int(mins)
        except ValueError:
            return fallback

    return _editor(
        request,
        current,
        day=day,
        start=minutes(values.get("start", ""), 19 * 60),
        end=minutes(values.get("end", ""), 21 * 60),
        room=values.get("room", ""),
        weekly=values.get("weekly") == "on",
        editing=editing,
        error=error,
    )


def _profile(current: Visitor) -> Profile:
    if current.profile is None:
        raise Refused("还没有读到可预定的琴房，请稍后再试")
    return current.profile


# ---- 详情 ---------------------------------------------------------------------


def _occurrence(request: Request, current: Visitor, booking_id: int, day: str):
    try:
        when = date.fromisoformat(day)
    except ValueError as error:
        raise HTTPException(404) from error
    occurrence = services(request).store.schedule(current.member.id).find(booking_id, when)
    if occurrence is None:
        raise HTTPException(404)
    return occurrence


@router.get("/b/{booking_id}/{day}")
async def detail(request: Request, booking_id: int, day: str, error: str = "") -> Response:
    current = member_of(request)
    return _detail(request, current, _occurrence(request, current, booking_id, day), error)


def _detail(request: Request, current: Visitor, occurrence, error: str = "") -> Response:
    now = now_of(request)
    member = current.member
    item = views.item(occurrence, current.profile, now, member.login_ok)
    booking = occurrence.booking
    sheet = {
        "item": item,
        "date": views.long_date(occurrence.day, now.date()),
        "weekly": f"每{views.weekday(booking.first)}" if booking.weekly else "",
        "opens": views.moment_name(open_at(occurrence.day), now),
        "open_now": now >= open_at(occurrence.day),
        "segments": views.segment_rows(occurrence.segments),
        "path": f"/b/{booking.id}/{occurrence.day}",
        # 单次预定提交之后、以及修改时被截短的旧预定，都只显示结果
        "can_edit": booking.last is None and not (occurrence.state is State.SUBMITTED and not booking.weekly),
        "can_skip": now.date() <= occurrence.day < now.date() + timedelta(days=HORIZON),
        "error": error,
    }
    return _home(request, current, sheet)


@router.get("/b/{booking_id}/{day}/edit")
async def edit_page(request: Request, booking_id: int, day: str) -> Response:
    current = member_of(request)
    occurrence = _occurrence(request, current, booking_id, day)
    booking = occurrence.booking
    shown = max(occurrence.day, now_of(request).date())
    return _editor(
        request,
        current,
        day=shown,
        start=booking.span.start,
        end=booking.span.end,
        room=booking.room,
        weekly=booking.weekly,
        editing=(booking.id, occurrence.day),
    )


@router.post("/b/{booking_id}/{day}/edit")
async def edit(request: Request, booking_id: int, day: str) -> Response:
    current = member_of(request)
    occurrence = _occurrence(request, current, booking_id, day)
    values = await _form(request)
    svc, now = services(request), now_of(request)
    try:
        actions.edit_booking(
            svc.store,
            current.member.id,
            _profile(current),
            now,
            booking_id,
            values.get("day", ""),
            values.get("start", ""),
            values.get("end", ""),
            values.get("room", ""),
            values.get("weekly") == "on",
        )
    except Refused as refused:
        return _editor_again(request, current, values, error=str(refused), editing=(booking_id, occurrence.day))
    svc.engine.poke()
    return go(request, "/")


@router.post("/b/{booking_id}/{day}/{verb}")
async def act(request: Request, booking_id: int, day: str, verb: str) -> Response:
    current = member_of(request)
    occurrence = _occurrence(request, current, booking_id, day)
    svc, now, member_id = services(request), now_of(request), current.member.id
    try:
        if verb == "skip":
            actions.skip(svc.store, member_id, now, booking_id, occurrence.day)
        elif verb == "unskip":
            actions.unskip(svc.store, member_id, now, booking_id, occurrence.day)
        elif verb == "pause":
            actions.set_paused(svc.store, member_id, now, booking_id, True)
        elif verb == "resume":
            actions.set_paused(svc.store, member_id, now, booking_id, False)
        elif verb == "delete":
            actions.delete_booking(svc.store, member_id, now, booking_id)
            svc.engine.poke()
            return go(request, "/")
        else:
            raise HTTPException(404)
    except Refused as refused:
        fresh = svc.store.schedule(member_id).find(booking_id, occurrence.day) or occurrence
        return _detail(request, current, fresh, str(refused))
    svc.engine.poke()
    return go(request, f"/b/{booking_id}/{occurrence.day}")


# ---- 账号 ---------------------------------------------------------------------


@router.get("/account")
async def account(request: Request) -> Response:
    current = member_of(request)
    return _account(request, current)


def _account(request: Request, current: Visitor, *, error: str = "", saved: str = "") -> Response:
    return render(request, "account.html", member=current.member, admin=current.session.admin, error=error, saved=saved)


@router.post("/account/phone")
async def save_phone(request: Request) -> Response:
    current = member_of(request)
    values = await _form(request)
    try:
        phone = actions.clean_phone(values.get("phone", ""))
    except Refused as refused:
        return _account(request, current, error=str(refused))
    services(request).store.set_phone(current.member.id, phone)
    return go(request, "/account")


@router.post("/account/password")
async def save_password(request: Request) -> Response:
    current = member_of(request)
    values = await _form(request)
    svc = services(request)
    try:
        if not request.app.state.account_limit.allow(f"member:{current.member.id}"):
            raise Refused("尝试次数过多，请稍后再试")
        await actions.refresh_credentials(
            svc.store, svc.connect, now_of(request), current.member.id, values.get("password", "")
        )
    except Refused as refused:
        return _account(request, current, error=str(refused))
    svc.engine.poke()
    fresh = visitor(request)
    return _account(request, fresh or current, saved="已更新")


# ---- 添加到主屏幕 -------------------------------------------------------------


@router.get("/manifest.webmanifest")
async def manifest(request: Request) -> Response:
    root = base(request)
    icon = f"{root}/static/icon.png"
    body = {
        "name": "MUS Booking",
        "short_name": "MUS Booking",
        "start_url": f"{root}/",
        "scope": f"{root}/",
        "display": "standalone",
        "background_color": "#F6F5F1",
        "theme_color": "#F6F5F1",
        "icons": [{"src": icon, "sizes": "512x512", "type": "image/png", "purpose": "any"}],
    }
    return JSONResponse(body, media_type="application/manifest+json")


# ---- 预览 ---------------------------------------------------------------------


@router.get("/preview/as/{member_id}")
async def preview_as(request: Request, member_id: int) -> Response:
    svc = services(request)
    if not svc.preview or svc.store.member(member_id) is None:
        raise HTTPException(404)
    admin = request.query_params.get("admin") == "1"
    token = svc.store.create_session(member_id, now_of(request), admin=admin)
    response = go(request, "/admin" if admin else "/")
    set_session(request, response, token)
    return response
