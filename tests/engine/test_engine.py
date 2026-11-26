"""用虚拟时钟和假学校系统跑完整的一晚，以及各种意外。"""

import asyncio
import itertools
import random
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from mus_booking.domain import CHINA, Booking, Outcome, Span, State
from mus_booking.engine import Engine, Pace, VirtualClock
from mus_booking.portal import Answers
from mus_booking.portal.fake import Booked, FakeSchool
from mus_booking.store import Store, Vault

pytestmark = pytest.mark.anyio

OPENING = datetime(2026, 11, 25, 21, 0, tzinfo=CHINA)
TOMORROW = date(2026, 11, 26)
FIRST = timedelta(seconds=2.5)
GAP = timedelta(seconds=1.5)


class Midpoint(random.Random):
    """随机数总取正中：第一段在开放后 2.5 秒，之后每段在答复后隔 1.5 秒，同一档里的先后保持时间顺序。"""

    def random(self) -> float:
        return 0.5


@dataclass
class World:
    clock: VirtualClock
    fake: FakeSchool
    store: Store
    engine: Engine

    async def enroll(self, student_id="123000001", password="pw", name="林知夏") -> int:
        self.fake.add_account(student_id, password, name)
        async with self.fake.school() as school:
            await school.sign_in(student_id, password, verify_password=True)
            profile = await school.read_profile()
            member = self.store.add_member(student_id, name, "", password, school.cookies(), self.clock.now())
        self.store.save_profile(member, profile, self.clock.now())
        return member

    def book(self, member: int, when: str, day=TOMORROW, room="room327", **extra) -> int:
        start, end = when.split("-")
        return self.store.put_booking(Booking(member, room, Span.parse(start, end), day, **extra), self.clock.now())

    def outcomes(self, member: int, day=TOMORROW) -> list[tuple[str, Outcome, str]]:
        return [(str(s.span), s.outcome, s.message) for o in self.store.schedule(member).on(day) for s in o.segments]

    def starts(self) -> list[str]:
        """各次提交的开始时间，按提交的先后。"""
        return [start[11:16] for _, _, start in self.fake.submissions]


def make_world(start: datetime) -> World:
    clock = VirtualClock(start)
    fake = FakeSchool(now=clock.now)
    store = Store.open(":memory:", Vault(Fernet.generate_key()))
    return World(clock, fake, store, Engine(store, clock, fake.school, pace=Pace(Midpoint())))


@pytest.fixture
def world():
    world = make_world(datetime(2026, 11, 25, 20, 40, tzinfo=CHINA))
    yield world
    world.store.close()


async def test_a_night_books_every_segment_at_a_quick_human_pace(world):
    member = await world.enroll()
    world.book(member, "13:00-19:00")
    checks_before = world.fake.password_checks

    await world.clock.run(world.engine.night(OPENING))

    assert [(span, outcome) for span, outcome, _ in world.outcomes(member)] == [
        ("13:00–15:00", Outcome.BOOKED),
        ("15:00–17:00", Outcome.BOOKED),
        ("17:00–19:00", Outcome.BOOKED),
    ]
    assert world.starts() == ["17:00", "13:00", "15:00"]  # 跨在 18:00 上的一段算晚上，先订
    times = [at for at, _, _ in world.fake.submissions]
    assert times[0] == OPENING + FIRST
    assert all(later - earlier >= GAP for earlier, later in itertools.pairwise(times))
    assert world.fake.password_checks == checks_before
    (run,) = world.store.runs()
    assert run.finished_at is not None


async def test_members_do_not_wait_for_each_other(world):
    first = await world.enroll("123000001")
    second = await world.enroll("123000002", name="周予安")
    world.book(first, "09:00-11:00", room="room321")
    world.book(second, "09:00-11:00", room="room322")
    await world.clock.run(world.engine.night(OPENING))
    assert sorted((user, at) for at, user, _ in world.fake.submissions) == [
        ("123000001", OPENING + FIRST),
        ("123000002", OPENING + FIRST),
    ]


