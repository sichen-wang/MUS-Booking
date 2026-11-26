"""SQLite 里的全部数据：成员、邀请码、可预定内容、预定、跳过、提交的段、浏览器会话、管理账号、
每晚的记录，以及开放后不久被别人订走的时段。

服务只有一个进程，网页和每晚的提交都在同一个事件循环里调用这里的同步方法，
所以一次调用内的检查和写入不会被打断。命令行工具可能同时写入，由 WAL 和忙等待处理。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..domain import CHINA, Booking, Outcome, Schedule, Segment, Span
from ..portal import DEFAULT_THEME, Answers, Profile
from .vault import Vault

INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
"""邀请码用的字符，去掉了 0、O、1、I、L 这类容易看错的。"""
INVITE_LENGTH = 4
SESSION_IDLE = timedelta(days=180)
HOT_WINDOW = timedelta(days=28)
"""估计抢手程度时看多少天的观察。"""
KEEP_TAKEN = timedelta(days=56)
SCRYPT = {"n": 2**14, "r": 8, "p": 1}
"""管理密码的散列参数，约占 16 MB 内存，算一次几十毫秒。"""

_SCHEMA = """
CREATE TABLE members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    theme TEXT NOT NULL DEFAULT '练琴',
    people INTEGER NOT NULL DEFAULT 1,
    purpose TEXT NOT NULL DEFAULT '',
    secret BLOB NOT NULL,
    credential_version INTEGER NOT NULL DEFAULT 1,
    login_ok INTEGER NOT NULL DEFAULT 1,
    login_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE invites (
    code TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    member_id INTEGER REFERENCES members(id) ON DELETE SET NULL,
    bound_at TEXT,
    voided_at TEXT
);
CREATE TABLE profiles (
    member_id INTEGER PRIMARY KEY REFERENCES members(id) ON DELETE CASCADE,
    data TEXT NOT NULL,
    read_at TEXT NOT NULL
);
CREATE TABLE bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    room TEXT NOT NULL,
    start_min INTEGER NOT NULL,
    end_min INTEGER NOT NULL,
    first_day TEXT NOT NULL,
    weekly INTEGER NOT NULL,
    last_day TEXT,
    paused INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX bookings_member ON bookings(member_id);
