from datetime import date, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from mus_booking.domain import CHINA, Booking, Outcome, Room, Span, State
from mus_booking.portal import Profile
from mus_booking.store import AdminAccount, MissingKey, Session, Store, Vault, password_matches
from mus_booking.store.db import INVITE_ALPHABET, SCHEMA_VERSION

NOW = datetime(2026, 11, 25, 20, 0, tzinfo=CHINA)
DAY = date(2026, 11, 26)


@pytest.fixture
def store():
    store = Store.open(":memory:", Vault(Fernet.generate_key()))
    yield store
    store.close()


def add(store: Store, student_id="123000001", password="pw") -> int:
    return store.add_member(
        student_id, "林知夏", "", password, [{"name": "a", "value": "b", "domain": "d", "path": "/"}], NOW
    )


# ---- 密钥与加密 ----------------------------------------------------------------


def test_a_missing_key_file_is_reported_clearly(tmp_path):
    with pytest.raises(MissingKey, match="mus-booking init"):
        Vault.load(tmp_path / "secret.key")


def test_the_key_file_is_created_once(tmp_path):
    path = tmp_path / "keys" / "secret.key"
    assert Vault.create(path) is True
    key = path.read_bytes()
    assert Vault.create(path) is False
    assert path.read_bytes() == key
    Vault.load(path)


def test_passwords_and_cookies_never_reach_the_database_file_in_plain_text(tmp_path):
    Vault.create(tmp_path / "secret.key")
    store = Store.open(tmp_path / "data.db", Vault.load(tmp_path / "secret.key"))
    member = store.add_member(
        "123000001",
        "林知夏",
        "",
        "P4ssw0rd-明文",
        [{"name": "JSESSIONID", "value": "COOKIE-77", "domain": "x", "path": "/"}],
        NOW,
    )
    assert store.credentials(member).password == "P4ssw0rd-明文"
    store.close()
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("data.db"))
    assert "P4ssw0rd-明文".encode() not in raw
    assert b"COOKIE-77" not in raw
    assert "林知夏".encode() in raw


def test_a_wrong_key_cannot_open_the_secrets(tmp_path):
    first = Store.open(tmp_path / "data.db", Vault(Fernet.generate_key()))
    member = add(first)
    first.close()
    second = Store.open(tmp_path / "data.db", Vault(Fernet.generate_key()))
    with pytest.raises(MissingKey, match="不匹配"):
        second.credentials(member)


# ---- 成员与凭据 ----------------------------------------------------------------


def test_background_jobs_do_not_overwrite_credentials_the_user_just_replaced(store):
    member = add(store)
    old = store.credentials(member)
    store.replace_credentials(member, "new-pw", [])
    assert store.save_cookies(member, [{"name": "x", "value": "y", "domain": "d", "path": "/"}], old.version) is False
    assert store.mark_login(member, False, "学号或密码不正确", old.version) is False
    assert store.member(member).login_ok
    current = store.credentials(member)
    assert (current.password, current.cookies) == ("new-pw", [])
    assert store.mark_login(member, False, "学号或密码不正确", current.version) is True
    assert not store.member(member).login_ok
    store.replace_credentials(member, "newer", [])
    assert store.member(member).login_ok


# ---- 邀请码 --------------------------------------------------------------------


def test_an_invite_binds_exactly_one_account(store):
    code = store.create_invite(NOW)
    assert len(code) == 4 and set(code) <= set(INVITE_ALPHABET)
    first, second = add(store), add(store, "123000002")
    assert store.bind_invite(code.lower(), first, NOW) is True
    assert store.bind_invite(code, second, NOW) is False
    assert store.invite(code).member_name == "林知夏"
    assert not store.invite(code).usable


def test_a_voided_invite_cannot_be_used_and_is_hidden(store):
    code = store.create_invite(NOW)
    store.void_invite(code, NOW)
    assert store.bind_invite(code, add(store), NOW) is False
    assert store.invites() == []


def test_removing_a_member_deletes_their_data_and_voids_their_invite(store):
    member = add(store)
    code = store.create_invite(NOW)
    store.bind_invite(code, member, NOW)
    booking = store.put_booking(Booking(member, "r1", Span(600, 720), DAY), NOW)
    store.claim(member, booking, DAY, [Span(600, 720)], "r1", "MPC327", NOW)
    token = store.create_session(member, NOW)
    store.remove_member(member, NOW)
    assert store.member(member) is None
    assert store.schedule(member).bookings == ()
    assert store.unsettled() == []
    assert store.session(token, NOW) is None
    assert store.invite(code).voided_at is not None


# ---- 版本升级 ------------------------------------------------------------------


