"""学校场地系统的访问入口。

所有请求都从这里发出，只发往 *.cuhk.edu.cn。会在学校系统里留下记录的只有 `submit` 一个方法。
登录链路、读取可预定内容和提交的步骤见 PLAN.md 的“学校系统的访问方式”。
"""

from __future__ import annotations

import base64
import re
import secrets
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from ..domain import Room, Span, hhmm
from . import pages
from .errors import LoginRejected, NeedsUser, PortalError, SessionLost, Uncertain, Unexpected, Unreachable

ADFS_HOST = "sts.cuhk.edu.cn"
ATRUST = "https://atrust.cuhk.edu.cn"
BOOKING = "https://booking.cuhk.edu.cn"
BOOKING_HOST = "booking.cuhk.edu.cn"
ALLOWED_SUFFIX = ".cuhk.edu.cn"
# aTrust 门户用脚本跳到这个地址；client_id 是公开的客户端标识，不是密钥。
AUTHORIZE = "https://sts.cuhk.edu.cn/adfs/oauth2/authorize?" + urlencode(
    {
        "resource": ATRUST,
        "client_id": "e6d8a534-4b6f-4021-bb44-51ee9d0b63fd",
        "redirect_uri": f"{ATRUST}/passport/v1/auth/httpsOauth2",
        "response_type": "code",
    }
)
ATRUST_COMMON = {"clientType": "SDPBrowserClient", "platform": "Windows", "lang": "zh-CN"}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
TIMEOUT = 20.0
SUBMIT_TIMEOUT = 30.0
THEME = "练琴"
DEFAULT_PHONE = "000000"
FORM_ID = "bizFieldBookMainForm"
_REDIRECTS = {301, 302, 303, 307, 308}
_MAX_HOPS = 20
_TYPE_PAGES = 60


# ---- 读到的内容 ---------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    """一个账号可预定的内容。绑定时完整读一次，之后每晚只重读日历页。"""

    user_id: str
    name: str
    type_id: str
    person_tag: str
    book_type: str
    rule_id: str
    time_limit: int
    day_start: int
    day_end: int
    form_url: str
    rooms: tuple[Room, ...] = field(default=())

    def room(self, room_id: str) -> Room | None:
        return next((room for room in self.rooms if room.id == room_id), None)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["rooms"] = [[room.id, room.name] for room in self.rooms]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Profile:
        return cls(**{**data, "rooms": tuple(Room(room_id, name) for room_id, name in data["rooms"])})


@dataclass(frozen=True)
class Form:
    """取到的一张预定表单。表单里有服务器生成的预定编号和一次性令牌。"""

    room: str
    day: date
    span: Span
    pairs: tuple[tuple[str, str], ...]

    @property
    def booking_id(self) -> str:
        return dict(self.pairs).get("id", "")

    def filled(self, phone: str) -> list[tuple[str, str]]:
        values = {
            "theme": THEME,
            "bizFieldBookField.theme": THEME,
            "bizFieldBookField.useDesc": THEME,
            "bizFieldBookField.joinNums": "1",
            "userPhone": phone or DEFAULT_PHONE,
        }
        result = [(name, values.pop(name, value)) for name, value in self.pairs]
        return result + list(values.items())


class Verdict(StrEnum):
    BOOKED = "booked"
    TOO_FAST = "too_fast"
    STALE = "stale"
    NOT_OPEN = "not_open"
    TAKEN = "taken"
    REJECTED = "rejected"


_VERDICTS = (
    ("操作过快", Verdict.TOO_FAST),
    ("页面已失效", Verdict.STALE),
    ("至多提前", Verdict.NOT_OPEN),
    ("已经被预订", Verdict.TAKEN),
    ("手速太慢", Verdict.TAKEN),
)


@dataclass(frozen=True)
class Reply:
    verdict: Verdict
    message: str


