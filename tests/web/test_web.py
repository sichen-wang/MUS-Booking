"""网页层：绑定、登录、添加与校验、权限、管理页、同源检查和路径前缀。"""

import re
from datetime import date, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from mus_booking.domain import CHINA, Booking, Outcome, Span
from mus_booking.engine import Engine
from mus_booking.portal.fake import FakeSchool
from mus_booking.store import Store, Vault
from mus_booking.web.app import Services, create_app

NOW = datetime(2026, 11, 25, 10, 0, tzinfo=CHINA)
TODAY = NOW.date()


class Clock:
    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.current.timestamp()

    async def sleep(self, seconds: float) -> None:
        return None

    async def sleep_until(self, moment: datetime) -> None:
        return None


class World:
    def __init__(self, root_path: str = "") -> None:
        self.clock = Clock(NOW)
        self.fake = FakeSchool(now=self.clock.now)
        self.fake.add_account("123000001", "right", "林知夏")
        self.fake.add_account("123000002", "right", "周予安")
        self.store = Store.open(":memory:", Vault(Fernet.generate_key()))
        engine = Engine(self.store, self.clock, self.fake.school)
        self.app = create_app(Services(self.store, engine, self.clock, self.fake.school), run_engine=False)
        self.client = TestClient(self.app, root_path=root_path, base_url="http://testserver")

    def invite(self) -> str:
        return self.store.create_invite(NOW)

    def bind(self, student_id="123000001", password="right", client=None, code=None) -> TestClient:
        client = client or self.client
        response = client.post(
            "/bind", data={"code": code or self.invite(), "student_id": student_id, "password": password, "phone": ""}
        )
        assert response.status_code == 200, response.text
        return client

    def new_client(self) -> TestClient:
        return TestClient(self.app, base_url="http://testserver")


@pytest.fixture
def world():
    world = World()
    yield world
    world.store.close()


def text(response) -> str:
    return re.sub(r"\s+", " ", response.text)


# ---- 绑定与登录 ----------------------------------------------------------------


def test_visitors_see_the_bind_page_with_the_invite_filled_in(world):
    response = world.client.get("/?invite=k7q2")
    assert "每晚 21:00" in response.text
    assert 'value="K7Q2"' in response.text


def test_binding_signs_in_and_uses_up_the_invite(world):
    code = world.invite()
    world.bind(code=code)
    home = world.client.get("/")
    assert 'aria-label="账号">林<' in home.text
    assert not world.store.invite(code).usable
    other = world.new_client()
    again = other.post("/bind", data={"code": code, "student_id": "123000002", "password": "right"})
    assert "邀请码无效" in again.text


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({"code": "ZZZZ", "student_id": "123000001", "password": "right"}, "邀请码无效"),
        ({"student_id": "123000001", "password": "wrong"}, "学号或密码不对"),
        ({"student_id": "", "password": "right"}, "请填写学号"),
        ({"student_id": "123000001", "password": "right", "phone": "abc"}, "电话只能填数字"),
    ],
)
def test_binding_errors_are_shown_on_the_form(world, form, message):
    form = {"code": world.invite(), **form}
    response = world.client.post("/bind", data=form)
    assert response.status_code == 200
    assert message in response.text
    assert "right" not in response.text  # 密码不会回填到页面上
    assert world.store.members() == []


def test_the_same_student_cannot_bind_twice(world):
    world.bind()
    response = world.new_client().post(
        "/bind", data={"code": world.invite(), "student_id": "123000001", "password": "right"}
    )
    assert "已经绑定过" in response.text


def test_signing_in_on_a_new_device_checks_the_password_with_the_school(world):
    world.bind()
    phone = world.new_client()
    assert "学号或密码不对" in phone.post("/login", data={"student_id": "123000001", "password": "nope"}).text
    phone.post("/login", data={"student_id": "123000001", "password": "right"})
    assert "下一次自动预定" in phone.get("/").text or "添加预定" in phone.get("/").text
    assert "还没有绑定" in world.new_client().post("/login", data={"student_id": "123009999", "password": "x"}).text


def test_too_many_attempts_are_refused(world):
    world.bind()
    phone = world.new_client()
    for _ in range(8):
        phone.post("/login", data={"student_id": "123000001", "password": "nope"})
    response = phone.post("/login", data={"student_id": "123000001", "password": "right"})
    assert "尝试次数过多" in response.text


def test_logging_out_forgets_the_session(world):
    world.bind()
    world.client.post("/logout")
    assert "每晚 21:00" in world.client.get("/").text


# ---- 预定 ----------------------------------------------------------------------


def add(client, day: date, start="19:00", end="21:00", room="room327", weekly=False):
    data = {"day": day.isoformat(), "start": start, "end": end, "room": room}
    if weekly:
        data["weekly"] = "on"
    return client.post("/add", data=data)


def test_adding_a_booking_shows_it_on_the_home_page(world):
    world.bind()
    add(world.client, TODAY + timedelta(days=2), weekly=True)
    home = text(world.client.get("/"))
    assert "19:00–21:00" in home
    assert "下一次自动预定 · 明晚 21:00" in home