async def test_evenings_go_first_then_afternoons_then_mornings(world):
    member = await world.enroll()
    world.book(member, "08:00-10:00", room="room321")
    world.book(member, "13:00-15:00", room="room322")
    world.book(member, "19:00-21:00")
    await world.clock.run(world.engine.night(OPENING))
    assert world.starts() == ["19:00", "13:00", "08:00"]


async def test_within_a_part_of_the_day_the_slots_others_grab_early_go_first(world):
    member = await world.enroll()
    world.book(member, "13:00-17:00")
    for back in (1, 2, 3):
        world.store.record_taken(TOMORROW - timedelta(days=back), [("room327", 15 * 60, 16 * 60)], world.clock.now())
    await world.clock.run(world.engine.night(OPENING))
    assert world.starts() == ["15:00", "13:00"]


async def test_every_form_is_fetched_ahead_in_turn_and_sent_at_planned_moments(world):
    member = await world.enroll()
    world.book(member, "08:00-10:00", room="room321")
    world.book(member, "19:00-21:00")
    await world.clock.run(world.engine.night(OPENING))
    assert world.starts() == ["19:00", "08:00"]
    assert [at for at, _, _ in world.fake.submissions] == [OPENING + FIRST, OPENING + FIRST + GAP]
    fetches = world.fake.forms
    assert all(at < OPENING for at, _, _ in fetches)
    assert [start[11:16] for _, _, start in fetches] == ["19:00", "08:00"]  # 按提交的先后取，不是按添加的先后


async def test_a_late_segment_pushes_the_next_one_back_instead_of_crowding_it(world):
    member = await world.enroll()
    world.clock.set(datetime(2026, 11, 26, 9, 59, 50, tzinfo=CHINA))
    world.book(member, "10:00-11:00")
    world.book(member, "19:00-20:00", room="room322")
    world.fake.fail_forms = 1  # 第一段的表单第一次没取到，晚了 2 秒
    await world.clock.run(world.engine.sweep())
    first, second = (at for at, _, _ in world.fake.submissions)
    assert second - first >= GAP
    assert world.starts() == ["10:00", "19:00"]


async def test_a_rejected_password_is_tried_only_once_for_all_segments(world):
    member = await world.enroll()
    world.book(member, "13:00-19:00")
    checks = world.fake.password_checks

    async def scenario():
        night = asyncio.ensure_future(world.engine.night(OPENING))
        await world.clock.sleep_until(OPENING + timedelta(seconds=1))
        world.fake.expire_sessions()
        world.fake.reject_passwords = True  # 用户在学校改了密码
        await night

    await world.clock.run(scenario())
    assert world.fake.password_checks == checks + 1
    assert {outcome for _, outcome, _ in world.outcomes(member)} == {Outcome.FAILED}
    assert world.store.member(member).login_ok is False


async def test_the_next_segment_waits_until_the_previous_one_is_settled(world):
    member = await world.enroll()
    world.book(member, "13:00-17:00")
    world.fake.opens_late = timedelta(seconds=6)  # 第一段重试到 6.5 秒才成功
    await world.clock.run(world.engine.night(OPENING))
    assert [at - OPENING for at, _, _ in world.fake.submissions] == [
        timedelta(seconds=s) for s in (2.5, 3.5, 4.5, 5.5, 6.5, 8.0)
    ]
    assert [outcome for _, outcome, _ in world.outcomes(member)] == [Outcome.BOOKED, Outcome.BOOKED]


async def test_a_booking_still_being_processed_is_waited_out_and_retried(world):
    member = await world.enroll()
    world.book(member, "09:00-10:00")
    world.fake.busy_replies = 1  # 学校说上一笔还在处理中
    await world.clock.run(world.engine.night(OPENING))
    assert world.outcomes(member) == [("09:00–10:00", Outcome.BOOKED, "保存成功")]
    assert [at - OPENING for at, _, _ in world.fake.submissions] == [timedelta(seconds=s) for s in (2.5, 4.5)]


