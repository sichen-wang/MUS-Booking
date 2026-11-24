"""学校页面的解析。这里只处理字符串，不发请求，所以可以直接用保存下来的页面样本测试。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote

from ..domain import CHINA, Room, parse_hhmm
from .errors import Unexpected

# ---- 表单 ---------------------------------------------------------------------

_UNSENT_TYPES = {"submit", "button", "image", "reset", "file"}


@dataclass
class Control:
    tag: str
    name: str
    type: str = ""
    value: str = ""
    checked: bool = False
    disabled: bool = False
    options: list[tuple[str, bool]] = field(default_factory=list)  # select 的 (取值, 是否选中)


@dataclass
class HtmlForm:
    id: str
    action: str
    method: str
    controls: list[Control] = field(default_factory=list)

    def pairs(self) -> list[tuple[str, str]]:
        """按浏览器提交表单的规则（等同 jQuery 的 serialize）取出要发送的键值，保持页面上的顺序。"""
        result = []
        for control in self.controls:
            if control.disabled or not control.name:
                continue
            if control.tag == "select":
                chosen = [value for value, selected in control.options if selected] or [
                    value for value, _ in control.options[:1]
                ]
                result += [(control.name, value) for value in chosen]
            elif control.tag == "textarea":
                result.append((control.name, control.value))
            elif control.type in _UNSENT_TYPES or (
                control.type in ("radio", "checkbox") and not control.checked
            ):
                continue
            else:
                result.append((control.name, control.value))
        return result

    def has(self, name: str) -> bool:
        return any(control.name == name for control in self.controls)

    @property
    def hidden_only(self) -> bool:
        named = [control for control in self.controls if control.name]
        return bool(named) and all(control.type == "hidden" for control in named)


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[HtmlForm] = []
        self._form: HtmlForm | None = None
        self._select: Control | None = None
        self._option: list[Any] | None = None  # [取值或 None, 是否选中, 文字]
        self._textarea: Control | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {key: (value if value is not None else "") for key, value in attrs}
        if tag == "form":
            self._form = HtmlForm(a.get("id", ""), a.get("action", ""), a.get("method", "get").lower())
            self.forms.append(self._form)
            return
        if self._form is None:
            return
        disabled = "disabled" in a
        if tag == "input":
            kind = a.get("type", "text").lower()
            value = a.get("value", "on" if kind in ("radio", "checkbox") else "")
            self._form.controls.append(
                Control("input", a.get("name", ""), kind, value, "checked" in a, disabled)
            )
        elif tag == "select":
            self._select = Control("select", a.get("name", ""), "select", disabled=disabled)
            self._form.controls.append(self._select)
        elif tag == "option" and self._select is not None:
            self._close_option()
            self._option = [a.get("value"), "selected" in a, ""]
        elif tag == "textarea":
            self._textarea = Control("textarea", a.get("name", ""), "textarea", disabled=disabled)
            self._form.controls.append(self._textarea)

    def handle_data(self, data: str) -> None:
        if self._textarea is not None:
            self._textarea.value += data
        elif self._option is not None:
            self._option[2] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "option":
            self._close_option()
        elif tag == "select":
            self._close_option()
            self._select = None
        elif tag == "textarea" and self._textarea is not None:
            self._textarea.value = self._textarea.value.removeprefix("\n")
            self._textarea = None
        elif tag == "form":
            self._close_option()
            self._form = None

    def _close_option(self) -> None:
        if self._option is not None and self._select is not None:
            value, selected, text = self._option
            self._select.options.append((value if value is not None else text.strip(), selected))
        self._option = None


def forms(html: str) -> list[HtmlForm]:
    parser = _FormParser()
    parser.feed(html)
    parser.close()
    return parser.forms


def form_by_id(html: str, form_id: str) -> HtmlForm | None:
    return next((form for form in forms(html) if form.id == form_id), None)


def password_form(html: str) -> HtmlForm | None:
    return next((form for form in forms(html) if form.has("Password")), None)


# ---- 登录页 -------------------------------------------------------------------

_EXTRA_AUTH = ("mfaGreeting", "verificationCodeInput", "authOptions", "azureMfa", "otpInput")


def login_error(html: str) -> str:
    """ADFS 登录页上的错误提示；没有提示时返回空字符串。"""
    for element_id in ("errorText", "errorTextPassword"):
        found = re.search(rf'id="{element_id}"[^>]*>(.*?)</', html, re.S)
        if found:
            text = re.sub(r"<[^>]+>|\s+", " ", found.group(1)).strip()
            if text:
                return text
    return ""


def asks_extra_auth(html: str) -> bool:
    return any(marker in html for marker in _EXTRA_AUTH)


# ---- 场地系统 -----------------------------------------------------------------

_USER_ID = re.compile(r'"userId"\s*:\s*"([^"]+)"')
_USER_NAME = re.compile(r'(?s)id="userId"[^>]*>.*?<span[^>]*>\s*(.*?)\s*</span>')
_ENTRY = re.compile(r"""onclick="goBook\('(\w*)','(\w*)'\)\"""")
_ROOM = re.compile(r"id:\s*'([0-9A-Za-z]+)',\s*fname:\s*'([^']*)'")
_RULE = re.compile(r"saveData\?reBookMainId=[^&\"']*&ruleId=([0-9A-Za-z]+)")
_LIMIT = re.compile(r'var\s+timeLimit\s*=\s*Number\("(\d+(?:\.\d+)?)"\)')
_SLOTS = re.compile(r"\b(slotMinTime|slotMaxTime)\s*:\s*'(\d{1,2}:\d{2})'")
_NOW = re.compile(r"new Date\((\d{13})\)")
_FORM_URL = re.compile(r"(?s)('/a/field/client/bookForm\?.+?)\]")


