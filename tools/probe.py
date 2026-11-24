#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.27"]
# ///
"""探测学校预订系统：纯 HTTP 登录，然后只读地取回预定所需的全部信息。

用法（在将要部署的机器上运行）：

    uv run tools/probe.py --anonymous   # 只测连通性，不需要账号
    uv run tools/probe.py               # 完整探测；没有可用会话时提示输入学号与密码
    uv run tools/probe.py --no-prompt   # 只用保存的会话，绝不提示输入密码

没有 uv 时：pip install httpx 后用 python 直接运行。

密码只会提交给 sts.cuhk.edu.cn，不落盘、不打印。全程只读：不提交、不修改、
不取消任何预定。产出三样东西，都只留在本机（已被 git 忽略）：

- probe-report.txt：主机、路径、状态码、字段名，以及系统级的配置（场地类型、
  规则、提示语）。不含凭据、令牌或个人信息，可以直接转交。
- probe-pages/：抓到的页面和接口原文，用来分析表单结构。含该账号的个人信息。
- probe-session.json：登录会话（Cookie）。有了它，之后的运行不必再输密码；
  它和密码一样敏感，不用时删掉即可。传 --session "" 可以不保存。
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import platform
import re
import secrets
import ssl
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import httpx

BOOKING = "https://booking.cuhk.edu.cn/"
BOOKING_HOST = "booking.cuhk.edu.cn"
BOOKING_LOGIN = "https://booking.cuhk.edu.cn/sso/login"  # 落地页上 Login 按钮的地址
BOOKING_HOME = "https://booking.cuhk.edu.cn/a/system/main"  # 登录成功后落到的主页
ADFS_HOST = "sts.cuhk.edu.cn"
ATRUST = "https://atrust.cuhk.edu.cn"
# aTrust 门户用 JS 跳到这个地址；其中 client_id 是公开的客户端标识，不是密钥。
ADFS_AUTHORIZE = (
    "https://sts.cuhk.edu.cn/adfs/oauth2/authorize"
    "?resource=https%3A%2F%2Fatrust.cuhk.edu.cn"
    "&client_id=e6d8a534-4b6f-4021-bb44-51ee9d0b63fd"
    "&redirect_uri=https%3A%2F%2Fatrust.cuhk.edu.cn%2Fpassport%2Fv1%2Fauth%2FhttpsOauth2"
    "&response_type=code"
)
ADFS_DOMAIN = "cuhksz"  # 登录页脚本会给不含 @ 或 \ 的用户名补上这个域前缀
# aTrust 门户脚本给每个接口请求附带的公共参数
ATRUST_COMMON = {"clientType": "SDPBrowserClient", "platform": "Windows", "lang": "zh-CN"}
ALLOWED_SUFFIX = ".cuhk.edu.cn"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
REDIRECTS = {301, 302, 303, 307, 308}
MAX_HOPS = 20
MFA_MARKERS = ("mfaGreeting", "verificationCodeInput", "authOptions", "azureMfa", "otpInput")
# 只有这些键的取值会原样写进报告（都是流程控制字段），其余只报告类型和长度
SAFE_KEYS = {
    "code", "message", "nextService", "type", "subtype", "subType", "authType", "source", "dest",
    "secondAuthType", "need", "isLogin", "autoRedirect", "enableStartClient", "status", "success",
}

CHINA = timezone(timedelta(hours=8))  # 中国标准时间没有夏令时，固定偏移即可
# 预订系统里会改动数据的接口，探测脚本一律不碰
UNSAFE_PATH = re.compile(
    r"log.?out|sign.?(?:out|in)|exit|delete|remove|cancel|save|update|submit|confirm|approv|apply|add|create"
    r"|insert|send|reset|clear|disable|enable|pay|del[A-Z/]",
    re.I,
)
# 这几个 POST 在网站里只是“打开页面”或“查询列表”，不改动数据
READ_ONLY_POSTS = {
    "/a/field/client/fieldTypeDetails",
    "/a/field/client/main",
    "/a/field/book/bizFieldBookMainMe/listData",
}
TYPE_HINT = re.compile(r"琴房|piano|practice", re.I)  # 仅用于探测时挑出要细看的场地类型
TYPE_LIMIT = 3
ROOM = re.compile(r"id:\s*'([0-9a-zA-Z]+)',\s*fname:\s*'([^']*)'")
RULE = re.compile(r"saveData\?reBookMainId=[^&\"']*&ruleId=([0-9a-zA-Z]*)")
PAGE_VAR = re.compile(r"var\s+(fbookFre\w*|timeLimit\w*|\w*[oO]penTime\w*|\w*[aA]dvance\w*|minTime|maxTime)\s*=\s*([^;\n]{0,60})")
PERSON_TAG = re.compile(r'name\s*=\s*"personTag";\s*\w+\.value\s*=\s*"(\w*)"')


class ProbeError(Exception):
    """探测无法继续；消息本身就是给人看的结论。"""


@dataclass
class Form:
    id: str
    method: str
    action: str
    fields: dict[str, tuple[str, str]] = field(default_factory=dict)  # name -> (type, value)

    @property
    def is_hidden_only(self) -> bool:
        return bool(self.fields) and all(kind == "hidden" for kind, _ in self.fields.values())

    def describe_fields(self) -> str:
        """字段名(类型)，带 * 表示服务器已经预填了值。"""
        return ", ".join(f"{name}({kind}){'*' if value else ''}" for name, (kind, value) in self.fields.items())


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[Form] = []
        self._open: Form | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form":
            self._open = Form(a.get("id", ""), a.get("method", "get").lower(), a.get("action", ""))
            self.forms.append(self._open)
        elif tag in ("input", "select", "textarea") and self._open is not None and a.get("name"):
            kind = a.get("type", "text").lower() if tag == "input" else tag
            self._open.fields[a["name"]] = (kind, a.get("value", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._open = None


def parse_forms(html: str) -> list[Form]:
    parser = _FormParser()
    parser.feed(html)
    return parser.forms


def has_password_form(html: str) -> bool:
    return any("Password" in form.fields for form in parse_forms(html))


def text_by_id(html: str, element_id: str) -> str:
    match = re.search(rf'id="{re.escape(element_id)}"[^>]*>(.*?)</', html, re.S)
    return re.sub(r"<[^>]+>|\s+", " ", match.group(1)).strip() if match else ""


def first_group(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1) if match else ""


def shape(value: Any, key: str = "", depth: int = 0) -> str:
    """把 JSON 压成“结构摘要”：保留键名和流程字段，其余取值一律抹掉。"""
    if isinstance(value, dict):
        if depth >= 3:
            return "{…}"
        return "{" + ", ".join(f"{k}: {shape(v, k, depth + 1)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return f"[{len(value)}× {shape(value[0], key, depth + 1)}]" if value else "[]"
    if isinstance(value, bool) or value is None or key in SAFE_KEYS:
        return repr(value)
    if isinstance(value, (int, float)):
        return type(value).__name__
    return f"str({len(str(value))})"


def loads_or_empty(text: str | None) -> Any:
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {}


def kind_of(response: httpx.Response) -> str:
    return response.headers.get("content-type", "?").split(";")[0].strip()


def decoded(response: httpx.Response) -> str:
    """预订系统的页面并不都是 UTF-8：先按 UTF-8 严格解码，不行再按 GB18030。"""
    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError:
        return response.content.decode("gb18030", errors="replace")


def describe(response: httpx.Response) -> str:
    body = decoded(response) if "html" in kind_of(response) else ""
    title = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    text = f"{response.status_code} {response.headers.get('content-type', '?')} {len(response.content)}B"
    if title and title.group(1).strip():
        text += f" 标题「{title.group(1).strip()[:40]}」"
    return text


def render_url(url: str) -> str:
    """主机 + 路径 + 参数名；参数值不进报告。"""
    parts = urlsplit(url)
    names = ",".join(name for name, _ in parse_qsl(parts.query, keep_blank_values=True))
    return f"{parts.netloc}{parts.path}" + (f"?{{{names}}}" if names else "")


@dataclass
class Hop:
    method: str
    url: str
    status: int
    elapsed_ms: float
    server: str
    skew_s: float | None
    cookies_set: list[str]
    note: str = ""

    def render(self, index: int) -> str:
        line = f"{index:>3}  {self.status} {self.method:<4} {render_url(self.url)}  {self.elapsed_ms:.0f}ms"
        if self.server:
            line += f"  server={self.server}"
        if self.skew_s is not None:
            line += f"  clock{self.skew_s:+.1f}s"
        if self.cookies_set:
            line += f"  set-cookie: {', '.join(self.cookies_set)}"
        if self.note:
            line += f"  [{self.note}]"
        return line


class Probe:
    def __init__(self, pages_dir: str = "", session_path: str = "",
                 ask: Callable[[], tuple[str, str]] | None = None) -> None:
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            follow_redirects=False,
            timeout=30,
        )
        self.pages_dir = pages_dir
        self.session_path = session_path
        self.lines: list[str] = []
        self.hop_count = 0
        self.page_count = 0
        self.csrf = ""
        self._ask = ask
        self._credentials: tuple[str, str] | None = None

    def say(self, text: str = "") -> None:
        print(text)
        self.lines.append(text)

    def section(self, title: str) -> None:
        self.say()
        self.say(f"== {title}")

    def keep(self, response: httpx.Response) -> None:
        """把响应原样（字节）存到本机，供之后分析。"""
        if not self.pages_dir:
            return
        os.makedirs(self.pages_dir, exist_ok=True)
        kind = kind_of(response)
        ext = next((e for e, mark in (("html", "html"), ("json", "json"), ("js", "javascript")) if mark in kind), "txt")
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", urlsplit(str(response.url)).path.strip("/"))[:80] or "root"
        self.page_count += 1
        name = f"{self.page_count:02d}_{stem}" + ("" if stem.endswith(f".{ext}") else f".{ext}")
        with open(os.path.join(self.pages_dir, name), "wb") as fh:
            fh.write(response.content)
        self.say(f"     ↳ 原文存为 {name}")

    # ---- 会话 ---------------------------------------------------------------

    def load_session(self) -> int:
        if not self.session_path or not os.path.exists(self.session_path):
            return 0
        with open(self.session_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        for cookie in saved:
            self.client.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
        return len(saved)

    def save_session(self) -> None:
        if not self.session_path:
            return
        saved = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                 for c in self.client.cookies.jar if c.value is not None]
        with open(self.session_path, "w", encoding="utf-8") as fh:
            json.dump(saved, fh)
        self.say(f"   会话已保存到 {self.session_path}（{len(saved)} 个 Cookie）")

    # ---- 请求 ---------------------------------------------------------------

    def request(self, method: str, url: str, *, data: dict[str, str] | None = None, json_body: Any = None,
                headers: dict[str, str] | None = None, note: str = "") -> httpx.Response:
        parts = urlsplit(url)
        if parts.scheme != "https" or not (parts.hostname or "").endswith(ALLOWED_SUFFIX):
            raise ProbeError(f"链路跳出了学校域名（{parts.netloc}），已停止")
        sent = time.time()
        try:
            response = self.client.request(method, url, data=data, json=json_body, headers=headers)
        except httpx.HTTPError as exc:
            raise ProbeError(f"请求 {parts.netloc}{parts.path} 失败：{type(exc).__name__}: {exc}") from exc
        received = time.time()
        skew = None
        if "date" in response.headers:
            try:
                server_time = parsedate_to_datetime(response.headers["date"]).timestamp()
                skew = server_time - (sent + received) / 2
            except (TypeError, ValueError):
                pass
        cookies = [raw.split("=", 1)[0] for raw in response.headers.get_list("set-cookie")]
        self.hop_count += 1
        hop = Hop(method, url, response.status_code, (received - sent) * 1000,
                  response.headers.get("server", ""), skew, cookies, note)
        self.say(hop.render(self.hop_count))
        return response

    def walk(self, method: str, url: str, data: dict[str, str] | None = None) -> httpx.Response:
        """像浏览器一样跟随跳转：HTTP 重定向、meta refresh、自动提交的隐藏表单、字面量 JS 跳转。"""
        note = ""
        for _ in range(MAX_HOPS):
            response = self.request(method, url, data=data, note=note)
            step = self._next_step(response)
            if step is None:
                return response
            method, url, data, note = step
        raise ProbeError("跳转次数过多，疑似循环")

    def _next_step(self, response: httpx.Response) -> tuple[str, str, dict[str, str] | None, str] | None:
        base = str(response.url)
        if response.status_code in REDIRECTS and "location" in response.headers:
            target = urljoin(base, response.headers["location"])
            if response.status_code in (307, 308):
                return response.request.method, target, dict(parse_qsl(response.request.content.decode())), ""
            return "GET", target, None, ""
        if response.status_code != 200 or "html" not in kind_of(response):
            return None
        html = response.text
        refresh = re.search(r'http-equiv=["\']?refresh["\']?[^>]*content=["\']?\d+;\s*url=([^"\'>\s]+)', html, re.I)
        if refresh:
            return "GET", urljoin(base, refresh.group(1)), None, "meta refresh"
        forms = parse_forms(html)
        if len(forms) == 1 and forms[0].is_hidden_only and ".submit()" in html:
            form = forms[0]
            values = {name: value for name, (_, value) in form.fields.items()}
            return form.method.upper(), urljoin(base, form.action), values, "自动提交表单"
        if not forms and len(html) < 3000:
            jump = re.search(r'location(?:\.href)?\s*=\s*["\']([^"\']+)["\']|location\.replace\(\s*["\']([^"\']+)', html)
            if jump:
                return "GET", urljoin(base, jump.group(1) or jump.group(2)), None, "JS 跳转"
        return None

    def atrust(self, method: str, path: str, *, json_body: Any = None, data: dict[str, str] | None = None,
               params: dict[str, str] | None = None) -> dict[str, Any]:
        """按 aTrust 门户脚本的方式调用它的接口：公共参数 + CSRF 头 + 追踪号。"""
        url = f"{ATRUST}{path}?{urlencode({**ATRUST_COMMON, **(params or {})})}"
        headers = {
            "x-csrf-token": self.csrf,
            "x-sdp-traceid": secrets.token_hex(4),
            "x-sdp-rid": base64.b64encode(urlsplit(ATRUST).netloc.encode()).decode(),
        }
        response = self.request(method, url, data=data, json_body=json_body, headers=headers)
        result = loads_or_empty(response.text)
        if not isinstance(result, dict):
            result = {}
        self.say(f"     ↳ {shape(result) if result else describe(response)}")
        return result

    def booking(self, method: str, path: str, *, params: dict[str, str] | None = None,
                data: dict[str, str] | None = None) -> httpx.Response:
        """只读地调用预订系统的一个接口，并把原文存下来。会改动数据的接口在这里被拦住。"""
        if UNSAFE_PATH.search(path) or (method != "GET" and path not in READ_ONLY_POSTS):
            raise ProbeError(f"探测脚本拒绝调用可能改动数据的接口：{method} {path}")
        url = urljoin(BOOKING, path) + (f"?{urlencode(params)}" if params else "")
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": BOOKING_HOME}
        response = self.request(method, url, data=data, headers=headers)
        self.keep(response)
        return response

    # ---- 登录 ---------------------------------------------------------------

    def environment(self) -> None:
        self.section("环境")
        now = datetime.now(timezone.utc)
        self.say(f"python {platform.python_version()} · {ssl.OPENSSL_VERSION} · httpx {httpx.__version__}")
        self.say(f"system {platform.system()} {platform.release()} · utc {now:%Y-%m-%d %H:%M:%S} · tz {time.strftime('%Z')}")
        proxies = sorted(k for k in os.environ if k.lower() in ("http_proxy", "https_proxy", "all_proxy"))
        self.say(f"proxy env: {', '.join(proxies) or '无'}")
        loaded = self.load_session()
        self.say(f"保存的会话：{f'{loaded} 个 Cookie' if loaded else '无'}")

    def reach(self) -> None:
        self.section("1. 访问预订系统")
        final = self.walk("GET", BOOKING)
        self.say(f"   停在 {final.url.host}{final.url.path}（{describe(final)}）")

    def sign_in(self) -> httpx.Response | None:
        """走完整条登录链路，返回预订系统的主页；任何一环没通过就返回 None。"""
        self.section("2. 学校统一登录（ADFS）")
        landing = self.walk("GET", ADFS_AUTHORIZE)
        if landing.url.host == ADFS_HOST:
            landing = self._submit_adfs(landing)
        else:
            self.say("   已有的登录仍然有效，没有提交密码")
        self.say(f"   停在 {landing.url.host}{landing.url.path}（{describe(landing)}）")
        self.finish_atrust(landing)
        return self.enter_booking()

    def _submit_adfs(self, page: httpx.Response) -> httpx.Response:
        """把凭据提交给 ADFS 登录表单；提交地址不是 sts 就拒绝发出密码。"""
        form = next((f for f in parse_forms(page.text) if "Password" in f.fields), None)
        if form is None:
            raise ProbeError(f"登录页里没有密码表单（{describe(page)}）")
        action = urljoin(str(page.url), form.action)
        if urlsplit(action).scheme != "https" or urlsplit(action).hostname != ADFS_HOST:
            raise ProbeError("登录表单的提交地址不是 sts.cuhk.edu.cn，已中止，密码没有发出")
        if self._credentials is None:
            if self._ask is None:
                raise ProbeError("需要输入密码，但这次运行不允许提示输入（--no-prompt）")
            self._credentials = self._ask()
        user, password = self._credentials
        payload = {name: value for name, (_, value) in form.fields.items()}
        payload["UserName"] = user if re.search(r"[@\\]", user) else f"{ADFS_DOMAIN}\\{user}"
        payload["Password"] = password
        payload["Kmsi"] = "true"
        final = self.walk("POST", action, payload)
        if final.url.host == ADFS_HOST:
            markers = [m for m in MFA_MARKERS if m in final.text]
            if markers:
                raise ProbeError(f"ADFS 要求额外验证（页面含 {', '.join(markers)}），纯 HTTP 登录不可行")
            reason = text_by_id(final.text, "errorText") or text_by_id(final.text, "errorTextPassword")
            raise ProbeError(f"ADFS 没有放行：{reason or describe(final)}")
        return final

    def finish_atrust(self, landing: httpx.Response) -> None:
        """代替门户页的 JS 走完登录收尾：环境上报、authCheck、accessCheck。"""
        self.section("3. aTrust 登录收尾（代替门户页脚本）")
        flow = dict(parse_qsl(urlsplit(str(landing.url)).query, keep_blank_values=True))
        extra = loads_or_empty(flow.get("data"))
        self.say(f"   落地参数 source={flow.get('source')!r} dest={flow.get('dest')!r} nextService={flow.get('nextService')!r}")

        config = self.atrust("GET", "/passport/v1/public/authConfig", params={"mod": "1", "needTicket": "1"})
        self.csrf = ((config.get("data") or {}).get("security") or {}).get("csrfToken", "")

        env = extra.get("env") if isinstance(extra, dict) else None
        if isinstance(env, dict) and env.get("need") and extra.get("ticket"):
            device_id = secrets.token_hex(32)
            self.atrust("POST", "/controller/v1/public/reportEnv", json_body={
                "ticket": extra["ticket"], "deviceId": device_id,
                "env": {"endpoint": {"device_id": device_id, "device": {"type": "browser"}}},
            })

        service = flow.get("nextService") or ""
        for _ in range(4):
            if service == "auth/authCheck":
                result = self.atrust("GET", "/passport/v1/auth/authCheck")
            elif service == "auth/accessCheck":
                result = self.atrust("POST", "/passport/v1/auth/accessCheck", data={})
            elif service:
                raise ProbeError(f"aTrust 要求额外的认证步骤 {service!r}，需要人工参与")
            else:
                break
            if result.get("code") not in (0, "0", None):
                raise ProbeError(f"aTrust {service} 没有通过：code={result.get('code')!r} message={result.get('message')!r}")
            service = (result.get("data") or {}).get("nextService") or ""

    def enter_booking(self) -> httpx.Response | None:
        self.section("4. 穿过网关，登录预订系统")
        landing = self.walk("GET", BOOKING)
        if landing.url.host != BOOKING_HOST:
            self.say(f"   没有通过网关，停在 {landing.url.host}{landing.url.path}")
            return None
        home = self.request("GET", BOOKING_HOME)
        if home.status_code != 200:
            # 落地页上的 Login 按钮：/sso/login → ADFS（已有凭据时自动放行）→ /sso/code
            home = self.walk("GET", BOOKING_LOGIN)
            if home.url.host == ADFS_HOST and has_password_form(home.text):
                home = self._submit_adfs(home)
        ok = home.url.host == BOOKING_HOST and home.status_code == 200 and home.url.path.startswith("/a/")
        self.say(f"   停在 {home.url.host}{home.url.path}（{describe(home)}）")
        return home if ok else None

    # ---- 只读探查 -----------------------------------------------------------

    def discover(self, home: httpx.Response) -> None:
        """按网站自己的顺序，把预定一间琴房要用到的信息都只读地取一遍。"""
        self.section("5. 这个账号能预定哪些场地类型")
        self.keep(home)
        user_id = first_group(re.compile(r'"userId":\s*"([^"]+)"'), decoded(home))
        self.say(f"   主页里的 userId：{'已找到' if user_id else '没有找到'}")
        types: list[dict[str, Any]] = []
        for page_no in range(1, 6):
            listing = self.booking("GET", "/a/field/cat/bizFieldType/listDataByUser", params={
                "userId": user_id, "deptId": "", "fcCode": "", "area": "", "position": "", "search": "",
                "pageNo": str(page_no),
            })
            payload = loads_or_empty(decoded(listing))
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or not rows:
                self.say(f"     ↳ {shape(payload) if payload else describe(listing)}")
                break
            if page_no == 1:
                self.say(f"     ↳ 每项的结构：{shape(rows[0])}")
            types.extend(row for row in rows if isinstance(row, dict))
            if page_no >= int((payload.get("meta") or {}).get("pages") or 1):
                break
        for row in types:
            self.say(f"   {row.get('id')} · {row.get('nameZh')} · {row.get('deptName')} · bookFlag={row.get('bookFlag')} extFlag={row.get('extFlag')}")

        chosen = [row for row in types if TYPE_HINT.search(f"{row.get('nameZh')} {row.get('nameEn')}")][:TYPE_LIMIT]
        if not chosen:
            self.say("   没有名字像琴房的场地类型，后面的步骤跳过")
        for row in chosen:
            self._discover_type(row)

        self.section("7. 我的预定（只读列表）")
        mine = self.booking("POST", "/a/field/book/bizFieldBookMainMe/listData",
                            data={"pagination[page]": "1", "pagination[perpage]": "10"})
        self.say(f"     ↳ {shape(loads_or_empty(decoded(mine))) or describe(mine)}")

    def _discover_type(self, row: dict[str, Any]) -> None:
        type_id = str(row.get("id"))
        self.section(f"6. 场地类型「{row.get('nameZh')}」")
        details = self.booking("POST", "/a/field/client/fieldTypeDetails",
                               data={"id": type_id, "bookFlag": str(row.get("bookFlag") or "")})
        self.say(f"     ↳ {describe(details)}")
        tag = first_group(PERSON_TAG, decoded(details)) or "Student"
        calendar = self.booking("POST", "/a/field/client/main",
                                data={"id": type_id, "bookType": "", "personTag": tag})
        text = decoded(calendar)
        rooms = list(dict.fromkeys(ROOM.findall(text)))
        rule = first_group(RULE, text)
        self.say(f"     ↳ {describe(calendar)}")
        self.say(f"   personTag={tag} · ruleId={f'{len(rule)} 位' if rule else '没有找到'} · 琴房 {len(rooms)} 间")
        self.say(f"   琴房：{'、'.join(name for _, name in rooms[:80])}")
        for name, value in dict.fromkeys(PAGE_VAR.findall(text)):
            self.say(f"   页面变量 {name} = {value.strip()}")
        for found in parse_forms(text):
            self.say(f"   表单 #{found.id or '?'} {found.method} → {urlsplit(found.action).path}: {found.describe_fields()}")

        opening = self.booking("GET", "/a/field/cat/bizFieldTypeOpentime/findOpenBookList", params={"fId": type_id})
        self.say(f"     ↳ 开放规则原文：{decoded(opening)[:900]}")

        now = datetime.now(CHINA)
        tomorrow = now + timedelta(days=1)
        after = now + timedelta(days=2)
        events = self.booking("GET", "/a/field/book/bizFieldBookField/eventsV1", params={
            "startTime": f"{tomorrow:%Y-%m-%d}", "endTime": f"{after:%Y-%m-%d}",
            "ftId": type_id, "reBookMainId": "", "jsonStr": "[]", "fitUseStr": "",
        })
        self.say(f"     ↳ 日历数据结构：{shape(loads_or_empty(decoded(events))) or describe(events)}")

        if not rooms:
            return
        slots = {
            "明天": (f"{tomorrow:%Y-%m-%d} 10:00", f"{tomorrow:%Y-%m-%d} 11:00"),
            "后天": (f"{after:%Y-%m-%d} 10:00", f"{after:%Y-%m-%d} 11:00"),
        }
        if now.hour < 21:
            later = (now + timedelta(hours=1)).replace(minute=0)
            slots = {"今天": (f"{later:%Y-%m-%d %H:%M}", f"{later + timedelta(hours=1):%Y-%m-%d %H:%M}"), **slots}
        for label, (start, end) in slots.items():
            query = {
                "fId": rooms[0][0], "bizFieldBookField.startTime": start, "bizFieldBookField.endTime": end,
                "repFlag": "1", "bookType": "", "userTag": tag, "approvalFlag": "0", "extend2": "",
                "bookedNum": "0", "fitBook": "false", "isDeptAdmin": "false", "adMost": "",
            }
            check = self.booking("GET", "/a/field/book/bizFieldBookMain/validateBlack", params=query)
            self.say(f"     ↳ {label} {start[11:]}–{end[11:]} 预检：{decoded(check)[:300]}")
            form = self.booking("GET", "/a/field/client/bookForm", params=query)
            self.say(f"     ↳ {label} 的预定表单：{describe(form)}")
            for found in parse_forms(decoded(form)):
                self.say(f"       表单 #{found.id or '?'} {found.method} → {urlsplit(found.action).path}: {found.describe_fields()}")

    def cookies(self) -> None:
        self.section("会话 Cookie（只列属性，不含取值）")
        now = time.time()
        for cookie in sorted(self.client.cookies.jar, key=lambda c: (c.domain, c.name)):
            life = "会话级" if cookie.expires is None else f"{(cookie.expires - now) / 3600:.1f}h"
            self.say(f"   {cookie.domain:<24} {cookie.name:<28} path={cookie.path:<10} 有效期={life}")


def ask_credentials() -> tuple[str, str]:
    user = input("\n学号：").strip()
    password = getpass.getpass("密码（不回显，只发往 sts.cuhk.edu.cn）：")
    return user, password


def main() -> int:
    parser = argparse.ArgumentParser(description="探测学校预订系统（只读）")
    parser.add_argument("--anonymous", action="store_true", help="只测连通性，不登录")
    parser.add_argument("--no-prompt", action="store_true", help="只用保存的会话，绝不提示输入密码")
    parser.add_argument("--report", default="probe-report.txt", help="报告输出路径")
    parser.add_argument("--pages", default="probe-pages", help="原文的保存目录；传空字符串则不保存")
    parser.add_argument("--session", default="probe-session.json", help="会话文件；传空字符串则不读取也不保存")
    args = parser.parse_args()

    probe = Probe(pages_dir=args.pages, session_path="" if args.anonymous else args.session,
                  ask=None if args.no_prompt else ask_credentials)
    verdict = ""
    try:
        probe.environment()
        probe.reach()
        if args.anonymous:
            page = probe.walk("GET", ADFS_AUTHORIZE)
            probe.atrust("GET", "/passport/v1/public/authConfig", params={"mod": "1"})
            verdict = ("匿名连通正常，ADFS 登录页可达" if has_password_form(page.text)
                       else "ADFS 登录页异常，没有找到密码表单")
        else:
            home = probe.sign_in()
            if home is None:
                verdict = "没能登录预订系统，卡住的位置见上面最后几跳"
            else:
                probe.save_session()
                probe.discover(home)
                verdict = "登录和只读探查都已完成"
    except ProbeError as exc:
        verdict = f"中止：{exc}"
    except KeyboardInterrupt:
        verdict = "用户取消"
    finally:
        probe.cookies()
        probe.section("结论")
        probe.say(verdict)
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write("\n".join(probe.lines) + "\n")
        print(f"\n报告已写入 {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