@pytest.mark.parametrize(
    ("day_offset", "start", "end", "message"),
    [
        (0, "09:00", "10:00", "开始时间已过"),
        (7, "19:00", "21:00", "只能选未来 7 天"),
        (1, "21:00", "19:00", "结束要晚于开始"),
        (1, "23:00", "24:00", "不在可预定的时间范围内"),
    ],
)
def test_invalid_bookings_are_refused(world, day_offset, start, end, message):
    world.bind()
    response = add(world.client, TODAY + timedelta(days=day_offset), start, end)
    assert response.status_code == 200
    assert message in response.text


def test_overlapping_bookings_are_refused(world):
    world.bind()
    add(world.client, TODAY + timedelta(days=1), "13:00", "15:00")
    response = add(world.client, TODAY + timedelta(days=1), "14:00", "16:00", room="room322")
    assert "时间与已有的预定重叠" in response.text


def test_skip_restore_pause_and_delete(world):
    world.bind()
    add(world.client, TODAY + timedelta(days=2), weekly=True)
    (booking,) = world.store.schedule(1).bookings
    day = (TODAY + timedelta(days=2)).isoformat()
    path = f"/b/{booking.id}/{day}"
    world.client.post(f"{path}/skip")
    assert "已跳过" in world.client.get(path).text
    world.client.post(f"{path}/unskip")
    world.client.post(f"{path}/pause")
    assert "恢复每周重复" in world.client.get(path).text
    world.client.post(f"{path}/resume")
    world.client.post(f"{path}/delete")
    assert world.store.schedule(1).bookings == ()


def test_editing_changes_the_booking(world):
    world.bind()
    add(world.client, TODAY + timedelta(days=1))
    (booking,) = world.store.schedule(1).bookings
    day = (TODAY + timedelta(days=1)).isoformat()
    world.client.post(
        f"/b/{booking.id}/{day}/edit", data={"day": day, "start": "08:00", "end": "09:30", "room": "room412"}
    )
    (edited,) = world.store.schedule(1).bookings
    assert (edited.id, str(edited.span), edited.room) == (booking.id, "08:00–09:30", "room412")


def test_a_submitted_one_off_offers_no_actions_and_refuses_edits(world):
    world.bind()
    booking = world.store.put_booking(Booking(1, "room327", Span.parse("19:00", "21:00"), TODAY), NOW)
    (segment,) = world.store.claim(1, booking, TODAY, [Span.parse("19:00", "21:00")], "room327", "MPC327", NOW)
    world.store.settle(segment, Outcome.BOOKED, "保存成功", NOW)
    detail = world.client.get(f"/b/{booking}/{TODAY}")
    assert "预定成功" in detail.text and "删除" not in detail.text
    response = world.client.post(
        f"/b/{booking}/{TODAY}/edit",
        data={"day": TODAY.isoformat(), "start": "20:00", "end": "21:00", "room": "room327"},
    )
    assert "已经提交" in response.text


def test_members_cannot_see_or_touch_each_others_bookings(world):
    world.bind()
    other = world.bind("123000002", client=world.new_client())
    add(world.client, TODAY + timedelta(days=1))
    (booking,) = world.store.schedule(1).bookings
    path = f"/b/{booking.id}/{TODAY + timedelta(days=1)}"
    assert other.get(path).status_code == 404
    assert other.post(f"{path}/delete").status_code == 404
    assert len(world.store.schedule(1).bookings) == 1


def test_pages_for_members_send_visitors_home(world):
    assert world.client.get("/add", follow_redirects=False).headers["location"] == "/"
    assert world.client.post("/add", data={}, follow_redirects=False).status_code == 303


# ---- 管理页 --------------------------------------------------------------------


def test_the_admin_page_is_hidden_without_the_admin_flag(world):
    world.bind()
    assert world.client.get("/admin").status_code == 404
    assert world.client.post("/admin/invites").status_code == 404


def test_an_admin_link_works_once(world):
    token = world.store.create_admin_link(NOW)
    admin = world.new_client()
    assert admin.get(f"/admin/enter/{token}").status_code == 200
    page = admin.get("/admin")
    assert page.status_code == 200 and "邀请码" in page.text
    assert world.new_client().get(f"/admin/enter/{token}").status_code == 404


def test_admins_generate_invites_and_remove_members(world):
    world.bind()
    admin = world.new_client()
    admin.get(f"/admin/enter/{world.store.create_admin_link(NOW)}")
    admin.post("/admin/invites")
    codes = [invite.code for invite in world.store.invites() if invite.usable]
    assert len(codes) == 1
    assert f"?invite={codes[0]}" in admin.get("/admin").text
    admin.post("/admin/members/1/remove")
    assert world.store.members() == []


# ---- 同源、HTMX 与路径前缀 -----------------------------------------------------


def test_posts_from_other_sites_are_refused(world):
    response = world.client.post("/login", data={}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_htmx_posts_navigate_with_hx_location(world):
    world.bind()
    response = world.client.post(
        "/add",
        data={"day": (TODAY + timedelta(days=1)).isoformat(), "start": "19:00", "end": "21:00", "room": "room327"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert '"path": "/"' in response.headers["HX-Location"]


def test_everything_works_under_a_path_prefix():
    world = World(root_path="/booking")
    page = world.client.get("/?invite=abcd")
    links = re.findall(r'(?:href|action|src)="([^"]+)"', page.text)
    assert links and all(link.startswith("/booking/") for link in links)
    response = world.client.post(
        "/bind", data={"code": world.invite(), "student_id": "123000001", "password": "right"}, follow_redirects=False
    )
    assert response.headers["location"] == "/booking/"
    assert "Path=/booking/" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    world.store.close()