def classify(status: int, text: str) -> Reply:
    payload = pages.json_or_none(text)
    if not isinstance(payload, dict):
        return Reply(Verdict.REJECTED, f"学校返回了无法识别的内容（HTTP {status}）")
    message = str(payload.get("message") or payload.get("msg") or "").strip()
    if payload.get("success") is True:
        return Reply(Verdict.BOOKED, message or "保存成功")
    verdict = next((verdict for marker, verdict in _VERDICTS if marker in message), Verdict.REJECTED)
    return Reply(verdict, message or f"学校拒绝了这次预定（HTTP {status}）")


@dataclass(frozen=True)
class Record:
    """“我的预定”里的一条。记录里的 fId 不是琴房编号，要按琴房名称和起止时间对应。"""

    id: str
    room_name: str
    start: str
    end: str
    status: str

    def matches(self, room_name: str, day: date, span: Span) -> bool:
        return (
            self.status == "2"
            and self.room_name.strip() == room_name.strip()
            and self.start[:16] == f"{day:%Y-%m-%d} {hhmm(span.start)}"
            and self.end[:16] == f"{day:%Y-%m-%d} {hhmm(span.end)}"
        )


# ---- 访问 ---------------------------------------------------------------------


class School:
    """一个学校账号的会话。每个成员各用一个，用完关闭。"""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._csrf = ""

    @classmethod
    def open(cls, cookies: Iterable[dict[str, str]] = (), *, transport: httpx.AsyncBaseTransport | None = None):
        client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            follow_redirects=False,
            timeout=TIMEOUT,
            transport=transport,
        )
        school = cls(client)
        for cookie in cookies:
            client.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
        return school

    async def __aenter__(self) -> School:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def cookies(self) -> list[dict[str, str]]:
        return [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in self._client.cookies.jar
            if c.value is not None
        ]

    # ---- 登录 -----------------------------------------------------------------

    async def sign_in(self, student_id: str, password: str | None, *, verify_password: bool = False) -> pages.Home:
        """登录到场地系统，返回首页上的用户信息。

        平时优先沿用保存的凭据，不提交密码。`verify_password` 为真时（绑定、换设备登录），
        要求整个过程中学校确实核对过密码，否则视为失败，以免沿用的凭据替一个错误的密码作保。
        """
        checked = False
        if verify_password:
            self._client.cookies.clear()
        else:
            home = await self._send("GET", f"{BOOKING}/a/system/main")
            if home.status_code == 200 and home.url.host == BOOKING_HOST:
                return pages.home(home.text)

        landing = await self._walk("GET", AUTHORIZE)
        if landing.url.host == ADFS_HOST:
            landing = await self._submit_password(landing, student_id, password)
            checked = True
        if landing.url.host != urlsplit(ATRUST).hostname:
            raise Unexpected(f"登录后停在了意外的位置（{landing.url.host}{landing.url.path}）")
        await self._finish_gateway(landing)

        landing = await self._walk("GET", f"{BOOKING}/")
        if landing.url.host != BOOKING_HOST:
            raise Unexpected("没能通过学校的网关")
        home = await self._send("GET", f"{BOOKING}/a/system/main")
        if home.status_code != 200:
            home = await self._walk("GET", f"{BOOKING}/sso/login")
            if home.url.host == ADFS_HOST:
                home = await self._submit_password(home, student_id, password)
                checked = True
        if not (home.status_code == 200 and home.url.host == BOOKING_HOST and home.url.path.startswith("/a/")):
            raise Unexpected(f"没能登录场地系统（停在 {home.url.host}{home.url.path}）")
        if verify_password and not checked:
            raise Unexpected("学校没有要求输入密码，无法核对")
        return pages.home(home.text)

    async def _submit_password(self, page: httpx.Response, student_id: str, password: str | None) -> httpx.Response:
        form = pages.password_form(page.text)
        if form is None:
            if pages.asks_extra_auth(page.text):
                raise NeedsUser("学校要求额外的身份验证")
            raise Unexpected("学校的登录页上没有密码框")
        action = urljoin(str(page.url), form.action)
        target = urlsplit(action)
        if target.scheme != "https" or target.hostname != ADFS_HOST:
            raise Unexpected("登录表单不是提交给学校的统一认证，已停止，密码没有发出")
        if not password:
            raise NeedsUser("保存的登录已过期，需要重新输入密码")
        payload = dict(form.pairs())
        user = student_id.strip()
        payload["UserName"] = user if ("@" in user or "\\" in user) else f"cuhksz\\{user}"
        payload["Password"] = password
        payload["Kmsi"] = "true"
        payload.setdefault("AuthMethod", "FormsAuthentication")
        result = await self._walk("POST", action, payload)
        if result.url.host == ADFS_HOST:
            if pages.asks_extra_auth(result.text):
                raise NeedsUser("学校要求额外的身份验证")
            if message := pages.login_error(result.text):
                raise LoginRejected(message)
            if result.status_code == 429 or result.status_code >= 500:
                raise Unreachable(f"学校的统一认证暂时不可用（HTTP {result.status_code}）")
            raise Unexpected("学校的统一认证没有放行，也没有说明原因")
        return result

    async def _finish_gateway(self, landing: httpx.Response) -> None:
        """代替 aTrust 门户页的脚本完成登录收尾：取 CSRF 令牌、上报环境、通过认证检查。"""
        flow = dict(landing.url.params)
        extra = pages.json_or_none(flow.get("data") or "") or {}
        config = await self._atrust("GET", "/passport/v1/public/authConfig", params={"mod": "1", "needTicket": "1"})
        self._csrf = str(((config.get("data") or {}).get("security") or {}).get("csrfToken") or "")
        env = extra.get("env") if isinstance(extra, dict) else None
        if isinstance(env, dict) and env.get("need") and extra.get("ticket"):
            device = secrets.token_hex(32)
            await self._atrust(
                "POST",
                "/controller/v1/public/reportEnv",
                json_body={
                    "ticket": extra["ticket"],
                    "deviceId": device,
                    "env": {"endpoint": {"device_id": device, "device": {"type": "browser"}}},
                },
            )
        service = flow.get("nextService") or ""
        for _ in range(4):
            if service == "auth/authCheck":
                result = await self._atrust("GET", "/passport/v1/auth/authCheck")
            elif service == "auth/accessCheck":
                result = await self._atrust("POST", "/passport/v1/auth/accessCheck", data={})
            elif service:
                raise NeedsUser("学校的网关要求额外的认证步骤")
            else:
                return
            if result.get("code") not in (0, "0", None):
                raise Unexpected(f"学校的网关没有放行（{result.get('message') or result.get('code')}）")
            service = (result.get("data") or {}).get("nextService") or ""

    # ---- 读取 -----------------------------------------------------------------

    async def read_profile(self) -> Profile:
        """按网站自己的顺序读出这个账号可预定的琴房、规则和表单地址。"""
        page = await self._booking("GET", "/a/system/main")
        me = pages.home(page.text)
        rows: list[dict[str, Any]] = []
        for page_no in range(1, _TYPE_PAGES + 1):
            listing = await self._booking(
                "GET",
                "/a/field/cat/bizFieldType/listDataByUser",
                params={
                    "userId": me.user_id,
                    "deptId": "",
                    "fcCode": "",
                    "area": "",
                    "position": "",
                    "search": "",
                    "pageNo": str(page_no),
                },
            )
            payload = pages.json_or_none(listing.text)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise Unexpected("场地类型列表的格式变了")
            rows += [row for row in payload["data"] if isinstance(row, dict)]
            if page_no >= int((payload.get("meta") or {}).get("pages") or 1):
                break
        kind = practice_rooms(rows)
        if kind is None:
            raise PortalError("这个账号在学校系统里没有可预定的琴房")
        details = await self._booking(
            "POST",
            "/a/field/client/fieldTypeDetails",
            data={"id": str(kind["id"]), "bookFlag": str(int(kind.get("bookFlag") or 0))},
        )
        person_tag, book_type = pages.entry(details.text)
        partial = Profile(
            user_id=me.user_id,
            name=me.name,
            type_id=str(kind["id"]),
            person_tag=person_tag,
            book_type=book_type,
            rule_id="",
            time_limit=120,
            day_start=0,
            day_end=23 * 60 + 30,
            form_url="",
        )
        profile, _ = await self.read_calendar(partial)
        return profile

    async def read_calendar(self, profile: Profile) -> tuple[Profile, datetime | None]:
        """重读日历页，返回更新后的内容和页面上的服务器时间。"""
        page = await self._booking(
            "POST",
            "/a/field/client/main",
            data={"id": profile.type_id, "bookType": profile.book_type, "personTag": profile.person_tag},
        )
        found = pages.calendar(page.text)
        updated = Profile(
            user_id=profile.user_id,
            name=profile.name,
            type_id=profile.type_id,
            person_tag=profile.person_tag,
            book_type=profile.book_type,
            rule_id=found.rule_id,
            time_limit=found.time_limit,
            day_start=found.day_start,
            day_end=found.day_end,
            form_url=found.form_url,
            rooms=found.rooms,
        )
        return updated, found.server_time

    async def form(self, profile: Profile, room: str, day: date, span: Span) -> Form:
        """取一张预定表单。未开放的日期也能取到，所以可以在 21:00 之前取好。"""
        path = pages.render(
            profile.form_url,
            {
                "fid": room,
                "startTime": f"{day:%Y-%m-%d} {hhmm(span.start)}",
                "endTime": f"{day:%Y-%m-%d} {hhmm(span.end)}",
                "bookedNum": "0",
                "isFitBook": "false",
            },
        )
        page = await self._booking("GET", path)
        found = pages.form_by_id(page.text, FORM_ID) if page.status_code == 200 else None
        if found is None or not found.has("bookToken"):
            raise Unexpected(f"没有取到预定表单（HTTP {page.status_code}）")
        return Form(room, day, span, tuple(found.pairs()))

    async def submit(self, profile: Profile, form: Form, phone: str) -> Reply:
        """提交一张表单。这是唯一会在学校系统里产生预定的请求。"""
        url = f"{BOOKING}/a/field/book/bizFieldBookMain/saveData?reBookMainId=&ruleId={profile.rule_id}"
        try:
            response = await self._client.post(
                url,
                content=urlencode(form.filled(phone)).encode(),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{BOOKING}/a/field/client/main",
                },
                timeout=SUBMIT_TIMEOUT,
                follow_redirects=False,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
            raise Unreachable(f"连不上学校的服务器（{type(error).__name__}）") from error
        except httpx.HTTPError as error:
            raise Uncertain(f"提交后没有收到答复（{type(error).__name__}）") from error
        if response.status_code >= 500:
            # 网关报错时学校可能已经保存了这条预定，不能当作失败
            raise Uncertain(f"提交后学校的服务器出错（HTTP {response.status_code}）")
        return classify(response.status_code, response.text)

    async def my_bookings(self, count: int = 30) -> list[Record]:
        """“我的预定”的最近几条，按创建时间从新到旧。"""
        page = await self._booking(
            "POST",
            "/a/field/book/bizFieldBookMainMe/listData",
            data={"pagination[page]": "1", "pagination[perpage]": str(count)},
        )
        payload = pages.json_or_none(page.text)
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise Unexpected("“我的预定”的格式变了")
        return [
            Record(
                str(row.get("id") or ""),
                str(row.get("nameZh") or ""),
                str(row.get("startTime") or ""),
                str(row.get("endTime") or ""),
                str(row.get("status") or ""),
            )
            for row in rows
            if isinstance(row, dict)
        ]

    # ---- 底层 -----------------------------------------------------------------

    async def _send(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        target = urlsplit(url)
        if target.scheme != "https" or not (target.hostname or "").endswith(ALLOWED_SUFFIX):
            raise Unexpected(f"学校的页面把请求引向了别处（{target.hostname}），已停止")
        try:
            return await self._client.request(
                method, url, data=data, json=json_body, headers=headers, follow_redirects=False
            )
        except httpx.HTTPError as error:
            raise Unreachable(f"连不上学校的服务器（{type(error).__name__}）") from error

    async def _walk(self, method: str, url: str, data: dict[str, str] | None = None) -> httpx.Response:
        """像浏览器一样跟随跳转：HTTP 重定向、meta refresh、自动提交的隐藏表单和简单的脚本跳转。"""
        for _ in range(_MAX_HOPS):
            response = await self._send(method, url, data=data)
            step = _next_step(response, data)
            if step is None:
                if response.status_code >= 500:
                    raise Unreachable(f"学校的服务器出错了（HTTP {response.status_code}）")
                return response
            method, url, data = step
        raise Unexpected("学校的页面跳转次数过多")

    async def _atrust(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> dict[str, Any]:
        url = f"{ATRUST}{path}?{urlencode({**ATRUST_COMMON, **(params or {})})}"
        headers = {
            "x-csrf-token": self._csrf,
            "x-sdp-traceid": secrets.token_hex(4),
            "x-sdp-rid": base64.b64encode(urlsplit(ATRUST).netloc.encode()).decode(),
        }
        response = await self._send(method, url, data=data, json_body=json_body, headers=headers)
        if response.status_code >= 500:
            raise Unreachable(f"学校的网关出错了（HTTP {response.status_code}）")
        payload = pages.json_or_none(response.text)
        return payload if isinstance(payload, dict) else {}

    async def _booking(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> httpx.Response:
        url = urljoin(f"{BOOKING}/", path.lstrip("/")) + (f"?{urlencode(params)}" if params else "")
        response = await self._send(
            method,
            url,
            data=data,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{BOOKING}/a/system/main"},
        )
        if response.status_code in _REDIRECTS:
            raise SessionLost("场地系统的登录已失效")
        # 预定表单的参数不对时学校返回 500，那是我们的问题，不该当作网络故障重试
        if response.status_code > 500 or (response.status_code == 500 and "/bookForm" not in path):
            raise Unreachable(f"场地系统出错了（HTTP {response.status_code}）")
        return response


def practice_rooms(rows: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """在可预定的场地类型里找琴房。优先名为“学生琴房”的，其次名字里带琴房的。"""
    bookable = [row for row in rows if _int(row.get("bookFlag")) > 0]
    named = [row for row in bookable if _looks_like_practice_rooms(row)]
    exact = [row for row in named if str(row.get("nameZh") or "").strip() == "学生琴房"]
    return (exact or named or [None])[0]


def _looks_like_practice_rooms(row: dict[str, Any]) -> bool:
    text = f"{row.get('nameZh') or ''} {row.get('nameEn') or ''}".lower()
    return "琴房" in text or "practice room" in text


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _next_step(response: httpx.Response, data: dict[str, str] | None) -> tuple[str, str, dict[str, str] | None] | None:
    base = str(response.url)
    if response.status_code in _REDIRECTS and "location" in response.headers:
        target = urljoin(base, response.headers["location"])
        if response.status_code in (307, 308):
            # 带着原来的内容重发；只允许发回同一台主机，免得把密码带到别处
            if urlsplit(target).hostname != response.url.host:
                raise Unexpected("学校的页面要求把表单内容转发到另一台主机，已停止")
            return response.request.method, target, data
        return "GET", target, None
    if response.status_code != 200 or "html" not in response.headers.get("content-type", ""):
        return None
    html = response.text
    refresh = re.search(r'http-equiv=["\']?refresh["\']?[^>]*content=["\']?\d+;\s*url=([^"\'>\s]+)', html, re.I)
    if refresh:
        return "GET", urljoin(base, refresh.group(1)), None
    found = pages.forms(html)
    if len(found) == 1 and found[0].hidden_only and ".submit()" in html:
        form = found[0]
        return form.method.upper() or "POST", urljoin(base, form.action), dict(form.pairs())
    if not found and len(html) < 3000:
        jump = re.search(r'location(?:\.href)?\s*=\s*["\']([^"\']+)["\']|location\.replace\(\s*["\']([^"\']+)', html)
        if jump:
            return "GET", urljoin(base, jump.group(1) or jump.group(2)), None
    return None