async def test_each_member_books_with_their_own_theme(world):
    plain = await world.enroll("123000001")
    custom = await world.enroll("123000002", name="周予安")
    world.store.save_answers(custom, Answers(theme="1"))
    world.book(plain, "09:00-10:00", room="room321")
    world.book(custom, "09:00-10:00", room="room322")
    await world.clock.run(world.engine.night(OPENING))
    users = [user for _, user, _ in world.fake.submissions]
    assert sorted(zip(users, world.fake.themes, strict=True)) == [("123000001", "练琴"), ("123000002", "1")]


async def test_a_theme_changed_just_before_21_is_used_that_night(world):
    member = await world.enroll()
    world.book(member, "09:00-10:00")

    async def scenario():
        night = asyncio.ensure_future(world.engine.night(OPENING))
        await world.clock.sleep_until(OPENING - timedelta(minutes=1))
        world.store.save_answers(member, Answers(theme="1"))
        await night

    await world.clock.run(scenario())
    assert world.fake.themes == ["1"]


async def test_a_segment_that_never_went_out_does_not_pull_the_next_one_earlier(world):
    member = await world.enroll()
    world.book(member, "19:00-21:00", room="room321")
    world.book(member, "08:00-10:00", room="room322")
    world.fake.broken_rooms = {"room321"}  # 排在最前的晚上那段取不到表单
    await world.clock.run(world.engine.night(OPENING))
    assert world.starts() == ["08:00"]
    assert [at for at, _, _ in world.fake.submissions] == [OPENING + FIRST]


async def test_an_unexpected_error_on_one_segment_does_not_stop_the_rest(world, monkeypatch):
    member = await world.enroll()
    world.book(member, "13:00-17:00")
    settle, calls = world.store.settle, []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return settle(*args, **kwargs)

    monkeypatch.setattr(world.store, "settle", flaky)
    await world.clock.run(world.engine.night(OPENING))
    assert [outcome for _, outcome, _ in world.outcomes(member)] == [Outcome.SENT, Outcome.BOOKED]


async def test_the_night_notes_which_slots_others_took_in_the_first_minutes(world):
    member = await world.enroll()
    world.book(member, "13:00-15:00")

    async def scenario():
        night = asyncio.ensure_future(world.engine.night(OPENING))
        await world.clock.sleep_until(OPENING + timedelta(seconds=30))
        world.fake.occupy("room322", TOMORROW, Span.parse("19:00", "21:00"))
        await night

    await world.clock.run(scenario())
    assert world.fake.occupancy_reads == [OPENING + timedelta(minutes=2)]
    later = TOMORROW + timedelta(days=1)
    assert world.store.hotness("room322", Span.parse("19:00", "21:00"), later) == 1.0
    assert world.store.hotness("room327", Span.parse("13:00", "15:00"), later) == 0.0  # 自己订到的不算
    dump = "\n".join(world.store._db.iterdump())
    assert "13800000000" not in dump and "某同学" not in dump  # 占用情况里的姓名电话一概不存


async def test_nothing_happens_for_members_without_plans(world):
    await world.enroll()
    await world.clock.run(world.engine.night(OPENING))
    assert world.fake.submissions == []
    assert world.store.runs() == []


async def test_a_rejected_password_marks_the_member_and_is_not_retried(world):
    member = await world.enroll()
    world.book(member, "09:00-11:00")
    world.fake.expire_sessions()
    world.fake.reject_passwords = True
    checks = world.fake.password_checks

    await world.clock.run(world.engine.night(OPENING))

    assert not world.store.member(member).login_ok
    assert world.fake.submissions == []
    assert world.fake.password_checks == checks + 1
    assert world.store.schedule(member).on(TOMORROW)[0].state is State.PLANNED
    await world.clock.run(world.engine.sweep())
    assert world.fake.password_checks == checks + 1


