# -*- coding: utf-8 -*-
"""
配置管理
从 GUI.py 提取的模块
"""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from datetime import datetime, date, timedelta
from typing import List, Optional

import yaml

# 导入常量和工具函数
from constants import PLACES, DEFAULT_THEME
from utils import app_base_dir

CONFIG_FILE = os.path.join(app_base_dir(), "config.yaml")

@dataclass
class RequestItemData:
    place: str = PLACES[0]
    date: str = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")  # 默认：明天
    start: str = "19:00"
    end: str = "21:00"

@dataclass
class AppConfig:
    target_time: str = datetime.now().strftime("%Y-%m-%d 21:00:00")  # 当天 21:00:00
    start_immediately: bool = False

    proxies: str = ""  # 文本框中保存的原始字符串（JSON/YAML 皆可）
    cookie: str = ""
    cookie_updated_at: str = ""

    user_id: str = ""
    user_password: str = ""  # 用户密码（用于自动登录）
    user_name: str = ""
    user_email: str = ""
    user_phone: str = ""
    theme: str = DEFAULT_THEME

    requests: Optional[List[RequestItemData]] = None  # type: ignore

    def __post_init__(self):
        if self.requests is None:
            self.requests = [RequestItemData()]

class ConfigManager:
    @staticmethod
    def load(path: str = CONFIG_FILE) -> AppConfig:
        if not os.path.exists(path):
            return AppConfig()
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        # 兼容旧结构
        reqs = []
        for it in raw.get("requests", []):
            reqs.append(RequestItemData(**it))
        cfg = AppConfig(
            target_time=raw.get("target_time", datetime.now().strftime("%Y-%m-%d 21:00:00")),
            start_immediately=bool(raw.get("start_immediately", False)),
            proxies=raw.get("proxies", ""),
            cookie=raw.get("cookie", ""),
            cookie_updated_at=raw.get("cookie_updated_at", ""),
            user_id=raw.get("user_id", ""),
            user_password=raw.get("user_password", ""),
            user_name=raw.get("user_name", ""),
            user_email=raw.get("user_email", ""),
            user_phone=raw.get("user_phone", ""),
            theme=raw.get("theme", DEFAULT_THEME),
            requests=reqs or [RequestItemData()],
        )
        return cfg

    @staticmethod
    def save(cfg: AppConfig, path: str = CONFIG_FILE):
        data = asdict(cfg)
        # dataclass 中的 RequestItemData 需要转为普通 dict
        data["requests"] = [asdict(r) for r in cfg.requests]
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)