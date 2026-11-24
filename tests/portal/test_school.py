"""对着假学校系统走完整的登录、读取和提交流程。"""

from datetime import date, datetime, timedelta

import pytest

from mus_booking.domain import CHINA, Span
from mus_booking.portal import LoginRejected, NeedsUser, Uncertain, Unexpected, Unreachable, Verdict
from mus_booking.portal.fake import FakeSchool

pytestmark = pytest.mark.anyio

DAY = date(2026, 11, 26)
EVENING = datetime(2026, 11, 25, 20, 59, tzinfo=CHINA)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock(EVENING)


@pytest.fixture
def fake(clock):
    school = FakeSchool(now=clock)
    school.add_account("123000001", "right", "林知夏")
    return school


async def test_sign_in_with_password_then_reuse_the_cookies(fake):
    async with fake.school() as school:
        me = await school.sign_in("123000001", "right", verify_password=True)
        cookies = school.cookies()
    assert me.name == "林知夏"
    assert fake.password_checks == 1

    async with fake.school(cookies) as again:
        await again.sign_in("123000001", None)
    assert fake.password_checks == 1


async def test_verifying_a_password_refuses_to_lean_on_saved_cookies(fake):
    async with fake.school() as school:
        await school.sign_in("123000001", "right")
        cookies = school.cookies()
    async with fake.school(cookies) as school:
        with pytest.raises(Unexpected, match="没有要求输入密码"):
            await school.sign_in("123000001", "wrong", verify_password=True)


async def test_a_changed_password_is_rejected_even_with_saved_cookies(fake):
    async with fake.school() as school:
        await school.sign_in("123000001", "right")
        cookies = school.cookies()
    fake.expire_sessions()
    fake.reject_passwords = True
    async with fake.school(cookies) as school:
        with pytest.raises(LoginRejected):
            await school.sign_in("123000001", "right")


async def test_a_wrong_password_is_rejected(fake):
    async with fake.school() as school:
        with pytest.raises(LoginRejected, match="不正确"):
            await school.sign_in("123000001", "wrong")


async def test_expired_cookies_without_a_password_need_the_user(fake):
    async with fake.school() as school:
        await school.sign_in("123000001", "right")
        cookies = school.cookies()
    fake.expire_sessions()
    async with fake.school(cookies) as school:
        with pytest.raises(NeedsUser):
            await school.sign_in("123000001", None)


async def test_an_outage_is_unreachable(fake):
    fake.down = True
    async with fake.school() as school:
        with pytest.raises(Unreachable):
            await school.sign_in("123000001", "right")


async def test_profile_lists_the_practice_rooms(fake):
    async with fake.school() as school:
        await school.sign_in("123000001", "right")
        profile = await school.read_profile()
    assert profile.name == "林知夏"
    assert profile.type_id == "af15efadc379429885681cbad7b1ec12"
    assert (profile.person_tag, profile.book_type) == ("Student", "0")
    assert len(profile.rule_id) == 32
    assert profile.time_limit == 120
    assert [room.code for room in profile.rooms][:3] == ["MPC321", "MPC322", "MPC319"]


async def test_the_submission_rules(fake, clock):
    span = Span.parse("19:00", "21:00")
    async with fake.school() as school:
        await school.sign_in("123000001", "right")
        profile = await school.read_profile()
        room = profile.rooms[0].id

        form = await school.form(profile, room, DAY, span)
        assert (await school.submit(profile, form, "")).verdict is Verdict.TOO_FAST
        clock.advance(2)
        assert (await school.submit(profile, form, "")).verdict is Verdict.NOT_OPEN
        clock.now = datetime(2026, 11, 25, 21, 0, tzinfo=CHINA)
        reply = await school.submit(profile, form, "")
        assert (reply.verdict, reply.message) == (Verdict.BOOKED, "保存成功")
        assert (await school.submit(profile, form, "")).verdict is Verdict.STALE

        fake.occupy(profile.rooms[1].id, DAY, Span.parse("20:00", "22:00"))
        other = await school.form(profile, profile.rooms[1].id, DAY, span)
        clock.advance(2)
        assert (await school.submit(profile, other, "")).verdict is Verdict.TAKEN

        records = await school.my_bookings()
    assert len(records) == 1
    assert records[0].id == form.booking_id
    assert records[0].matches(profile.rooms[0].name, DAY, span)


async def test_a_lost_reply_is_uncertain_but_the_booking_exists(fake, clock):
    clock.now = datetime(2026, 11, 25, 21, 0, tzinfo=CHINA)
    async with fake.school() as school:
        await school.sign_in("123000001", "right")
        profile = await school.read_profile()
        form = await school.form(profile, profile.rooms[0].id, DAY, Span.parse("09:00", "10:00"))
        clock.advance(3)
        fake.lose_replies = 1
        with pytest.raises(Uncertain):
            await school.submit(profile, form, "13800000000")
        records = await school.my_bookings()
    assert [r.matches(profile.rooms[0].name, DAY, Span.parse("09:00", "10:00")) for r in records] == [True]
