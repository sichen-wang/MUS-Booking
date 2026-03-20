# -*- coding: utf-8 -*-
"""
BookingWorker线程
从 GUI.py 提取的模块
"""

from __future__ import annotations

import time
from typing import List, Tuple

from PySide6 import QtCore

# 导入核心预订函数
from main import book

# 导入配置管理
from config import AppConfig

# 导入工具函数和常量
from utils import parse_proxies
from constants import DEFAULT_THEME, MUST_STOP_KEYWORDS


class BookingWorker(QtCore.QThread):
    log = QtCore.Signal(str)
    popup = QtCore.Signal(str, str)   # (level, message) level in {"info","warn","error"}
    finished_all = QtCore.Signal()

    def __init__(self, cfg: AppConfig, chunks: List[Tuple[str, str, str]], parent=None):
        """
        :param chunks: [(place, start_ts, end_ts)]，ts 格式 "YYYY-MM-DD HH:MM"
        """
        super().__init__(parent)
        self.cfg = cfg
        self.chunks = chunks
        self._stop_flag = False

    def stop(self):
        self._stop_flag = True

    def _try_once(self, place: str, start_ts: str, end_ts: str) -> str:
        return book(
            cookie=self.cfg.cookie,
            user_id=self.cfg.user_id,
            user_name=self.cfg.user_name,
            place=place,
            start_time=start_ts,
            end_time=end_ts,
            user_email=self.cfg.user_email,
            user_phone=self.cfg.user_phone,
            theme=self.cfg.theme or DEFAULT_THEME,
            proxies=parse_proxies(self.cfg.proxies),
        )

    def run(self):
        MUST_STOP = MUST_STOP_KEYWORDS
        for (place, start_ts, end_ts) in self.chunks:
            if self._stop_flag:
                break
            self.log.emit(f"开始预定：{place}  {start_ts} - {end_ts}")
            # 重试直到命中 MUST_STOP 之一
            while not self._stop_flag:
                try:
                    msg = self._try_once(place, start_ts, end_ts)
                except Exception as e:
                    msg = f"异常：{e!r}"

                self.log.emit(f"返回：{msg}")
                if any(key in msg for key in MUST_STOP):
                    # 弹窗
                    if "保存成功" in msg:
                        self.popup.emit("info", f"保存成功：{place}  {start_ts} - {end_ts}")
                    elif "手速太慢" in msg:
                        self.popup.emit("warn", f"已被预订：{place}  {start_ts} - {end_ts}")
                    elif "Cookie 过期" in msg:
                        self.popup.emit("error", "Cookie 过期，请在右上角按钮中重新设置 Cookie。")
                    elif "请求失败" in msg:
                        self.popup.emit("error", "请求失败，请检查网络、代理服务器或 VPN。")
                    break
                time.sleep(0.2)
        self.finished_all.emit()
