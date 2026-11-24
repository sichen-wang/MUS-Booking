"""在进程内运行的假学校系统。

它走同样的登录链路、给出与真实页面写法一致的页面，并按实测到的规则答复提交：
21:00 开放、取表单后至少等 2 秒、令牌必须有效、被占用的时段拒绝。测试和预览命令都用它。
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape
from urllib.parse import parse_qsl, urlencode

import httpx

from ..domain import CHINA, Room, Span, hhmm, open_at, parse_hhmm
from .school import ATRUST, BOOKING, School

TYPE_ID = "af15efadc379429885681cbad7b1ec12"
RULE_ID = "4b4d6e5c826c425b9a5ed7a02a46656a"

DEFAULT_ROOMS = tuple(
    Room(f"room{code}", name)
    for code, name in (
        ("321", "MPC321 室内乐琴房（GP）"),
        ("322", "MPC322 室内乐琴房（UP）"),
        ("319", "MPC319 管弦乐学部琴房"),
        ("327", "MPC327 管弦乐学部琴房（UP）"),
        ("328", "MPC328 管弦乐学部琴房（UP）"),
        ("412", "MPC412 室内乐琴房（GP）"),
        ("425", "MPC425 室内乐琴房（GP）"),
        ("518", "MPC518 室内乐琴房（Double GP）"),
        ("524", "MPC524室内乐琴房（Double GP）"),
    )
)

_FORM_URL = (
    "'/a/field/client/bookForm?fId=' + fid + '&bizFieldBookField.startTime=' + startTime + "
    "'&bizFieldBookField.endTime=' + endTime + '&repFlag=0' +\n"
    "            '&bookType=0&userTag=Student&approvalFlag=0&extend2=&bookedNum=' + bookedNum + "
    "'&fitBook=' + isFitBook + '&isDeptAdmin=false' + '&adMost=0'"
)


@dataclass
class Account:
    student_id: str
    password: str
    name: str


@dataclass
class Booked:
    id: str
    student_id: str
    room: str
    day: date
    span: Span
    created: datetime
    status: str = "2"


@dataclass
class _Token:
    student_id: str
    form_id: str
    room: str
    day: date
    span: Span
    issued: datetime


@dataclass
class FakeSchool:
    now: Callable[[], datetime] = field(default=lambda: datetime.now(CHINA))
    rooms: tuple[Room, ...] = DEFAULT_ROOMS
    accounts: dict[str, Account] = field(default_factory=dict)
    bookings: list[Booked] = field(default_factory=list)
    opens_late: timedelta = timedelta(0)
    """学校实际开放的时刻比 21:00 晚多少。"""
    down: bool = False
    """为真时所有请求都连不上。"""
    lose_replies: int = 0
    """接下来这么多次提交照常处理，但答复在路上丢失。"""
    reject_passwords: bool = False
    """为真时任何密码都被拒绝（模拟用户改了学校密码）。"""
    password_checks: int = 0
    submissions: list[tuple[datetime, str, str]] = field(default_factory=list)
    _codes: dict[str, str] = field(default_factory=dict)
    _sessions: dict[str, str] = field(default_factory=dict)
    _gateway: dict[str, tuple[str, bool]] = field(default_factory=dict)
    _tokens: dict[str, _Token] = field(default_factory=dict)

    def add_account(self, student_id: str, password: str, name: str = "测试同学") -> Account:
        account = Account(student_id, password, name)
        self.accounts[student_id] = account
        return account

    def occupy(self, room: str, day: date, span: Span, *, by: str = "someone") -> None:
        """让别人先订走一个时段。"""
        self.bookings.append(Booked(secrets.token_hex(16), by, room, day, span, self.now()))

    def expire_sessions(self) -> None:
        """让所有登录状态失效，包括统一认证的“保持登录”。"""
        self._sessions.clear()
        self._gateway.clear()

    def mine(self, student_id: str) -> list[Booked]:
        return [b for b in self.bookings if b.student_id == student_id and b.status == "2"]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def school(self, cookies=()) -> School:
        return School.open(cookies, transport=self.transport)

    # ---- 路由 -----------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("学校系统连不上", request=request)
        host, path = request.url.host, request.url.path
        cookies = _cookies(request)
        if host == "sts.cuhk.edu.cn" and path == "/adfs/oauth2/authorize":
            return self._adfs(request, cookies)
        if host == "atrust.cuhk.edu.cn":
            return self._atrust(request, cookies)
        if host == "booking.cuhk.edu.cn":
            return self._booking(request, cookies)
        return httpx.Response(404, request=request)

    def _adfs(self, request: httpx.Request, cookies: dict[str, str]) -> httpx.Response:
        query = dict(request.url.params)
        redirect = query.get("redirect_uri", "")
        signed_in = self._sessions.get(cookies.get("MSISAuth", ""))
        if request.method == "GET" and signed_in and not self.reject_passwords:
            return self._redirect_with_code(request, redirect, signed_in)
        if request.method == "POST":
            body = dict(parse_qsl(request.content.decode()))
            self.password_checks += 1
            user = body.get("UserName", "").removeprefix("cuhksz\\")
            account = self.accounts.get(user)
            if account and account.password == body.get("Password") and not self.reject_passwords:
                token = secrets.token_hex(16)
                self._sessions[token] = user
                response = self._redirect_with_code(request, redirect, user)
                response.headers["set-cookie"] = f"MSISAuth={token}; Path=/adfs; Secure; HttpOnly"
                return response
            return _html(request, _ADFS_PAGE.format(query=escape(request.url.query.decode()), error=_ADFS_ERROR))
        return _html(request, _ADFS_PAGE.format(query=escape(request.url.query.decode()), error=""))

    def _redirect_with_code(self, request: httpx.Request, redirect: str, user: str) -> httpx.Response:
        code = secrets.token_hex(8)
        self._codes[code] = user
        return _redirect(request, f"{redirect}?code={code}")

    def _atrust(self, request: httpx.Request, cookies: dict[str, str]) -> httpx.Response:
        path = request.url.path
        session = cookies.get("sid", "")
        if path == "/passport/v1/auth/httpsOauth2":
            user = self._codes.pop(request.url.params.get("code", ""), None)
            if user is None:
                return _html(request, "<html><body>登录失败</body></html>", 400)
            sid = secrets.token_hex(16)
            self._gateway[sid] = (user, False)
            data = json.dumps({"env": {"need": True}, "ticket": secrets.token_hex(8)})
            query = urlencode({"source": "oauth2", "nextService": "auth/authCheck", "data": data})
            response = _redirect(request, f"{ATRUST}/portal/shortcut.html?{query}")
            response.headers["set-cookie"] = f"sid={sid}; Path=/; Secure; HttpOnly"
            return response
        if path == "/portal/shortcut.html":
            return _html(request, "<html><head><title>aTrust</title></head><body><div id=app></div></body></html>")
        if path == "/passport/v1/public/authConfig":
            return _json(request, {"code": 0, "data": {"security": {"csrfToken": f"csrf-{session}"}}})
        if path == "/controller/v1/public/reportEnv":
            return _json(request, {"code": 0})
        if path == "/passport/v1/auth/authCheck":
            if session in self._gateway and request.headers.get("x-csrf-token") == f"csrf-{session}":
                self._gateway[session] = (self._gateway[session][0], True)
                return _json(request, {"code": 0, "data": {}})
            return _json(request, {"code": 1, "message": "认证失败"})
        if path == "/controller/v1/public/verify":
            user, active = self._gateway.get(session, ("", False))
            if not active:
                return _redirect(request, f"{ATRUST}/portal/#!/login")
            ticket = secrets.token_hex(8)
            self._codes[ticket] = user
            return _redirect(request, f"{BOOKING}/?sdp_ticket={ticket}")
        return httpx.Response(404, request=request)

    def _booking(self, request: httpx.Request, cookies: dict[str, str]) -> httpx.Response:
        path = request.url.path
        if path == "/" and "sdp_ticket" in request.url.params:
            user = self._codes.pop(request.url.params["sdp_ticket"], None)
            if user is None:
                return _redirect(request, f"{ATRUST}/controller/v1/public/verify")
            app_session = secrets.token_hex(16)
            self._sessions[f"sdp:{app_session}"] = user
            response = _html(request, '<html><body><a href="/sso/login">Login</a></body></html>')
            response.headers["set-cookie"] = f"sdp_app_session={app_session}; Path=/; Secure; HttpOnly"
            return response
        if f"sdp:{cookies.get('sdp_app_session', '')}" not in self._sessions:
            return _redirect(request, f"{ATRUST}/controller/v1/public/verify")
        if path == "/":
            return _html(request, '<html><body><a href="/sso/login">Login</a></body></html>')
        if path == "/sso/login":
            query = urlencode({"client_id": "booking", "redirect_uri": f"{BOOKING}/sso/code", "response_type": "code"})
            return _redirect(request, f"https://sts.cuhk.edu.cn/adfs/oauth2/authorize?{query}")
        if path == "/sso/code":
            user = self._codes.pop(request.url.params.get("code", ""), None)
            if user is None:
                return _html(request, "登录失败", 400)
            session = secrets.token_hex(16)
            self._sessions[f"app:{session}"] = user
            response = _redirect(request, f"{BOOKING}/a/system/main")
            response.headers["set-cookie"] = f"JSESSIONID={session}; Path=/; Secure; HttpOnly"
            return response
        user = self._sessions.get(f"app:{cookies.get('JSESSIONID', '')}")
        if user is None:
            return _redirect(request, f"{BOOKING}/sso/login")
        account = self.accounts[user]
        form = dict(parse_qsl(request.content.decode())) if request.method == "POST" else {}
        if path == "/a/system/main":
            return _html(request, _HOME.format(name=escape(account.name), user_id=user))
        if path == "/a/field/cat/bizFieldType/listDataByUser":
            return self._types(request)
        if path == "/a/field/client/fieldTypeDetails":
            return _html(request, _DETAILS)
        if path == "/a/field/client/main":
            return _html(request, self._calendar(with_rule=form.get("bookType") == "0"))
        if path == "/a/field/client/bookForm":
            return self._form(request, account)
        if path == "/a/field/book/bizFieldBookMain/saveData":
            return self._save(request, user, form)
        if path == "/a/field/book/bizFieldBookMainMe/listData":
            return self._listing(request, user)
        return httpx.Response(404, request=request)

    # ---- 场地系统的页面 -------------------------------------------------------

    def _types(self, request: httpx.Request) -> httpx.Response:
        rows = [
            {"id": "1097", "nameZh": "羽毛球场", "nameEn": "Badminton Court", "bookFlag": 1},
            {"id": "1149", "nameZh": "音乐类", "nameEn": "Music Room", "bookFlag": 1},
            {"id": "1180", "nameZh": "音乐学院图书馆静音舱", "nameEn": "SilenCells", "bookFlag": 1},
            {"id": "2001", "nameZh": "教师琴房", "nameEn": "Teacher Practice Room", "bookFlag": 0},
            {"id": TYPE_ID, "nameZh": "学生琴房", "nameEn": "Student Practice Room", "bookFlag": 1},
        ] + [{"id": str(3000 + n), "nameZh": f"研讨室 {n}", "nameEn": "Seminar Room", "bookFlag": 1} for n in range(7)]
        page = int(request.url.params.get("pageNo", "1"))
        per = 8
        pages = (len(rows) + per - 1) // per
        meta = {"page": page, "pages": pages, "perpage": per, "total": len(rows)}
        return _json(request, {"data": rows[(page - 1) * per : page * per], "meta": meta})

    def _calendar(self, *, with_rule: bool) -> str:
        resources = "\n".join(
            _RESOURCE.format(id=room.id, name=room.name.replace("'", "\\'"), sort=n)
            for n, room in enumerate(self.rooms)
        )
        millis = int(self.now().timestamp() * 1000)
        return _CALENDAR.format(resources=resources, now=millis, rule=RULE_ID if with_rule else "", form_url=_FORM_URL)

    def _form(self, request: httpx.Request, account: Account) -> httpx.Response:
        params = dict(request.url.params)
        try:
            room = params["fId"]
            start = params["bizFieldBookField.startTime"]
            end = params["bizFieldBookField.endTime"]
            day = date.fromisoformat(start[:10])
            span = Span(parse_hhmm(start[11:]), parse_hhmm(end[11:]))
        except (KeyError, ValueError):
            return _html(request, "<html>系统错误</html>", 500)
        form_id, token = secrets.token_hex(16), secrets.token_hex(16)
        self._tokens[token] = _Token(account.student_id, form_id, room, day, span, self.now())
        fields = {
            "form_id": form_id,
            "user_id": account.student_id,
            "field_id": secrets.token_hex(16),
            "room": room,
            "token": token,
            "type_id": TYPE_ID,
            "name": escape(account.name),
            "start": start,
            "end": end,
        }
        return _html(request, _FORM.format(**fields))

    def _save(self, request: httpx.Request, user: str, form: dict[str, str]) -> httpx.Response:
        now = self.now()
        self.submissions.append((now, user, form.get("bizFieldBookField.startTime", "")))
        token = self._tokens.get(form.get("bookToken", ""))
        if token is None or token.student_id != user:
            reply = {"success": False, "message": "预订页面已失效，请刷新后重新提交!"}
        elif now - token.issued < timedelta(seconds=2):
            reply = {"success": False, "message": "操作过快，请稍后重试!"}
        elif now < open_at(token.day) + self.opens_late:
            reply = {"success": False, "message": "至多提前0天预订"}
        elif any(
            b.room == token.room and b.day == token.day and b.span.overlaps(token.span) and b.status == "2"
            for b in self.bookings
        ):
            reply = {"success": False, "message": "手速太慢，该时间段已经被预订啦"}
        elif any(
            b.student_id == user and b.day == token.day and b.span.overlaps(token.span) and b.status == "2"
            for b in self.bookings
        ):
            reply = {"success": False, "message": "该时间段您已有预订"}
        elif not form.get("theme") or not form.get("userPhone"):
            reply = {"success": False, "message": "请填写完整的预订信息"}
        else:
            del self._tokens[form["bookToken"]]
            self.bookings.append(Booked(token.form_id, user, token.room, token.day, token.span, now))
            reply = {"success": True, "message": "保存成功", "obj": {"id": token.form_id}}
        if self.lose_replies:
            self.lose_replies -= 1
            raise httpx.ReadTimeout("答复丢失", request=request)
        return _json(request, reply)

    def _listing(self, request: httpx.Request, user: str) -> httpx.Response:
        names = {room.id: room.name for room in self.rooms}
        rows = [
            {
                "id": b.id,
                "fId": secrets.token_hex(16),
                "nameZh": names.get(b.room, b.room),
                "startTime": f"{b.day:%Y-%m-%d} {hhmm(b.span.start)}",
                "endTime": f"{b.day:%Y-%m-%d} {hhmm(b.span.end)}",
                "status": b.status,
                "createDate": f"{b.created:%Y-%m-%d %H:%M:%S}",
            }
            for b in sorted(self.bookings, key=lambda b: b.created, reverse=True)
            if b.student_id == user
        ]
        form = dict(parse_qsl(request.content.decode()))
        page, per = int(form.get("pagination[page]", "1")), int(form.get("pagination[perpage]", "10"))
        meta = {"page": page, "pages": max(1, -(-len(rows) // per)), "perpage": per, "total": len(rows)}
        return _json(request, {"data": rows[(page - 1) * per : page * per], "meta": meta})


# ---- 工具 ---------------------------------------------------------------------


def _cookies(request: httpx.Request) -> dict[str, str]:
    header = request.headers.get("cookie", "")
    return dict(part.strip().split("=", 1) for part in header.split(";") if "=" in part)


def _redirect(request: httpx.Request, location: str) -> httpx.Response:
    return httpx.Response(302, headers={"location": location}, request=request)


def _html(request: httpx.Request, text: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, headers={"content-type": "text/html;charset=UTF-8"}, text=text, request=request)


def _json(request: httpx.Request, payload: object) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json;charset=UTF-8"},
        text=json.dumps(payload, ensure_ascii=False),
        request=request,
    )


_ADFS_ERROR = '<span id="errorText" for="">用户 ID 或密码不正确。请键入正确的用户 ID 和密码，然后重试。</span>'

_ADFS_PAGE = """<!DOCTYPE html><html><head><title>登录</title></head><body>
<form method="post" id="loginForm" autocomplete="off"
      action="/adfs/oauth2/authorize?{query}&amp;client-request-id=0">
