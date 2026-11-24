#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.27"]
# ///
"""探测登录链路：不借助浏览器，纯 HTTP 能否走通 aTrust → ADFS → 预订系统。

用法（在将要部署的机器上运行）：

    uv run tools/probe.py --anonymous   # 只测连通性，不需要账号
    uv run tools/probe.py               # 完整探测，会提示输入学号与密码

没有 uv 时：pip install httpx 后用 python 直接运行。

密码只会提交给 sts.cuhk.edu.cn，不落盘、不打印。报告（probe-report.txt）只含
主机、路径、状态码、耗时、Cookie 名、字段名和少量流程控制字段，不含凭据、令牌
或个人信息，可以直接转交。
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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import httpx

BOOKING = "https://booking.cuhk.edu.cn/"
BOOKING_HOME = "https://booking.cuhk.edu.cn/a/field/client/main"
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
    "secondAuthType", "need", "isLogin", "autoRedirect", "enableStartClient", "status",
}
# 已登录后只读探查时，绝不访问带这些字样的地址
UNSAFE_PATH = re.compile(
    r"logout|exit|delete|remove|cancel|save|update|submit|confirm|approve|apply|add|create|insert"
    r"|send|reset|clear|disable|enable|pay|del[A-Z/]",
    re.I,
)
EXPLORE_PATH = re.compile(r"form|list|main|index|book|rule|select|tree", re.I)
EXPLORE_LIMIT = 12


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


def text_by_id(html: str, element_id: str) -> str:
    match = re.search(rf'id="{re.escape(element_id)}"[^>]*>(.*?)</', html, re.S)
    return re.sub(r"<[^>]+>|\s+", " ", match.group(1)).strip() if match else ""


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
        parts = urlsplit(self.url)
        query = ",".join(name for name, _ in parse_qsl(parts.query, keep_blank_values=True))
        line = f"{index:>3}  {self.status} {self.method:<4} {parts.netloc}{parts.path}"
        if query:
            line += f"?{{{query}}}"
        line += f"  {self.elapsed_ms:.0f}ms"
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
    def __init__(self) -> None:
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            follow_redirects=False,
            timeout=20,
        )
        self.lines: list[str] = []
        self.hop_count = 0
        self.csrf = ""

    def say(self, text: str = "") -> None:
        print(text)
        self.lines.append(text)

    def section(self, title: str) -> None:
        self.say()
        self.say(f"== {title}")

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
        if response.status_code != 200 or "html" not in response.headers.get("content-type", ""):
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

    # ---- 各阶段 -------------------------------------------------------------

    def environment(self) -> None:
        self.section("环境")
        now = datetime.now(timezone.utc)
        self.say(f"python {platform.python_version()} · {ssl.OPENSSL_VERSION} · httpx {httpx.__version__}")
        self.say(f"system {platform.system()} {platform.release()} · utc {now:%Y-%m-%d %H:%M:%S} · tz {time.strftime('%Z')}")
        proxies = sorted(k for k in os.environ if k.lower() in ("http_proxy", "https_proxy", "all_proxy"))
        self.say(f"proxy env: {', '.join(proxies) or '无'}")

    def anonymous(self) -> None:
        self.section("1. 匿名访问预订系统")
        final = self.walk("GET", BOOKING)
        self.say(f"   停在 {final.url.host}{final.url.path}（{describe(final)}）")

    def login(self, user: str, password: str) -> httpx.Response:
        self.section("2. ADFS 登录页")
        page = self.walk("GET", ADFS_AUTHORIZE)
        form = next((f for f in parse_forms(page.text) if "Password" in f.fields), None)
        if form is None:
            raise ProbeError(f"登录页里没有密码表单（{describe(page)}）")
        action = urljoin(str(page.url), form.action)
        if urlsplit(action).scheme != "https" or urlsplit(action).hostname != ADFS_HOST:
            raise ProbeError("登录表单的提交地址不是 sts.cuhk.edu.cn，已中止，密码没有发出")
        self.say(f"   表单 #{form.id} 字段：{', '.join(form.fields)}")

        self.section("3. 提交凭据，跟随回跳")
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
        self.say(f"   停在 {final.url.host}{final.url.path}（{describe(final)}）")
        return final

    def finish_atrust(self, landing: httpx.Response) -> None:
        """代替门户页的 JS 走完登录收尾：环境上报、authCheck、accessCheck。"""
        self.section("4. aTrust 登录收尾（代替门户页脚本）")
        flow = dict(parse_qsl(urlsplit(str(landing.url)).query, keep_blank_values=True))
        extra = loads_or_empty(flow.get("data"))
        self.say(f"   落地参数 source={flow.get('source')!r} dest={flow.get('dest')!r} nextService={flow.get('nextService')!r}")
        self.say(f"   nextServiceList: {shape(loads_or_empty(flow.get('nextServiceList')))}")
        self.say(f"   data: {shape(extra)}")

        config = self.atrust("GET", "/passport/v1/public/authConfig", params={"mod": "1", "needTicket": "1"})
        self.csrf = ((config.get("data") or {}).get("security") or {}).get("csrfToken", "")
        self.say(f"   csrfToken: {'已取得' if self.csrf else '没有取得'}")

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

        self.atrust("GET", "/passport/v1/user/onlineInfo")

    def enter_booking(self) -> bool:
        self.section("5. 带着会话回到预订系统")
        final = self.walk("GET", BOOKING)
        self.say(f"   停在 {final.url.host}{final.url.path}（{describe(final)}）")
        if final.url.host != urlsplit(BOOKING).hostname:
            return False

        self.section("6. 已登录页面的结构（只读，只发 GET）")
        home = self.walk("GET", BOOKING_HOME)
        self.say(f"   {describe(home)}")
        if home.url.host != urlsplit(BOOKING).hostname or home.status_code != 200:
            return False
        seen = {urlsplit(BOOKING_HOME).path}
        queue = self._report_page(home)
        visited = 0
        while queue and visited < EXPLORE_LIMIT:
            path = queue.pop(0)
            if path in seen:
                continue
            seen.add(path)
            visited += 1
            page = self.request("GET", urljoin(BOOKING, path))
            self.say(f"     ↳ {describe(page)}")
            if page.status_code == 200 and "html" in page.headers.get("content-type", ""):
                queue.extend(p for p in self._report_page(page) if p not in seen)
            elif "json" in page.headers.get("content-type", ""):
                self.say(f"     ↳ {shape(loads_or_empty(page.text))}")
        return True

    def _report_page(self, page: httpx.Response) -> list[str]:
        """列出页面里的 /a/ 路径和表单字段，返回值得继续只读访问的路径。"""
        paths = sorted(set(re.findall(r'["\'(=](/a/[A-Za-z0-9_/.\-]+)', page.text)))
        self.say(f"     /a/ 路径 {len(paths)} 个：{' '.join(paths[:60])}")
        for found in parse_forms(page.text):
            self.say(f"     表单 #{found.id or '?'} {found.method} → {urlsplit(found.action).path}: {found.describe_fields()}")
        return [p for p in paths if EXPLORE_PATH.search(p) and not UNSAFE_PATH.search(p) and "." not in p.rsplit("/", 1)[-1]]

    def cookies(self) -> None:
        self.section("会话 Cookie（只列属性，不含取值）")
        now = time.time()
        for cookie in sorted(self.client.cookies.jar, key=lambda c: (c.domain, c.name)):
            life = "会话级" if cookie.expires is None else f"{(cookie.expires - now) / 3600:.1f}h"
            flags = [flag for flag, on in (("Secure", cookie.secure),
                                           ("HttpOnly", cookie.has_nonstandard_attr("HttpOnly"))) if on]
            self.say(f"   {cookie.domain:<24} {cookie.name:<28} path={cookie.path:<10} 有效期={life:<8} {' '.join(flags)}")


def describe(response: httpx.Response) -> str:
    kind = response.headers.get("content-type", "?").split(";")[0]
    title = re.search(r"<title[^>]*>(.*?)</title>", response.text, re.S | re.I) if "html" in kind else None
    scripts = len(re.findall(r"<script", response.text, re.I)) if "html" in kind else 0
    text = f"{response.status_code} {kind} {len(response.content)}B"
    if title:
        text += f" 标题「{title.group(1).strip()[:40]}」"
    if scripts:
        text += f" {scripts} 个脚本"
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="探测 MUS Booking 的登录链路")
    parser.add_argument("--anonymous", action="store_true", help="只测连通性，不登录")
    parser.add_argument("--report", default="probe-report.txt", help="报告输出路径")
    args = parser.parse_args()

    probe = Probe()
    verdict = ""
    try:
        probe.environment()
        probe.anonymous()
        if args.anonymous:
            page = probe.walk("GET", ADFS_AUTHORIZE)
            reachable = any("Password" in f.fields for f in parse_forms(page.text))
            probe.atrust("GET", "/passport/v1/public/authConfig", params={"mod": "1"})
            verdict = "匿名连通正常，ADFS 登录页可达" if reachable else "ADFS 登录页异常，没有找到密码表单"
        else:
            user = input("\n学号：").strip()
            password = getpass.getpass("密码（不回显，只发往 sts.cuhk.edu.cn）：")
            landing = probe.login(user, password)
            probe.finish_atrust(landing)
            ok = probe.enter_booking()
            verdict = ("纯 HTTP 登录走通了，服务器上不需要浏览器" if ok
                       else "登录后没能进入预订系统，卡住的位置见上面最后几跳")
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
