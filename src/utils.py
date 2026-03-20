# -*- coding: utf-8 -*-
"""
工具函数模块
提供应用程序基础设施和实用函数
"""

from __future__ import annotations

import os
import sys
import json
from typing import Dict, List, Optional, Tuple

import yaml


def app_base_dir() -> str:
    """
    获取应用程序基础目录
    - 打包后：可执行文件所在目录
    - 开发时：当前文件所在目录
    """
    try:
        if getattr(sys, "frozen", False):
            return os.path.dirname(sys.executable)
    except Exception:
        pass
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(rel: str) -> str:
    """
    获取资源文件的绝对路径
    - 打包后：从 PyInstaller 临时目录读取
    - 开发时：从应用目录读取
    """
    try:
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            return os.path.join(sys._MEIPASS, rel)
    except Exception:
        pass
    return os.path.join(app_base_dir(), rel)


# ---- 加载核心模块：book() / timer_run() 来自用户的 main.txt 或 main.py ----
def _load_core():
    """
    优先从同目录的 main.py / main.txt 加载（支持打包后的资源目录），否则尝试 import main。
    """
    # 1) 直接从文件加载（优先）：main.py / main.txt（资源路径）
    try:
        import importlib.util
        for name in ("main.py", "main.txt"):
            cand = resource_path(name)
            if os.path.exists(cand):
                spec = importlib.util.spec_from_file_location("cuhk_booking_core", cand)
                if spec and spec.loader:
                    core = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(core)
                    if hasattr(core, "book") and hasattr(core, "timer_run"):
                        return core
    except Exception:
        pass

    # 2) 兜底：import main（若被打包为模块或在 sys.path 中）
    try:
        import importlib
        core = importlib.import_module("main")
        if hasattr(core, "book") and hasattr(core, "timer_run"):
            return core
    except Exception:
        pass

    raise ImportError("未找到核心文件 main.py 或 main.txt，或其缺少 book()/timer_run() 函数。")


CORE = _load_core()
book = CORE.book
timer_run = CORE.timer_run


# ---- 导入代理检测模块 ----
try:
    from proxy_detector import ProxyDetector
    PROXY_DETECTOR_AVAILABLE = True
except ImportError:
    PROXY_DETECTOR_AVAILABLE = False
    print("注意：未找到 proxy_detector.py，自动代理检测功能将不可用。")


# ---- 工具函数 ----

def parse_proxies(raw: str) -> Optional[Dict[str, str]]:
    """
    解析代理配置字符串
    支持 JSON / YAML 格式，或简单的 "host:port" 格式

    Args:
        raw: 代理配置字符串

    Returns:
        代理字典 {"http": "...", "https": "..."}，或 None（无代理）

    Examples:
        >>> parse_proxies('{"http": "127.0.0.1:9000"}')
        {'http': '127.0.0.1:9000'}
        >>> parse_proxies('127.0.0.1:9000')
        {'http': '127.0.0.1:9000', 'https': '127.0.0.1:9000'}
        >>> parse_proxies('')
        None
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        # 先尝试 JSON
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    try:
        obj = yaml.safe_load(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # 最后兜底：支持 "host:port" 一行，自动映射为 http/https
    if ":" in raw and " " not in raw and "{" not in raw:
        return {"http": raw, "https": raw}
    return None


def hhmm_to_minutes(hhmm: str) -> int:
    """
    将 "HH:MM" 时间格式转换为分钟数

    Args:
        hhmm: 时间字符串，格式为 "HH:MM"

    Returns:
        从午夜开始的分钟数

    Example:
        >>> hhmm_to_minutes("19:30")
        1170
    """
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def minutes_to_hhmm(x: int) -> str:
    """
    将分钟数转换为 "HH:MM" 时间格式

    Args:
        x: 从午夜开始的分钟数

    Returns:
        时间字符串，格式为 "HH:MM"

    Example:
        >>> minutes_to_hhmm(1170)
        "19:30"
    """
    h = x // 60
    m = x % 60
    return f"{h:02d}:{m:02d}"


def split_to_slots(start_hm: str, end_hm: str, max_minutes: int = 120) -> List[Tuple[str, str]]:
    """
    将时间段拆分为多个不超过 max_minutes 的时隙
    用于处理超长预订（如超过2小时）

    Args:
        start_hm: 开始时间，格式为 "HH:MM"
        end_hm: 结束时间，格式为 "HH:MM"
        max_minutes: 每个时隙的最大分钟数，默认120（2小时）

    Returns:
        时隙列表，每个元素为 (开始时间, 结束时间) 元组

    Example:
        >>> split_to_slots("19:00", "22:00", max_minutes=120)
        [('19:00', '21:00'), ('21:00', '22:00')]
    """
    s = hhmm_to_minutes(start_hm)
    e = hhmm_to_minutes(end_hm)
    out = []
    cur = s
    while cur < e:
        nxt = min(cur + max_minutes, e)
        out.append((minutes_to_hhmm(cur), minutes_to_hhmm(nxt)))
        cur = nxt
    return out