<div id="error" class="fieldMargin error smallText">{error}</div>
<input id="userNameInput" name="UserName" type="email" value="" autocomplete="off">
<input id="passwordInput" name="Password" type="password" autocomplete="off">
<input id="kmsiInput" type="checkbox" name="Kmsi" value="true">
<input id="optionForms" type="hidden" name="AuthMethod" value="FormsAuthentication">
<span id="submitButton" class="submit" role="button">登录</span>
</form></body></html>"""

_HOME = """<html><body>
<div id="userId" class="kt-header__topbar-user" style="cursor: context-menu;">
    <i class="fa fa-user-circle userinfo colorWhite"></i>
    <span class="font12 colorWhite" style="margin-left: 5px;line-height: 20px;">
    {name}
    </span>
</div>
<script>
$.ajax({{ url: "/a/field/cat/bizFieldType/listDataByUser", data: {{
    "userId": "{user_id}",
    "deptId": $(".text_level_1_active").attr("id")
}} }});
</script></body></html>"""

_DETAILS = """<html><body>
<!-- 预订按钮 -->
<button class="btn btn-label-brand btn-bold" onclick="goBook('Student','0')">预订</button>
</body></html>"""

_RESOURCE = """                var resource = {{
                    id: '{id}',
                    fname: '{name}',
                    title: '{name}',
                    sort: {sort}
                }}
                resources.push(resource)"""

_CALENDAR = """<html><body><script>
{resources}
            nowIndicator:false,
            slotMinTime : '00:00',
            slotMaxTime : '23:30',
            if($("#selectDate").val() != dateFormat('yyyy-MM-dd',new Date({now}))){{ calendar.prev() }}
            var timeLimitLow = Number("0");
            var timeLimit = Number("2");
            $.post("/a/field/book/bizFieldBookMain/saveData?reBookMainId=&ruleId={rule}", JSON.parse(result));
            content: [{form_url}],