CREATE TABLE skips (
    booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    PRIMARY KEY (booking_id, day)
);
CREATE TABLE segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    start_min INTEGER NOT NULL,
    end_min INTEGER NOT NULL,
    room TEXT NOT NULL,
    room_name TEXT NOT NULL,
    outcome TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    school_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at TEXT,
    settled_at TEXT,
    UNIQUE (booking_id, day, start_min)
);
CREATE INDEX segments_member ON segments(member_id, day);
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    member_id INTEGER REFERENCES members(id) ON DELETE CASCADE,
    admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    seen_at TEXT NOT NULL
);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    opening TEXT NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    skew_ms INTEGER
);
"""

_ADMIN = """
CREATE TABLE admin (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL,
    secret TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_TAKEN = """
CREATE TABLE observed (
    day TEXT PRIMARY KEY,
    seen_at TEXT NOT NULL
);
CREATE TABLE taken (
    day TEXT NOT NULL REFERENCES observed(day) ON DELETE CASCADE,
    room TEXT NOT NULL,
    start_min INTEGER NOT NULL,
    end_min INTEGER NOT NULL
);
CREATE INDEX taken_room ON taken(room, day);
"""

SCHEMA_VERSION = 6
_UPGRADES = {
    1: _ADMIN + "DROP TABLE admin_links;",  # 第 2 版：一次性管理链接换成管理账号
    2: _TAKEN,  # 第 3 版：记录开放后不久被别人订走的时段
    3: f"ALTER TABLE members ADD COLUMN theme TEXT NOT NULL DEFAULT '{DEFAULT_THEME}';",  # 第 4 版：成员自己的主题
    4: "ALTER TABLE members ADD COLUMN people INTEGER NOT NULL DEFAULT 1;"  # 第 5 版：参与人数和用途说明
    " ALTER TABLE members ADD COLUMN purpose TEXT NOT NULL DEFAULT '';",
    # 第 6 版：管理账号不再对应成员。这一列带外键，SQLite 删不掉，只好换一张表
    5: "CREATE TABLE admin_next (id INTEGER PRIMARY KEY CHECK (id = 1), name TEXT NOT NULL,"
    " secret TEXT NOT NULL, updated_at TEXT NOT NULL);"
    " INSERT INTO admin_next (id, name, secret, updated_at) SELECT id, name, secret, updated_at FROM admin;"
    " DROP TABLE admin; ALTER TABLE admin_next RENAME TO admin;",
}


# ---- 读出的记录 ---------------------------------------------------------------


@dataclass(frozen=True)
class Member:
    id: int
    student_id: str
    name: str
    phone: str
    login_ok: bool
    login_note: str
    created_at: datetime
    theme: str = DEFAULT_THEME
    people: int = 1
    purpose: str = ""

    @property
    def answers(self) -> Answers:
        """填进预定表单的几项。"""
        return Answers(self.phone, self.theme, self.people, self.purpose)


@dataclass(frozen=True)
class Credentials:
    password: str
    cookies: list[dict[str, str]]
    version: int


@dataclass(frozen=True)
class Invite:
    code: str
    created_at: datetime
    member_id: int | None
    member_name: str
    bound_at: datetime | None
    voided_at: datetime | None

    @property
    def usable(self) -> bool:
        return self.bound_at is None and self.voided_at is None


@dataclass(frozen=True)
class Session:
    member_id: int | None
    admin: bool


@dataclass(frozen=True)
class AdminAccount:
    """唯一的管理账号。它只管理，不对应任何成员。"""

    name: str


@dataclass(frozen=True)
class Claimed:
    """一段已经认领、等待或正在提交的预定。"""

    id: int
    member_id: int
    booking_id: int
    day: date
    span: Span
    room: str
    room_name: str
    outcome: Outcome
    school_id: str
    sent_at: datetime | None


@dataclass(frozen=True)
class LogEntry:
    member_id: int
    member_name: str
    day: date
    """提交发生的日期（中国时间）。"""
    booked: int
    failed: int


@dataclass(frozen=True)
class Run:
    opening: datetime
    started_at: datetime
    finished_at: datetime | None
    skew_ms: int | None


# ---- 存储 ---------------------------------------------------------------------


class Store:
    def __init__(self, connection: sqlite3.Connection, vault: Vault) -> None:
        self._db = connection
        self._vault = vault

    @classmethod
    def open(cls, path: Path | str, vault: Vault) -> Store:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        store = cls(connection, vault)
        store._migrate()
        return store

    def close(self) -> None:
        self._db.close()

    def _migrate(self) -> None:
        # 先拿写锁再读版本号：服务和命令行同时打开旧数据库时，只有一个会去升级
        with self.transaction():
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"数据库是第 {version} 版，比这个程序新，请先更新程序")
            if version == SCHEMA_VERSION:
                return
            steps = (
                [_SCHEMA + _ADMIN + _TAKEN] if version == 0 else [_UPGRADES[v] for v in range(version, SCHEMA_VERSION)]
            )
            for statement in "".join(steps).split(";"):  # 表结构里没有带分号的字符串
                if statement.strip():
                    self._db.execute(statement)
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._db.in_transaction:
            yield
            return
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def _one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        return self._db.execute(sql, args).fetchone()

    def _all(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return self._db.execute(sql, args).fetchall()

    # ---- 成员 -----------------------------------------------------------------

    def add_member(
        self,
        student_id: str,
        name: str,
        phone: str,
        password: str,
        cookies: list[dict[str, str]],
        now: datetime,
    ) -> int:
        secret = self._vault.seal({"password": password, "cookies": cookies})
        cursor = self._db.execute(
            "INSERT INTO members (student_id, name, phone, secret, created_at) VALUES (?, ?, ?, ?, ?)",
            (student_id, name, phone, secret, _t(now)),
        )
        return int(cursor.lastrowid or 0)

    def member(self, member_id: int) -> Member | None:
        row = self._one("SELECT * FROM members WHERE id = ?", member_id)
        return _member(row) if row else None

    def member_by_student(self, student_id: str) -> Member | None:
        row = self._one("SELECT * FROM members WHERE student_id = ?", student_id)
        return _member(row) if row else None

    def members(self) -> list[Member]:
        return [_member(row) for row in self._all("SELECT * FROM members ORDER BY created_at, id")]

    def credentials(self, member_id: int) -> Credentials:
        row = self._one("SELECT secret, credential_version FROM members WHERE id = ?", member_id)
        if row is None:
            raise KeyError(member_id)
        secret = self._vault.open(row["secret"])
        return Credentials(secret.get("password", ""), secret.get("cookies", []), row["credential_version"])

    def replace_credentials(self, member_id: int, password: str, cookies: list[dict[str, str]]) -> None:
        """用户本人重新登录之后：换上新密码和凭据，并恢复正常状态。"""
        secret = self._vault.seal({"password": password, "cookies": cookies})
        self._db.execute(
            "UPDATE members SET secret = ?, credential_version = credential_version + 1, login_ok = 1,"
            " login_note = '' WHERE id = ?",
            (secret, member_id),
        )

    def save_cookies(self, member_id: int, cookies: list[dict[str, str]], version: int) -> bool:
        """后台登录之后保存新的凭据；如果期间用户重新登录过（版本变了）或成员已被移除，不写。"""
        with self.transaction():
            try:
                current = self.credentials(member_id)
            except KeyError:
                return False
            if current.version != version:
                return False
            secret = self._vault.seal({"password": current.password, "cookies": cookies})
            self._db.execute("UPDATE members SET secret = ? WHERE id = ?", (secret, member_id))
            return True

    def mark_login(self, member_id: int, ok: bool, note: str, version: int) -> bool:
        cursor = self._db.execute(
            "UPDATE members SET login_ok = ?, login_note = ? WHERE id = ? AND credential_version = ?",
            (int(ok), note, member_id, version),
        )
        return cursor.rowcount == 1

    def save_answers(self, member_id: int, answers: Answers) -> None:
        """保存成员在设置里填的电话、主题、参与人数和用途说明。"""
        self._db.execute(
            "UPDATE members SET phone = ?, theme = ?, people = ?, purpose = ? WHERE id = ?",
            (answers.phone, answers.theme, answers.people, answers.purpose, member_id),
        )

    def remove_member(self, member_id: int, now: datetime) -> None:
        with self.transaction():
            self._db.execute(
                "UPDATE invites SET voided_at = COALESCE(voided_at, ?) WHERE member_id = ?",
                (_t(now), member_id),
            )
            self._db.execute("DELETE FROM members WHERE id = ?", (member_id,))

    # ---- 邀请码 ---------------------------------------------------------------

    def create_invite(self, now: datetime) -> str:
        while True:
            code = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(INVITE_LENGTH))
            try:
                self._db.execute("INSERT INTO invites (code, created_at) VALUES (?, ?)", (code, _t(now)))
                return code
            except sqlite3.IntegrityError:
                continue

    def invite(self, code: str) -> Invite | None:
        row = self._one(_INVITE_SELECT + " WHERE invites.code = ?", normalize_code(code))
        return _invite(row) if row else None

    def invites(self) -> list[Invite]:
        rows = self._all(_INVITE_SELECT + " WHERE invites.voided_at IS NULL ORDER BY invites.created_at DESC")
        return [_invite(row) for row in rows]

    def bind_invite(self, code: str, member_id: int, now: datetime) -> bool:
        cursor = self._db.execute(
            "UPDATE invites SET member_id = ?, bound_at = ? WHERE code = ? AND bound_at IS NULL AND voided_at IS NULL",
            (member_id, _t(now), normalize_code(code)),
        )
        return cursor.rowcount == 1

    def void_invite(self, code: str, now: datetime) -> None:
        self._db.execute(
            "UPDATE invites SET voided_at = ? WHERE code = ? AND voided_at IS NULL",
            (_t(now), normalize_code(code)),
        )

    # ---- 可预定的内容 ---------------------------------------------------------

    def save_profile(self, member_id: int, profile: Profile, now: datetime) -> None:
        self._db.execute(
            "INSERT INTO profiles (member_id, data, read_at) VALUES (?, ?, ?)"
            " ON CONFLICT (member_id) DO UPDATE SET data = excluded.data, read_at = excluded.read_at",
            (member_id, json.dumps(profile.to_dict(), ensure_ascii=False), _t(now)),
        )

    def profile(self, member_id: int) -> Profile | None:
        row = self._one("SELECT data FROM profiles WHERE member_id = ?", member_id)
        return Profile.from_dict(json.loads(row["data"])) if row else None

    # ---- 预定 -----------------------------------------------------------------

    def schedule(self, member_id: int) -> Schedule:
        bookings = tuple(
            _booking(row) for row in self._all("SELECT * FROM bookings WHERE member_id = ? ORDER BY id", member_id)
        )
        skips = frozenset(
            (row["booking_id"], date.fromisoformat(row["day"]))
            for row in self._all(
                "SELECT skips.* FROM skips JOIN bookings ON bookings.id = skips.booking_id"
                " WHERE bookings.member_id = ?",
                member_id,
            )
        )
        segments = tuple(
            _segment(row) for row in self._all("SELECT * FROM segments WHERE member_id = ? ORDER BY id", member_id)
        )
        return Schedule(bookings, skips, segments)

    def put_booking(self, booking: Booking, now: datetime) -> int:
        """新增（id 为 0）或覆盖一条预定，返回它的 id。"""
        values = (
            booking.room,
            booking.span.start,
            booking.span.end,
            booking.first.isoformat(),
            int(booking.weekly),
            booking.last.isoformat() if booking.last else None,
            int(booking.paused),
        )
        if booking.id:
            self._db.execute(
                "UPDATE bookings SET room = ?, start_min = ?, end_min = ?, first_day = ?, weekly = ?, last_day = ?,"
                " paused = ? WHERE id = ? AND member_id = ?",
                (*values, booking.id, booking.member_id),
            )
            return booking.id
        cursor = self._db.execute(
            "INSERT INTO bookings (room, start_min, end_min, first_day, weekly, last_day, paused, member_id,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*values, booking.member_id, _t(now)),
        )
        return int(cursor.lastrowid or 0)

    def delete_booking(self, booking_id: int) -> None:
        self._db.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))

    def skip(self, booking_id: int, day: date) -> None:
        self._db.execute("INSERT OR IGNORE INTO skips (booking_id, day) VALUES (?, ?)", (booking_id, day.isoformat()))

    def unskip(self, booking_id: int, days: Iterable[date]) -> None:
        self._db.executemany(
            "DELETE FROM skips WHERE booking_id = ? AND day = ?",
            [(booking_id, day.isoformat()) for day in days],
        )

    # ---- 段 -------------------------------------------------------------------

    def claim(
        self,
        member_id: int,
        booking_id: int,
        day: date,
        spans: Iterable[Span],
        room: str,
        room_name: str,
        now: datetime,
    ) -> list[int]:
        """把这一次的各段记为排队中，从此这一次算已提交。已经有段的不重复认领，返回空列表。"""
        with self.transaction():
            if self._one("SELECT 1 FROM segments WHERE booking_id = ? AND day = ?", booking_id, day.isoformat()):
                return []
            ids = []
            for span in spans:
                cursor = self._db.execute(
                    "INSERT INTO segments (member_id, booking_id, day, start_min, end_min, room, room_name, outcome,"
                    " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        member_id,
                        booking_id,
                        day.isoformat(),
                        span.start,
                        span.end,
                        room,
                        room_name,
                        "queued",
                        _t(now),
                    ),
                )
                ids.append(int(cursor.lastrowid or 0))
            return ids

    def mark_sent(self, segment_id: int, school_id: str, now: datetime) -> bool:
        """发出之前标记。这一段已经不在（例如成员刚被移除）时返回假，调用方不应再发。"""
        cursor = self._db.execute(
            "UPDATE segments SET outcome = 'sent', school_id = ?, sent_at = ?"
            " WHERE id = ? AND outcome IN ('queued', 'sent')",
            (school_id, _t(now), segment_id),
        )
        return cursor.rowcount == 1

    def requeue(self, segment_id: int) -> None:
        """学校明确拒绝了这一次尝试（还没开放、太快等），这一段回到排队中，重启后可以接着试。"""
        self._db.execute("UPDATE segments SET outcome = 'queued' WHERE id = ? AND outcome = 'sent'", (segment_id,))

    def settle(self, segment_id: int, outcome: Outcome, message: str, now: datetime) -> None:
        self._db.execute(
            "UPDATE segments SET outcome = ?, message = ?, settled_at = ?, sent_at = COALESCE(sent_at, ?) WHERE id = ?",
            (outcome.value, message, _t(now), _t(now), segment_id),
        )

    def unsettled(self, member_id: int | None = None) -> list[Claimed]:
        """排队中或已发出、还没有结果的段，按时间先后排列。"""
        sql = "SELECT * FROM segments WHERE outcome IN ('queued', 'sent')"
        args: tuple[Any, ...] = ()
        if member_id is not None:
            sql += " AND member_id = ?"
            args = (member_id,)
        return [_claimed(row) for row in self._all(sql + " ORDER BY day, start_min, id", *args)]

    def log(self, since: date) -> list[LogEntry]:
        """每天每个成员的提交结果，供管理页显示。日期按提交发生的那一天算。"""
        rows = self._all(
            "SELECT segments.member_id, members.name,"
            " substr(COALESCE(sent_at, segments.created_at), 1, 10) AS sent_day,"
            " SUM(outcome = 'booked') AS booked, SUM(outcome = 'failed') AS failed"
            " FROM segments JOIN members ON members.id = segments.member_id"
            " WHERE outcome IN ('booked', 'failed') AND substr(COALESCE(sent_at, segments.created_at), 1, 10) >= ?"
            " GROUP BY segments.member_id, sent_day ORDER BY sent_day DESC, members.created_at, members.id",
            since.isoformat(),
        )
        return [
            LogEntry(
                row["member_id"],
                row["name"],
                date.fromisoformat(row["sent_day"]),
                row["booked"],
                row["failed"],
            )
            for row in rows
        ]

    def latest_results(self) -> dict[int, tuple[int, int]]:
        """每个成员最近一个有结果的日子里，成功和失败的段数。"""
        result: dict[int, tuple[int, int]] = {}
        for entry in self.log(date.min):
            result.setdefault(entry.member_id, (entry.booked, entry.failed))
        return result

    # ---- 浏览器会话 -----------------------------------------------------------

    def create_session(self, member_id: int | None, now: datetime, *, admin: bool = False) -> str:
        token = secrets.token_urlsafe(32)
        self._db.execute(
            "INSERT INTO sessions (token_hash, member_id, admin, created_at, seen_at) VALUES (?, ?, ?, ?, ?)",
            (_hash(token), member_id, int(admin), _t(now), _t(now)),
        )
        return token

    def session(self, token: str, now: datetime) -> Session | None:
        if not token:
            return None
        row = self._one("SELECT * FROM sessions WHERE token_hash = ?", _hash(token))
        if row is None:
            return None
        seen = datetime.fromisoformat(row["seen_at"])
        if now - seen > SESSION_IDLE:
            self._db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))
            return None
        if now - seen > timedelta(hours=1):
            self._db.execute("UPDATE sessions SET seen_at = ? WHERE token_hash = ?", (_t(now), _hash(token)))
        return Session(row["member_id"], bool(row["admin"]))

    def drop_session(self, token: str) -> None:
        self._db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))

    # ---- 管理账号 -------------------------------------------------------------

    def set_admin(self, name: str, password: str, now: datetime) -> None:
        """设置管理账号的名字和密码。

        重新设置时，所有浏览器上的管理权限一并收回（例如手机丢了）。
        """
        secret = _hash_password(password)
        with self.transaction():
            self._db.execute(
                "INSERT INTO admin (id, name, secret, updated_at) VALUES (1, ?, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET name = excluded.name, secret = excluded.secret,"
                " updated_at = excluded.updated_at",
                (name, secret, _t(now)),
            )
            self._db.execute("UPDATE sessions SET admin = 0 WHERE admin = 1")

    def admin_account(self) -> AdminAccount | None:
        row = self._one("SELECT name FROM admin WHERE id = 1")
        return AdminAccount(row["name"]) if row else None

    def admin_secret(self, name: str) -> tuple[AdminAccount, str] | None:
        """名字（不分大小写）对得上时，返回管理账号和密码散列，由调用方用 `password_matches` 核对。

        核对要算几十毫秒的 scrypt，网页里放到别的线程去算，免得耽误同一个事件循环里的提交。
        """
        row = self._one("SELECT name, secret FROM admin WHERE id = 1")
        if row is None or row["name"].casefold() != name.strip().casefold():
            return None
        return AdminAccount(row["name"]), row["secret"]

    # ---- 抢手程度 -------------------------------------------------------------

    def record_taken(self, day: date, taken: Iterable[tuple[str, int, int]], now: datetime) -> None:
        """记下开放后不久这一天已被别人订走的时段（琴房、开始和结束的分钟）。同一天再记一次会覆盖。"""
        with self.transaction():
            self._db.execute(
                "DELETE FROM observed WHERE day = ? OR day < ?", (day.isoformat(), (day - KEEP_TAKEN).isoformat())
            )
            self._db.execute("INSERT INTO observed (day, seen_at) VALUES (?, ?)", (day.isoformat(), _t(now)))
            self._db.executemany(
                "INSERT INTO taken (day, room, start_min, end_min) VALUES (?, ?, ?, ?)",
                [(day.isoformat(), room, start, end) for room, start, end in taken],
            )

    def hotness(self, room: str, span: Span, day: date) -> float:
        """这间琴房的这一段有多抢手：day 之前四周观察过的日子里，开放后不久它已被别人订走的比例。"""
        since, until = (day - HOT_WINDOW).isoformat(), day.isoformat()
        seen = self._one("SELECT COUNT(*) FROM observed WHERE day >= ? AND day < ?", since, until)[0]
        if not seen:
            return 0.0
        hits = self._one(
            "SELECT COUNT(DISTINCT day) FROM taken"
            " WHERE room = ? AND day >= ? AND day < ? AND start_min < ? AND end_min > ?",
            room,
            since,
            until,
            span.end,
            span.start,
        )[0]
        return hits / seen

    def our_spans(self, day: date) -> set[tuple[str, int, int]]:
        """所有成员这一天订到的段，以及已经发出、还在核对的段，用来从占用情况里排除自己人。"""
        rows = self._all(
            "SELECT room, start_min, end_min FROM segments WHERE day = ? AND outcome IN ('booked', 'sent')",
            day.isoformat(),
        )
        return {(row["room"], row["start_min"], row["end_min"]) for row in rows}

    # ---- 每晚的记录 -----------------------------------------------------------

    def start_run(self, opening: datetime, now: datetime) -> None:
        self._db.execute(
            "INSERT INTO runs (opening, started_at) VALUES (?, ?)"
            " ON CONFLICT (opening) DO UPDATE SET started_at = excluded.started_at, finished_at = NULL",
            (_t(opening), _t(now)),
        )

    def finish_run(self, opening: datetime, now: datetime, skew_ms: int | None) -> None:
        self._db.execute(
            "UPDATE runs SET finished_at = ?, skew_ms = ? WHERE opening = ?", (_t(now), skew_ms, _t(opening))
        )

    def runs(self, limit: int = 14) -> list[Run]:
        rows = self._all("SELECT * FROM runs ORDER BY opening DESC LIMIT ?", limit)
        return [
            Run(
                datetime.fromisoformat(row["opening"]),
                datetime.fromisoformat(row["started_at"]),
                datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
                row["skew_ms"],
            )
            for row in rows
        ]