def test_a_version_one_database_is_upgraded_in_place(tmp_path):
    vault = Vault(Fernet.generate_key())
    path = tmp_path / "mus-booking.db"
    store = Store.open(path, vault)
    member = add(store)
    code = store.create_invite(NOW)
    store._db.executescript(
        "DROP TABLE admin; DROP TABLE taken; DROP TABLE observed; ALTER TABLE members DROP COLUMN theme;"
        " ALTER TABLE members DROP COLUMN people; ALTER TABLE members DROP COLUMN purpose;"
        " CREATE TABLE admin_links (token_hash TEXT PRIMARY KEY, expires_at TEXT NOT NULL);"
        " PRAGMA user_version = 1;"
    )
    store.close()

    store = Store.open(path, vault)
    tables = {row[0] for row in store._db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"admin", "observed", "taken"} <= tables and "admin_links" not in tables
    assert "member_id" not in {row[1] for row in store._db.execute("PRAGMA table_info(admin)")}
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert store.member(member).student_id == "123000001"
    assert store.invite(code).usable
    old = store.member(member)
    assert (old.theme, old.people, old.purpose) == ("练琴", 1, "")  # 原有成员用默认值
    store.close()


def test_the_admin_account_survives_dropping_its_member_link(tmp_path):
    vault = Vault(Fernet.generate_key())
    path = tmp_path / "mus-booking.db"
    store = Store.open(path, vault)
    member = add(store)
    store.set_admin("Keeper", "open sesame", NOW)
    store._db.executescript(  # 退回第 5 版：管理账号还带着对应成员的一列
        "ALTER TABLE admin RENAME TO admin_v6;"
        " CREATE TABLE admin (id INTEGER PRIMARY KEY CHECK (id = 1), name TEXT NOT NULL, secret TEXT NOT NULL,"
        " member_id INTEGER REFERENCES members(id) ON DELETE SET NULL, updated_at TEXT NOT NULL);"
        f" INSERT INTO admin SELECT id, name, secret, {member}, updated_at FROM admin_v6;"
        " DROP TABLE admin_v6; PRAGMA user_version = 5;"
    )
    store.close()

    store = Store.open(path, vault)
    account, secret = store.admin_secret("keeper")
    assert account == AdminAccount("Keeper") and password_matches("open sesame", secret)
    assert store.member(member) is not None
    store.close()


def test_a_database_from_a_newer_version_is_refused(tmp_path):
    vault = Vault(Fernet.generate_key())
    path = tmp_path / "mus-booking.db"
    store = Store.open(path, vault)
    store._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    store.close()
    with pytest.raises(RuntimeError, match="请先更新程序"):
        Store.open(path, vault)


# ---- 抢手程度 ------------------------------------------------------------------


def test_hotness_is_the_share_of_observed_days_a_slot_went_early(store):
    evening = Span.parse("19:00", "21:00")
    assert store.hotness("room327", evening, DAY) == 0.0
    nights = {
        1: [("room327", 20 * 60, 21 * 60)],
        2: [],
        3: [("room327", 19 * 60, 20 * 60), ("room327", 20 * 60, 21 * 60)],  # 同一天两条只算一天
        4: [("room322", 19 * 60, 21 * 60)],  # 别的琴房
    }
    for back, taken in nights.items():
        store.record_taken(DAY - timedelta(days=back), taken, NOW)
    assert store.hotness("room327", evening, DAY) == 0.5
    assert store.hotness("room327", Span.parse("08:00", "10:00"), DAY) == 0.0

    store.record_taken(DAY - timedelta(days=1), [], NOW)  # 同一天再记一次会覆盖
    assert store.hotness("room327", evening, DAY) == 0.25
    store.record_taken(DAY + timedelta(days=60), [], NOW)  # 两个月前的观察随之清掉
    assert store._db.execute("SELECT COUNT(*) FROM observed").fetchone()[0] == 1
    assert store._db.execute("SELECT COUNT(*) FROM taken").fetchone()[0] == 0


# ---- 会话与管理账号 ------------------------------------------------------------


def test_sessions_store_only_a_hash_and_expire_when_idle(store):
    member = add(store)
    token = store.create_session(member, NOW)
    rows = store._db.execute("SELECT token_hash FROM sessions").fetchall()
    assert token not in {row[0] for row in rows}
    assert store.session(token, NOW + timedelta(days=100)).member_id == member
    assert store.session(token, NOW + timedelta(days=279)).member_id == member
    assert store.session(token, NOW + timedelta(days=460)) is None
    assert store.session("forged", NOW) is None


def test_the_admin_password_is_stored_only_as_a_hash(store):
    assert store.admin_account() is None
    assert store.admin_secret("Keeper") is None
    store.set_admin("Keeper", "open sesame", NOW)
    account, secret = store.admin_secret(" keeper ")
    assert account == AdminAccount("Keeper")
    assert "open sesame" not in secret and secret.startswith("scrypt$")
    assert password_matches("open sesame", secret)
    assert not password_matches("open sesam", secret)
    assert store.admin_secret("someone") is None