async def test_a_broken_occupancy_reply_is_not_retried_with_another_member(world):
    first = await world.enroll("123000001")
    second = await world.enroll("123000002", name="周予安")
    world.book(first, "09:00-11:00", room="room321")
    world.book(second, "09:00-11:00", room="room322")
    world.fake.occupancy_broken = True
    await world.clock.run(world.engine.night(OPENING))
    assert len(world.fake.occupancy_reads) == 1
    assert world.store._db.execute("SELECT COUNT(*) FROM observed").fetchone()[0] == 0


async def test_daytime_catch_up_goes_in_time_order_so_nothing_runs_out_of_time(world):
    member = await world.enroll()
    world.clock.set(datetime(2026, 11, 26, 9, 59, 50, tzinfo=CHINA))
    world.book(member, "10:00-11:00")
    world.book(member, "19:00-20:00", room="room322")
    await world.clock.run(world.engine.sweep())
    assert world.starts() == ["10:00", "19:00"]
    assert [outcome for _, outcome, _ in world.outcomes(member)] == [Outcome.BOOKED, Outcome.BOOKED]
    first, second = (at for at, _, _ in world.fake.submissions)
    assert second - first >= GAP


async def test_a_session_lost_at_the_moment_of_submitting_is_renewed_once(world):
    member = await world.enroll()
    world.book(member, "09:00-10:00")

    async def scenario():
        night = asyncio.ensure_future(world.engine.night(OPENING))
        await world.clock.sleep_until(OPENING + timedelta(seconds=1))  # 第一张表单已经取好，还没提交
        world.fake.expire_sessions()
        await night

    await world.clock.run(scenario())
    assert world.outcomes(member) == [("09:00–10:00", Outcome.BOOKED, "保存成功")]
    assert len(world.fake.submissions) == 1


async def test_late_opening_is_retried_every_second(world):
    member = await world.enroll()
    world.book(member, "09:00-10:00")
    world.fake.opens_late = timedelta(seconds=6)
    await world.clock.run(world.engine.night(OPENING))
    assert world.outcomes(member) == [("09:00–10:00", Outcome.BOOKED, "保存成功")]
    assert [at - OPENING for at, _, _ in world.fake.submissions] == [
        timedelta(seconds=s) for s in (2.5, 3.5, 4.5, 5.5, 6.5)
    ]


async def test_a_slot_that_never_opens_gives_up_after_30_seconds(world):
    member = await world.enroll()
    world.book(member, "09:00-10:00")
    world.fake.opens_late = timedelta(minutes=5)
    await world.clock.run(world.engine.night(OPENING))
    ((_, outcome, message),) = world.outcomes(member)
    assert (outcome, message) == (Outcome.FAILED, "至多提前0天预订")
    assert 30 <= len(world.fake.submissions) <= 32


async def test_a_taken_segment_fails_and_the_rest_continue(world):
    member = await world.enroll()
    world.book(member, "13:00-17:00")
    world.fake.occupy("room327", TOMORROW, Span.parse("13:00", "14:00"))
    await world.clock.run(world.engine.night(OPENING))
    assert world.outcomes(member) == [
        ("13:00–15:00", Outcome.FAILED, "手速太慢，该时间段已经被预订啦"),
        ("15:00–17:00", Outcome.BOOKED, "保存成功"),
    ]
    assert world.store.schedule(member).on(TOMORROW)[0].result.label == "1/2 成功"


async def test_a_lost_reply_is_confirmed_in_my_bookings(world):
    member = await world.enroll()
    world.book(member, "09:00-10:00")
    world.fake.lose_replies = 1
    await world.clock.run(world.engine.night(OPENING))
    assert world.outcomes(member) == [("09:00–10:00", Outcome.BOOKED, "已在“我的预定”中确认")]
    assert len(world.fake.submissions) == 1


async def test_an_edit_just_before_21_is_what_gets_submitted(world):
    member = await world.enroll()
    booking = world.book(member, "09:00-10:00")

    async def scenario():
        task = asyncio.ensure_future(world.engine.night(OPENING))
        await world.clock.sleep_until(OPENING - timedelta(seconds=10))
        world.store.put_booking(
            Booking(member, "room412", Span.parse("10:00", "11:00"), TOMORROW, id=booking), world.clock.now()
        )
        await task

    await world.clock.run(scenario())
    (booked,) = world.fake.mine("123000001")
    assert (booked.room, str(booked.span)) == ("room412", "10:00–11:00")