@dataclass(frozen=True)
class Home:
    user_id: str
    name: str


def home(html: str) -> Home:
    user_id = _USER_ID.search(html)
    if not user_id:
        raise Unexpected("学校首页上没有找到用户编号")
    name = _USER_NAME.search(html)
    return Home(user_id.group(1), re.sub(r"\s+", " ", name.group(1)).strip() if name else "")


def entry(html: str) -> tuple[str, str]:
    """详情页上预定按钮给出的（身份标签, 预定类型），进入日历页时要原样带上。"""
    found = _ENTRY.search(html)
    if not found:
        raise Unexpected("琴房详情页上没有预定按钮")
    return found.group(1), found.group(2)


@dataclass(frozen=True)
class Calendar:
    rooms: tuple[Room, ...]
    rule_id: str
    time_limit: int
    day_start: int
    day_end: int
    form_url: str
    server_time: datetime | None


def calendar(html: str) -> Calendar:
    rooms = tuple(Room(room_id, name) for room_id, name in dict.fromkeys(_ROOM.findall(html)))
    rule = _RULE.search(html)
    form_url = _FORM_URL.search(html)
    if not rooms or not rule or not form_url:
        missing = [
            label for label, ok in (("琴房", rooms), ("规则编号", rule), ("表单地址", form_url)) if not ok
        ]
        raise Unexpected(f"日历页上没有找到{'、'.join(missing)}")
    limit = _LIMIT.search(html)
    slots = dict(_SLOTS.findall(html))
    now = _NOW.search(html)
    return Calendar(
        rooms=rooms,
        rule_id=rule.group(1),
        time_limit=round(float(limit.group(1)) * 60) if limit else 120,
        day_start=parse_hhmm(slots.get("slotMinTime", "00:00")),
        day_end=parse_hhmm(slots.get("slotMaxTime", "23:30")),
        form_url=form_url.group(1),
        server_time=datetime.fromtimestamp(int(now.group(1)) / 1000, CHINA) if now else None,
    )


def render(template: str, values: dict[str, str]) -> str:
    """代入页面脚本里 `'?a=' + x + '&b=1'` 这样的拼接表达式。认不出的变量说明页面变了，直接报错。"""
    out = []
    for part in re.split(r"\s*\+\s*", template.strip()):
        if part[:1] in ("'", '"'):
            out.append(part[1:-1])
        elif part in values:
            out.append(quote(values[part]))
        elif part:
            raise Unexpected(f"预定表单的地址里有不认识的变量 {part}")
    return "".join(out)


def json_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None
