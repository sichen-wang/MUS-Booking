"""预览：用假学校系统和一份示例数据启动整个服务，不需要真实账号就能看到所有页面。"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet

from .domain import CHINA, Booking, Outcome, Span
from .engine import Engine, SystemClock
from .portal.fake import FakeSchool
from .store import Store, Vault
from .web.app import Services

PEOPLE = (
    ("123090421", "林知夏"),
    ("123090207", "周予安"),
    ("123090358", "陈一诺"),
    ("123090136", "沈听澜"),
)
NEWCOMER = ("123090519", "宋清和", "preview")


def build() -> tuple[Services, dict[str, str]]:
    clock = SystemClock()
    fake = FakeSchool(now=clock.now)
    for student_id, name in PEOPLE:
        fake.add_account(student_id, "preview", name)
    student_id, name, password = NEWCOMER
    fake.add_account(student_id, password, name)
    folder = Path(tempfile.mkdtemp(prefix="mus-booking-preview-"))
    store = Store.open(folder / "preview.db", Vault(Fernet.generate_key()))
    links = asyncio.run(_seed(store, fake, clock.now()))
    engine = Engine(store, clock, fake.school)
    return Services(store, engine, clock, fake.school, preview=True), links


async def _seed(store: Store, fake: FakeSchool, now: datetime) -> dict[str, str]:
    today = now.date()
    yesterday_night = datetime.combine(today - timedelta(days=1), datetime.min.time(), CHINA) + timedelta(hours=21)
    ids = {}
    for student_id, name in PEOPLE:
        async with fake.school() as school:
            await school.sign_in(student_id, "preview")
            profile = await school.read_profile()
            member = store.add_member(student_id, name, "", "preview", school.cookies(), now - timedelta(days=9))
        store.save_profile(member, profile, now)
        ids[student_id] = member
    lin, zhou, chen, shen = (ids[s] for s, _ in PEOPLE)

    def book(member: int, when: str, day_offset: int, room: str, *, weekly: bool = False) -> int:
        start, end = when.split("-")
        first = today + timedelta(days=day_offset)
        return store.put_booking(Booking(member, room, Span.parse(start, end), first, weekly=weekly), now)

    def submitted(member: int, booking: int, day_offset: int, outcomes: list[Outcome], at: datetime) -> None:
        schedule = store.schedule(member)
        day = today + timedelta(days=day_offset)
        occurrence = schedule.find(booking, day)
        assert occurrence is not None
        spans = occurrence.span.split(120)
        room = occurrence.room
        for segment_id, outcome in zip(store.claim(member, booking, day, spans, room, room, at), outcomes, strict=True):
            store.mark_sent(segment_id, "", at)
            message = "保存成功" if outcome is Outcome.BOOKED else "手速太慢，该时间段已经被预订啦"
            store.settle(segment_id, outcome, message, at + timedelta(seconds=2))

    # 林知夏：首页上各种状态都有一条
    morning = book(lin, "09:00-11:00", -7, "room327", weekly=True)
    afternoon = book(lin, "13:00-19:00", -7, "room327", weekly=True)
    for offset in (-7, 0):
        night = yesterday_night + timedelta(days=offset)
        submitted(lin, morning, offset, [Outcome.BOOKED], night)
        submitted(
            lin,
            afternoon,
            offset,
            [Outcome.BOOKED, Outcome.BOOKED, Outcome.FAILED if offset == 0 else Outcome.BOOKED],
            night,
        )
    book(lin, "19:00-21:00", 2, "room327", weekly=True)
    skipped = book(lin, "09:00-12:00", 4, "room518", weekly=True)
    store.skip(skipped, today + timedelta(days=4))

    # 其他人：管理页的成员和日志
    for member, room in ((zhou, "room322"), (chen, "room412"), (shen, "room425")):
        booking = book(member, "10:00-12:00", -3, room, weekly=True)
        submitted(member, booking, -3, [Outcome.BOOKED], yesterday_night + timedelta(days=-3))
    extra = book(chen, "14:00-18:00", -1, "room412")
    submitted(chen, extra, -1, [Outcome.BOOKED, Outcome.FAILED], yesterday_night + timedelta(days=-1))
    book(zhou, "15:00-17:00", 1, "room322")
    store.mark_login(chen, False, "学号或密码不正确", store.credentials(chen).version)
    for offset, seconds in ((-7, 5), (-3, 6), (-1, 4), (0, 6)):
        opening = yesterday_night + timedelta(days=offset)
        store.start_run(opening, opening - timedelta(minutes=5))
        store.finish_run(opening, opening + timedelta(seconds=seconds), -200)

    for code_owner in (shen, chen):
        code = store.create_invite(now - timedelta(days=5))
        store.bind_invite(code, code_owner, now - timedelta(days=5))
    open_code = store.create_invite(now)
    fake.occupy("room327", today + timedelta(days=1), Span.parse("08:00", "09:00"))

    return {
        "member": f"/preview/as/{lin}",
        "admin": f"/preview/as/{lin}?admin=1",
        "invite": f"/?invite={open_code}",
        "newcomer": f"{NEWCOMER[0]} / {NEWCOMER[2]}",
    }
