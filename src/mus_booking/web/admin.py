"""管理页：成员、邀请码和日志。没有管理权限的浏览器访问这里一律得到 404。"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..domain import State, next_opening
from ..store import Invite, LogEntry, Member
from . import views
from .app import go, render, services
from .pages import Visitor, now_of, visitor
from .security import set_session

router = APIRouter(prefix="/admin")


def _admin(request: Request) -> Visitor:
    current = visitor(request)
    if current is None or not current.session.admin:
        raise HTTPException(404)
    return current


@router.get("/enter/{token}")
async def enter(request: Request, token: str) -> Response:
    store, now = services(request).store, now_of(request)
    if not store.redeem_admin_link(token, now):
        raise HTTPException(404)
    current = visitor(request)
    response = go(request, "/admin")
    if current is not None:
        store.update_session(current.token, admin=True)
    else:
        set_session(request, response, store.create_session(None, now, admin=True))
    return response


@dataclass(frozen=True)
class MemberRow:
    member: Member
    planned: int
    booked: int
    failed: int


@dataclass(frozen=True)
class LogDay:
    title: str
    took: str
    entries: list[LogEntry]


def _invite_note(invite: Invite) -> str:
    made = f"{views.short_date(invite.created_at.date())} 生成"
    return f"已绑定 {invite.member_name} · {made}" if invite.bound_at else f"未绑定 · {made}"


@router.get("")
async def page(request: Request) -> Response:
    _admin(request)
    svc, now = services(request), now_of(request)
    store = svc.store
    opening = next_opening(now)
    target = opening.date() + timedelta(days=1)
    latest = store.latest_results()
    rows = []
    for member in store.members():
        profile = store.profile(member.id)
        limit = profile.time_limit if profile else 120
        planned = sum(
            len(o.span.split(limit)) for o in store.schedule(member.id).on(target) if o.state is State.PLANNED
        )
        booked, failed = latest.get(member.id, (0, 0))
        rows.append(MemberRow(member, planned, booked, failed))

    took = {}
    for run in store.runs(30):
        if run.finished_at:
            took[run.opening.date()] = f"用时 {max(1, round((run.finished_at - run.opening).total_seconds()))} 秒"
    grouped: OrderedDict[date, list[LogEntry]] = OrderedDict()
    for entry in store.log(now.date() - timedelta(days=14)):
        grouped.setdefault(entry.day, []).append(entry)
    log = [
        LogDay(f"{views.short_date(day)} {views.weekday(day)}", took.get(day, ""), entries)
        for day, entries in grouped.items()
    ]
    waiting = [row for row in rows if row.planned]
    status = svc.engine.status
    return render(
        request,
        "admin.html",
        opens=views.moment_name(opening, now),
        summary=f"{len(waiting)} 位成员 · {sum(r.planned for r in waiting)} 个时段" if waiting else "暂无",
        healthy=status.running and not status.last_error,
        rows=rows,
        invites=[(invite, _invite_note(invite)) for invite in store.invites()],
        invite_url=f"{str(request.base_url).rstrip('/')}/?invite=",
        log=log,
        has_member=visitor(request).member is not None,
    )


@router.post("/invites")
async def create_invite(request: Request) -> Response:
    _admin(request)
    services(request).store.create_invite(now_of(request))
    return go(request, "/admin")


@router.post("/invites/{code}/void")
async def void_invite(request: Request, code: str) -> Response:
    _admin(request)
    services(request).store.void_invite(code, now_of(request))
    return go(request, "/admin")


@router.post("/members/{member_id}/remove")
async def remove_member(request: Request, member_id: int) -> Response:
    _admin(request)
    services(request).store.remove_member(member_id, now_of(request))
    return go(request, "/admin")