# ---- 转换 ---------------------------------------------------------------------

_INVITE_SELECT = (
    "SELECT invites.*, COALESCE(members.name, '') AS member_name FROM invites"
    " LEFT JOIN members ON members.id = invites.member_id"
)


def normalize_code(code: str) -> str:
    return code.strip().upper()


def _t(moment: datetime) -> str:
    """一律存成中国时间，这样取前十个字符就是中国的日期。"""
    if moment.tzinfo is None:
        raise ValueError("时间必须带时区")
    return moment.astimezone(CHINA).isoformat(timespec="milliseconds")


def _maybe(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${salt.hex()}${digest.hex()}"


def password_matches(password: str, stored: str) -> bool:
    _, n, r, p, salt, digest = stored.split("$")
    candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    return hmac.compare_digest(candidate, bytes.fromhex(digest))


def _member(row: sqlite3.Row) -> Member:
    return Member(
        row["id"],
        row["student_id"],
        row["name"],
        row["phone"],
        bool(row["login_ok"]),
        row["login_note"],
        datetime.fromisoformat(row["created_at"]),
        row["theme"],
        row["people"],
        row["purpose"],
    )


def _invite(row: sqlite3.Row) -> Invite:
    return Invite(
        row["code"],
        datetime.fromisoformat(row["created_at"]),
        row["member_id"],
        row["member_name"],
        _maybe(row["bound_at"]),
        _maybe(row["voided_at"]),
    )


def _booking(row: sqlite3.Row) -> Booking:
    return Booking(
        member_id=row["member_id"],
        room=row["room"],
        span=Span(row["start_min"], row["end_min"]),
        first=date.fromisoformat(row["first_day"]),
        weekly=bool(row["weekly"]),
        last=date.fromisoformat(row["last_day"]) if row["last_day"] else None,
        paused=bool(row["paused"]),
        id=row["id"],
    )


def _segment(row: sqlite3.Row) -> Segment:
    return Segment(
        booking_id=row["booking_id"],
        day=date.fromisoformat(row["day"]),
        span=Span(row["start_min"], row["end_min"]),
        outcome=Outcome(row["outcome"]),
        message=row["message"],
        at=_maybe(row["settled_at"]) or _maybe(row["sent_at"]),
        id=row["id"],
    )


def _claimed(row: sqlite3.Row) -> Claimed:
    return Claimed(
        id=row["id"],
        member_id=row["member_id"],
        booking_id=row["booking_id"],
        day=date.fromisoformat(row["day"]),
        span=Span(row["start_min"], row["end_min"]),
        room=row["room"],
        room_name=row["room_name"],
        outcome=Outcome(row["outcome"]),
        school_id=row["school_id"],
        sent_at=_maybe(row["sent_at"]),
    )