</script></body></html>"""

_FORM = """<html><body>
<form id="bizFieldBookMainForm" class="kt-form" action="/a/edu/rec/bizServFlowRec/saveData" method="post">
<input id="id" name="id" type="hidden" value="{form_id}"/>
<input id="user.id" name="user.id" type="hidden" value="{user_id}"/>
<input id="bizFieldBookField.id" name="bizFieldBookField.id" type="hidden" value="{field_id}"/>
<input id="bizFieldBookField.FId" name="bizFieldBookField.FId" type="hidden" value="{room}"/>
<input id="bizFieldBookField.BId" name="bizFieldBookField.BId" type="hidden" value="{form_id}"/>
<input id="bizFieldBookField.theme" name="bizFieldBookField.theme" type="hidden" value=""/>
<input type="text" name="submitTime" value="" style="display:none"/>
<input id="isNewRecord" name="isNewRecord" type="hidden" value="true"/>
<input type="hidden" name="bookToken" value="{token}"/>
<input id="extend1" name="extend1" type="hidden" value="{type_id}"/>
<input id="userTag" name="userTag" type="hidden" value="Student"/>
<input id="bookType" name="bookType" type="hidden" value="0"/>
<input id="user.name" name="user.name" readonly="readonly" type="text" value="{name}"/>
<input id="userPhone" name="userPhone" type="text" value=""/>
<input id="theme" name="theme" type="text" value=""/>
<input id="bizFieldBookField.startTime" name="bizFieldBookField.startTime" type="text" value="{start}"/>
<input id="bizFieldBookField.endTime" name="bizFieldBookField.endTime" type="text" value="{end}"/>
<input id="bizFieldBookField.joinNums" name="bizFieldBookField.joinNums" type="text" value=""/>
<select id="extend18" name="extend18">
<option value="0" selected>否</option><option value="1">是</option>
</select>
<input type="radio" name="extend16" value="1"/><input type="radio" name="extend16" value="0" checked/>
<textarea id="bizFieldBookField.useDesc" name="bizFieldBookField.useDesc"></textarea>
<button type="button" name="cancel">取消</button>
</form>
<input name="person_select_dialogname" value="outside"/>
</body></html>"""
