"""命令行：启动服务、预览、生成邀请码和管理链接、检查能否连到学校系统。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

import httpx

from . import __version__
from .config import Settings
from .domain import CHINA
from .store import MissingKey, Store, Vault


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mus-booking", description="每晚 21:00 替音乐学院的同学预定琴房")
    parser.add_argument("--version", action="version", version=f"MUS Booking {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="命令")

    serve = commands.add_parser("serve", help="启动服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--root-path", default="", help="挂在路径前缀下时填写，例如 /booking")

    commands.add_parser("init", help="生成密钥文件和数据目录")
    commands.add_parser("invite", help="生成一个邀请码")
    commands.add_parser("admin-link", help="生成一次性的管理链接，15 分钟内有效")

    preview = commands.add_parser("preview", help="用假学校系统和示例数据启动，查看全部页面")
    preview.add_argument("--port", type=int, default=8000)

    commands.add_parser("check", help="不登录，检查能否连到学校系统")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx 在 INFO 级别会记下完整地址，其中有登录用的一次性 code，不能进日志
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return {
        "serve": _serve,
        "init": _init,
        "invite": _invite,
        "admin-link": _admin_link,
        "preview": _preview,
        "check": _check,
    }[args.command](args)


def _open_store(settings: Settings) -> Store:
    try:
        vault = Vault.load(settings.key_file)
    except MissingKey as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
    return Store.open(settings.database, vault)


def _now():
    from datetime import datetime

    return datetime.now(CHINA)


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .engine import Engine, SystemClock
    from .portal import School
    from .web.app import Services, create_app

    settings = Settings.from_env()
    store = _open_store(settings)
    clock = SystemClock()
    engine = Engine(store, clock, School.open)
    app = create_app(Services(store, engine, clock, School.open))
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        root_path=args.root_path.rstrip("/"),
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        access_log=False,
        log_config=None,
    )
    return 0


def _init(_: argparse.Namespace) -> int:
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    created = Vault.create(settings.key_file)
    print(f"密钥文件：{settings.key_file}（{'新建' if created else '已存在，未改动'}）")
    _open_store(settings).close()
    print(f"数据库：{settings.database}")
    return 0


def _invite(_: argparse.Namespace) -> int:
    settings = Settings.from_env()
    store = _open_store(settings)
    code = store.create_invite(_now())
    print(code)
    print(f"{settings.public_url}/?invite={code}")
    return 0


def _admin_link(_: argparse.Namespace) -> int:
    settings = Settings.from_env()
    token = _open_store(settings).create_admin_link(_now())
    print(f"{settings.public_url}/admin/enter/{token}")
    return 0


def _preview(args: argparse.Namespace) -> int:
    import uvicorn

    from .preview import build
    from .web.app import create_app

    services, links = build()
    base = f"http://127.0.0.1:{args.port}"
    print("预览已准备好：")
    print(f"  成员首页   {base}{links['member']}")
    print(f"  管理页     {base}{links['admin']}")
    print(f"  绑定新账号 {base}{links['invite']}（学号 / 密码：{links['newcomer']}）")
    uvicorn.run(create_app(services), host="127.0.0.1", port=args.port, access_log=False, log_config=None)
    return 0


def _check(_: argparse.Namespace) -> int:
    """依次访问统一认证、网关和场地系统，不提交任何账号信息。"""
    from .portal import pages
    from .portal.school import ATRUST, ATRUST_COMMON, AUTHORIZE, BOOKING, USER_AGENT

    async def run() -> bool:
        ok = True
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True) as client:
            checks = (
                ("统一认证的登录页", AUTHORIZE, lambda r: pages.password_form(r.text) is not None),
                (
                    "网关的公开配置",
                    f"{ATRUST}/passport/v1/public/authConfig?mod=1&clientType="
                    f"{ATRUST_COMMON['clientType']}&platform={ATRUST_COMMON['platform']}&lang=zh-CN",
                    lambda r: r.status_code == 200 and "code" in r.text,
                ),
                ("场地系统", f"{BOOKING}/", lambda r: r.status_code < 500),
            )
            for label, url, good in checks:
                started = time.monotonic()
                try:
                    response = await client.get(url)
                    passed = good(response)
                    detail = f"HTTP {response.status_code}，停在 {response.url.host}"
                except httpx.HTTPError as error:
                    passed, detail = False, f"{type(error).__name__}: {error}"
                ok &= passed
                print(f"{'✓' if passed else '✗'} {label}：{detail}（{time.monotonic() - started:.1f} 秒）")
        return ok

    return 0 if asyncio.run(run()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