async def test_a_booking_added_after_21_is_submitted_by_the_sweep(world):
    member = await world.enroll()
    world.clock.set(OPENING + timedelta(minutes=30))
    world.book(member, "20:00-21:00")
    await world.clock.run(world.engine.sweep())
    assert world.outcomes(member) == [("20:00–21:00", Outcome.BOOKED, "保存成功")]


async def test_segments_that_already_started_are_not_submitted():
    world = make_world(datetime(2026, 11, 26, 10, 30, tzinfo=CHINA))
    member = await world.enroll()
    world.book(member, "09:00-13:00", day=TOMORROW)
    await world.clock.run(world.engine.sweep())
    assert world.outcomes(member) == [
        ("09:00–11:00", Outcome.FAILED, "时间已过"),
        ("11:00–13:00", Outcome.BOOKED, "保存成功"),
    ]


async def test_an_outage_backs_off_and_recovers(world):
    member = await world.enroll()
    world.clock.set(OPENING + timedelta(minutes=10))
    world.book(member, "20:00-21:00")
    world.fake.down = True
    await world.clock.run(world.engine.sweep())
    assert world.outcomes(member) == []
    assert world.store.member(member).login_ok

    world.fake.down = False
    await world.clock.run(world.engine.sweep())
    assert world.outcomes(member) == []  # 还在退避期内

    world.clock.set(world.clock.now() + timedelta(minutes=3))
    await world.clock.run(world.engine.sweep())
    assert world.outcomes(member) == [("20:00–21:00", Outcome.BOOKED, "保存成功")]


async def test_a_segment_left_sent_by_a_crash_is_reconciled(world):
    member = await world.enroll()
    world.clock.set(OPENING + timedelta(minutes=1))
    booked = world.book(member, "09:00-10:00")
    lost = world.book(member, "11:00-12:00")
    world.fake.bookings.append(
        Booked("f0rm-1", "123000001", "room327", TOMORROW, Span.parse("09:00", "10:00"), world.clock.now())
    )
    for booking, school_id in ((booked, "f0rm-1"), (lost, "f0rm-2")):
        start = 9 * 60 if booking == booked else 11 * 60
        (segment,) = world.store.claim(
            member,
            booking,
            TOMORROW,
            [Span(start, start + 60)],
            "room327",
            "MPC327 管弦乐学部琴房（UP）",
            world.clock.now(),
        )
        world.store.mark_sent(segment, school_id, world.clock.now())

    await world.clock.run(world.engine.sweep())
    assert world.outcomes(member) == [
        ("09:00–10:00", Outcome.BOOKED, "已在“我的预定”中确认"),
        ("11:00–12:00", Outcome.SENT, ""),  # 查不到时先等一等，学校可能还没记下来
    ]

    world.clock.set(world.clock.now() + timedelta(minutes=11))
    await world.clock.run(world.engine.sweep())
    assert world.outcomes(member)[1] == ("11:00–12:00", Outcome.FAILED, "提交后没有收到答复，“我的预定”里也没有这一段")
    assert world.fake.submissions == []


async def test_a_booking_added_after_an_empty_2055_still_goes_at_21(world):
    member = await world.enroll()
    engine = world.engine

    async def scenario():
        night = asyncio.ensure_future(engine.night(OPENING))
        await world.clock.sleep_until(OPENING - timedelta(minutes=2))
        world.book(member, "09:00-10:00")
        await night
        assert engine._wake.is_set()
        await engine.sweep()

    await world.clock.run(scenario())
    assert world.outcomes(member) == [("09:00–10:00", Outcome.BOOKED, "保存成功")]
    assert world.fake.submissions[0][0] - OPENING < timedelta(seconds=10)


