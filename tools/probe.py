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
主机、路径、状态码、耗时、Cookie 名和表单字段名，不含任何取值，可以直接转交。
"""

from __future__ import annotations

import argparse
import getpass
import os
import platform
import re
import ssl
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

BOOKING = "https://booking.cuhk.edu.cn/"
BOOKING_HOME = "https://booking.cuhk.edu.cn/a/field/client/main"
ADFS_HOST = "sts.cuhk.edu.cn"
# aTrust 门户用 JS 跳到这个地址；其中 client_id 是公开的客户端标识，不是密钥。
ADFS_AUTHORIZE = (
    "https://sts.cuhk.edu.cn/adfs/oauth2/authorize"
    "?resource=https%3A%2F%2Fatrust.cuhk.edu.cn"
    "&client_id=e6d8a534-4b6f-4021-bb44-51ee9d0b63fd"
    "&redirect_uri=https%3A%2F%2Fatrust.cuhk.edu.cn%2Fpassport%2Fv1%2Fauth%2FhttpsOauth2"
    "&response_type=code"
)
ADFS_DOMAIN = "cuhksz"  # 登录页脚本会给不含 @ 或 \ 的用户名补上这个域前缀
ALLOWED_SUFFIX = ".cuhk.edu.cn"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
REDIRECTS = {301, 302, 303, 307, 308}
MAX_HOPS = 20
MFA_MARKERS = ("mfaGreeting", "verificationCodeInput", "authOptions", "azureMfa", "otpInput")


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
        elif tag == "input" and self._open is not None and a.get("name"):
            self._open.fields[a["name"]] = (a.get("type", "text").lower(), a.get("value", ""))

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

    def say(self, text: str = "") -> None:
        print(text)
        self.lines.append(text)

    def section(self, title: str) -> None:
        self.say()
        self.say(f"== {title}")

    def request(self, method: str, url: str, data: dict[str, str] | None = None, note: str = "") -> httpx.Response:
        parts = urlsplit(url)
        if parts.scheme != "https" or not (parts.hostname or "").endswith(ALLOWED_SUFFIX):
            raise ProbeError(f"链路跳出了学校域名（{parts.netloc}），已停止")
        sent = time.time()
        try:
            response = self.client.request(method, url, data=data)
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
            response = self.request(method, url, data, note)
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

    def login(self, user: str, password: str) -> None:
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

    def enter_booking(self) -> bool:
        self.section("4. 带着会话回到预订系统")
        final = self.walk("GET", BOOKING)
        self.say(f"   停在 {final.url.host}{final.url.path}（{describe(final)}）")
        if final.url.host != urlsplit(BOOKING).hostname:
            return False

        self.section("5. 已登录页面的结构（只读）")
        home = self.walk("GET", BOOKING_HOME)
        self.say(f"   {describe(home)}")
        paths = sorted(set(re.findall(r'["\'(](/a/[A-Za-z0-9_/.\-]+)', home.text)))
        self.say(f"   页面内出现的 /a/ 路径（{len(paths)} 个）：")
        for path in paths[:80]:
            self.say(f"     {path}")
        for found in parse_forms(home.text):
            self.say(f"   表单 #{found.id or '?'} {found.method} → {urlsplit(found.action).path}: {', '.join(found.fields)}")
        return home.url.host == urlsplit(BOOKING).hostname and home.status_code == 200

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
            verdict = "匿名连通正常，ADFS 登录页可达" if reachable else "ADFS 登录页异常，没有找到密码表单"
        else:
            user = input("\n学号：").strip()
            password = getpass.getpass("密码（不回显，只发往 sts.cuhk.edu.cn）：")
            probe.login(user, password)
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