def test_resetting_the_admin_password_takes_admin_rights_back_from_every_browser(store):
    store.set_admin("Keeper", "open sesame", NOW)
    member = add(store)
    lost_phone = store.create_session(None, NOW, admin=True)
    own_phone = store.create_session(member, NOW, admin=True)
    store.set_admin("Keeper", "new secret", NOW)
    assert store.session(lost_phone, NOW).admin is False
    assert store.session(own_phone, NOW) == Session(member, False)


# ---- 预定与段 ------------------------------------------------------------------


def test_bookings_skips_and_segments_round_trip(store):
    member = add(store)
    weekly = store.put_booking(Booking(member, "r1", Span(1140, 1260), DAY, weekly=True), NOW)
    once = store.put_booking(Booking(member, "r2", Span(540, 660), DAY), NOW)
    store.skip(weekly, DAY + timedelta(days=7))
    assert store.claim(member, once, DAY, [Span(540, 660)], "r2", "MPC412", NOW)
    assert store.claim(member, once, DAY, [Span(540, 660)], "r2", "MPC412", NOW) == []

    schedule = store.schedule(member)
    assert [o.state for o in schedule.on(DAY)] == [State.SUBMITTED, State.PLANNED]
    assert schedule.on(DAY + timedelta(days=7))[0].state is State.SKIPPED

    store.unskip(weekly, [DAY + timedelta(days=7)])
    updated = schedule.booking(weekly)
    store.put_booking(Booking(member, "r3", updated.span, updated.first, weekly=True, paused=True, id=weekly), NOW)
    again = store.schedule(member)
    assert again.booking(weekly).room == "r3" and again.booking(weekly).paused
    assert again.on(DAY + timedelta(days=7))[0].state is State.PAUSED


def test_segments_move_from_queued_to_sent_to_settled(store):
    member = add(store)
    booking = store.put_booking(Booking(member, "r1", Span(780, 1140), DAY), NOW)
    ids = store.claim(member, booking, DAY, Span(780, 1140).split(120), "r1", "MPC327", NOW)
    assert [c.outcome for c in store.unsettled(member)] == [Outcome.QUEUED] * 3
    store.mark_sent(ids[0], "f0rm1", NOW)
    store.settle(ids[0], Outcome.BOOKED, "保存成功", NOW)
    store.mark_sent(ids[1], "f0rm2", NOW)
    unsettled = store.unsettled(member)
    assert [(c.outcome, c.school_id) for c in unsettled] == [(Outcome.SENT, "f0rm2"), (Outcome.QUEUED, "")]
    store.settle(ids[1], Outcome.FAILED, "手速太慢，该时间段已经被预订啦", NOW)
    store.settle(ids[2], Outcome.BOOKED, "保存成功", NOW)
    result = store.schedule(member).on(DAY)[0].result
    assert (result.label, result.tone) == ("2/3 成功", "partial")


def test_the_log_groups_results_by_night_and_member(store):
    first, second = add(store), add(store, "123000002")
    for member, outcomes in ((first, [Outcome.BOOKED, Outcome.BOOKED]), (second, [Outcome.BOOKED, Outcome.FAILED])):
        booking = store.put_booking(Booking(member, "r1", Span(600, 840), DAY), NOW)
        for segment_id, outcome in zip(
            store.claim(member, booking, DAY, Span(600, 840).split(120), "r1", "MPC327", NOW), outcomes, strict=True
        ):
            store.mark_sent(segment_id, "", NOW + timedelta(hours=1))
            store.settle(segment_id, outcome, "", NOW + timedelta(hours=1))
    entries = store.log(date(2026, 11, 1))
    assert [(e.member_id, e.day, e.booked, e.failed) for e in entries] == [
        (first, date(2026, 11, 25), 2, 0),
        (second, date(2026, 11, 25), 1, 1),
    ]
    assert store.latest_results() == {first: (2, 0), second: (1, 1)}


def test_profiles_round_trip(store):
    member = add(store)
    profile = Profile(
        "u1", "林知夏", "t", "Student", "0", "rule", 120, 0, 1410, "'/a'", (Room("r1", "MPC327 琴房（UP）"),)
    )
    store.save_profile(member, profile, NOW)
    store.save_profile(member, profile, NOW)
    assert store.profile(member) == profile


def test_runs_are_recorded_per_opening(store):
    opening = datetime(2026, 11, 25, 21, 0, tzinfo=CHINA)
    store.start_run(opening, opening - timedelta(minutes=5))
    store.finish_run(opening, opening + timedelta(seconds=6), skew_ms=-200)
    (run,) = store.runs()
    assert (run.finished_at - opening).seconds == 6 and run.skew_ms == -200