async def test_removing_a_member_during_the_night_does_not_break_the_run(world):
    gone = await world.enroll("123000001")
    stays = await world.enroll("123000002", name="周予安")
    world.book(gone, "09:00-10:00")
    world.book(stays, "09:00-10:00", room="room322")

    async def scenario():
        night = asyncio.ensure_future(world.engine.night(OPENING))
        await world.clock.sleep_until(OPENING - timedelta(minutes=2))
        world.store.remove_member(gone, world.clock.now())
        await night

    await world.clock.run(scenario())
    assert [user for _, user, _ in world.fake.submissions] == ["123000002"]
    assert world.outcomes(stays) == [("09:00–10:00", Outcome.BOOKED, "保存成功")]


async def test_a_today_booking_added_at_2056_is_not_held_up_by_the_night(world):
    member = await world.enroll()
    world.book(member, "13:00-14:00")

    async def scenario():
        night = asyncio.ensure_future(world.engine.night(OPENING))
        await world.clock.sleep_until(OPENING - timedelta(minutes=4))
        world.book(member, "22:00-23:00", day=OPENING.date())
        await world.engine.sweep()
        assert world.clock.now() < OPENING - timedelta(minutes=3)
        await night

    await world.clock.run(scenario())
    assert world.outcomes(member, OPENING.date()) == [("22:00–23:00", Outcome.BOOKED, "保存成功")]
    assert world.outcomes(member) == [("13:00–14:00", Outcome.BOOKED, "保存成功")]


async def test_an_outage_that_ends_just_before_21_still_books_at_the_opening(world):
    member = await world.enroll()
    world.book(member, "09:00-10:00")
    world.fake.down = True

    async def scenario():
        night = asyncio.ensure_future(world.engine.night(OPENING))
        await world.clock.sleep_until(OPENING - timedelta(seconds=1))
        world.fake.down = False
        await night

    await world.clock.run(scenario())
    assert world.outcomes(member) == [("09:00–10:00", Outcome.BOOKED, "保存成功")]
    assert world.fake.submissions[0][0] - OPENING < timedelta(seconds=30)


async def test_a_session_that_expires_after_2055_is_renewed_before_preparing(world):
    member = await world.enroll()
    world.book(member, "09:00-10:00")

    async def scenario():
        night = asyncio.ensure_future(world.engine.night(OPENING))
        await world.clock.sleep_until(OPENING - timedelta(minutes=2))
        world.fake.expire_sessions()
        await night

    await world.clock.run(scenario())
    assert world.outcomes(member) == [("09:00–10:00", Outcome.BOOKED, "保存成功")]
    assert world.fake.submissions[0][0] == OPENING + FIRST


async def test_removing_a_member_mid_run_stops_their_remaining_segments(world):
    member = await world.enroll()
    world.book(member, "13:00-19:00")

    async def scenario():
        night = asyncio.ensure_future(world.engine.night(OPENING))
        await world.clock.sleep_until(OPENING + FIRST + timedelta(milliseconds=500))
        world.store.remove_member(member, world.clock.now())
        await night

    await world.clock.run(scenario())
    assert len(world.fake.submissions) == 1


async def test_a_booking_added_while_a_sweep_is_busy_gets_picked_up_right_after(world):
    member = await world.enroll()
    world.clock.set(OPENING + timedelta(minutes=30))
    world.book(member, "20:00-21:00")
    engine = world.engine

    async def scenario():
        first = asyncio.ensure_future(engine.sweep())
        await world.clock.sleep(1.0)  # 第一段还在等表单满 2 秒
        world.book(member, "22:00-23:00")
        engine._wake.clear()
        engine._dispatch()  # 相当于新加预定时的 poke：成员正忙，这次扫不到
        await first
        assert engine._wake.is_set()  # 做完之后会再扫一遍
        await engine.sweep()

    await world.clock.run(scenario())
    assert [o for _, o, _ in world.outcomes(member)] == [Outcome.BOOKED, Outcome.BOOKED]
