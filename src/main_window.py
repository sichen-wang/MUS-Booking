# -*- coding: utf-8 -*-
"""
MainWindow主窗口
从 GUI.py 提取的模块
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import List, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

try:
    from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    WEBENGINE_AVAILABLE = True
except ImportError:
    WEBENGINE_AVAILABLE = False

# 导入工具函数
from utils import app_base_dir, resource_path, parse_proxies, hhmm_to_minutes, minutes_to_hhmm, split_to_slots

# 导入核心逻辑
from main import timer_run

# 导入配置管理
from config import RequestItemData, AppConfig, ConfigManager

# 导入常量
from constants import PLACES, REQUIRED_FIELDS

# 导入代理检测
try:
    from proxy_detector import ProxyDetector
    PROXY_DETECTOR_AVAILABLE = True
except ImportError:
    PROXY_DETECTOR_AVAILABLE = False
    print("注意：未找到 proxy_detector.py，自动代理检测功能将不可用。")

# 导入自定义控件
from widgets import WheelCombo, DateWheel, TimeWheel, MinuteToggle, RequestItemWidget

# 导入对话框
from dialogs import SettingsDialog, CookieDialog, AutoLoginDialog

# 导入后台线程
from workers import BookingWorker


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        # 软件标题改为“魔丸”
        self.setWindowTitle("魔丸")
        # 左上角图标设置为 CCA.ico（需与可执行放同目录）
        try:
            self.setWindowIcon(QtGui.QIcon(resource_path("CCA.ico")))
        except Exception:
            pass

        # 样式（Fusion + 轻量美化 + 全局字体）
        QtWidgets.QApplication.setStyle("Fusion")
        app = QtWidgets.QApplication.instance()
        if app is not None:
            # 尝试加载打包内字体
            try:
                QPF = QtGui.QFontDatabase
                fonts_dir = resource_path("fonts")
                for fname in ("FiraCode-Regular.ttf", "FangZhengXinShuSongJianTi-1.ttf"):
                    fpath = os.path.join(fonts_dir, fname)
                    if os.path.exists(fpath):
                        QPF.addApplicationFont(fpath)
            except Exception:
                pass

            font = app.font()
            font.setFamily("Fira Code, 方正新书宋简体")
            app.setFont(font)
            app.setStyleSheet("* { font-family: 'Fira Code','方正新书宋简体'; }")
        self.setMinimumSize(920, 680)

        # 配置
        self.cfg = ConfigManager.load()

        # 启动时自动检测代理（如果proxies为空或检测失败）
        self._auto_detect_proxy_on_startup()

        # 顶部工具栏：设置、Cookie
        self.btn_settings = QtWidgets.QToolButton()
        self.btn_settings.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_FileDialogDetailedView))
        self.btn_settings.setToolTip("设置（proxies / 用户信息 / 主题）")

        self.btn_cookie = QtWidgets.QToolButton()
        self.btn_cookie.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DialogOpenButton))
        self.btn_cookie.setToolTip("设置 Cookie")

        self.cookie_info = QtWidgets.QLabel(self._cookie_summary())
        self.cookie_info.setStyleSheet("color:#777;")

        topbar = QtWidgets.QHBoxLayout()
        topbar.addWidget(self.btn_settings)
        topbar.addWidget(self.btn_cookie)
        topbar.addWidget(self.cookie_info)
        topbar.addStretch()

        # 目标时间（滚轮）+ 立即启动
        tgt_group = QtWidgets.QGroupBox("启动时间")
        # 放大并加粗“启动时间”标题
        tgt_group.setStyleSheet("QGroupBox::title { font-size: 18px; font-weight: 700; }")
        tgt_lay = QtWidgets.QHBoxLayout(tgt_group)

        # 日期轮 + 时间轮（秒也支持）
        target_dt = datetime.strptime(self.cfg.target_time, "%Y-%m-%d %H:%M:%S")
        self.target_date = DateWheel(default=target_dt.date())
        h_items = [f"{h:02d}" for h in range(0, 24)]
        m_items = [f"{m:02d}" for m in range(0, 60)]
        s_items = [f"{s:02d}" for s in range(0, 60)]
        self.target_h = WheelCombo(h_items)
        self.target_m = WheelCombo(m_items)
        self.target_s = WheelCombo(s_items)
        self.target_h.setCurrentText(f"{target_dt.hour:02d}")
        self.target_m.setCurrentText(f"{target_dt.minute:02d}")
        self.target_s.setCurrentText(f"{target_dt.second:02d}")

        tgt_lay.addWidget(self.target_date, 3)
        tgt_lay.addWidget(self.target_h, 1)
        tgt_lay.addWidget(self.target_m, 1)
        tgt_lay.addWidget(self.target_s, 1)

        self.cb_immediate = QtWidgets.QCheckBox("立即启动")
        self.cb_immediate.setChecked(self.cfg.start_immediately)
        tgt_lay.addWidget(self.cb_immediate)

        # 请求列表
        self.req_container = QtWidgets.QWidget()
        self.req_vbox = QtWidgets.QVBoxLayout(self.req_container)
        self.req_vbox.setContentsMargins(0, 0, 0, 0)
        self.req_vbox.setSpacing(4)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.req_container)

        self.btn_add = QtWidgets.QPushButton("＋ 添加一组预定请求")
        self.btn_add.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        # 日志 + 启动
        self.te_log = QtWidgets.QPlainTextEdit()
        self.te_log.setReadOnly(True)
        self.te_log.setPlaceholderText("运行日志将在此显示…")

        self.btn_start = QtWidgets.QPushButton("开始")
        # 顶边不变，底边与左侧组框对齐：垂直方向可扩展
        self.btn_start.setMinimumHeight(44)
        self.btn_start.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Expanding)
        self.btn_start.setStyleSheet("font-size:22px; font-weight:600;")

        # 主布局
        central = QtWidgets.QWidget()
        main = QtWidgets.QVBoxLayout(central)
        main.setContentsMargins(16, 12, 16, 12)
        main.setSpacing(8)
        main.addLayout(topbar)
        tgt_row = QtWidgets.QHBoxLayout()
        tgt_row.addWidget(tgt_group, 4)
        # 把“开始”按钮放在目标时间旁边
        tgt_row.addWidget(self.btn_start, 1)
        main.addLayout(tgt_row)
        main.addWidget(self.scroll, 2)
        main.addWidget(self.btn_add, 0)
        # 运行日志高度小一些
        self.te_log.setMaximumHeight(160)
        main.addWidget(self.te_log, 1)
        self.setCentralWidget(central)

        # 信号
        self.btn_settings.clicked.connect(self.open_settings)
        self.btn_cookie.clicked.connect(self.open_cookie)
        self.btn_add.clicked.connect(self.add_request_item)
        self.btn_start.clicked.connect(self.on_start_clicked)

        # 载入配置中的请求
        for r in self.cfg.requests:
            self.add_request_item(r)

        # 状态
        self.worker = None
        self.scheduled_timer = None  # threading.Timer 来自用户 core.timer_run()
        self.qt_timer = None         # Qt 定时器，保障到点必触发
        self._has_started = False    # 防止重复触发

    # --- helpers ---
    def _cookie_summary(self) -> str:
        if self.cfg.cookie and self.cfg.cookie_updated_at:
            return f"Cookie 已设置，最近更新时间：{self.cfg.cookie_updated_at}"
        elif self.cfg.cookie:
            return f"Cookie 已设置"
        else:
            return "Cookie 未设置"

    def _auto_detect_proxy_on_startup(self):
        """启动时自动检测网络配置（优先检测Reqable代理）"""
        if not PROXY_DETECTOR_AVAILABLE:
            return

        print("[启动] 开始自动检测网络配置（优先检测Reqable）...")

        # 禁用SSL警告
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass

        # 自动检测网络配置（总是执行，不跳过）
        try:
            proxy_dict = ProxyDetector.auto_detect()
            proxy_str = ProxyDetector.format_for_config(proxy_dict)

            if proxy_dict:
                # 检测到代理（Reqable等）
                self.cfg.proxies = proxy_str
                ConfigManager.save(self.cfg)
                print(f"[启动] [OK] 已自动检测并应用代理: {proxy_str}")
            else:
                # 可以直接连接（校园网或VPN）
                self.cfg.proxies = ""
                ConfigManager.save(self.cfg)
                print("[启动] [OK] 检测到可以直接连接（校园网内或 AnyConnect VPN）")
                print("[启动] [WARNING] 注意：booking接口可能需要Reqable代理才能正常工作")
        except Exception as e:
            print(f"[启动] [ERROR] 自动检测网络配置失败: {e}")

    def add_request_item(self, data: RequestItemData | None = None):
        item = RequestItemWidget(data)
        item.removed.connect(self._remove_request_item)
        item.changed.connect(self._save_requests_snapshot)
        self.req_vbox.addWidget(item)
        # 若当前只有一组请求，进一步压缩其内部间距
        if len(self._iter_items()) == 1:
            # 缩小该卡片的外边距/行距
            w: RequestItemWidget = item
            lay = w.layout()
            if isinstance(lay, QtWidgets.QGridLayout):
                lay.setContentsMargins(2, 2, 2, 2)
                lay.setHorizontalSpacing(4)
                lay.setVerticalSpacing(1)
        self._save_requests_snapshot()

    def _remove_request_item(self, item: RequestItemWidget):
        item.setParent(None)
        item.deleteLater()
        self._save_requests_snapshot()

    def _iter_items(self) -> List[RequestItemWidget]:
        res = []
        for i in range(self.req_vbox.count()):
            w = self.req_vbox.itemAt(i).widget()
            if isinstance(w, RequestItemWidget):
                res.append(w)
        return res

    def _save_requests_snapshot(self):
        reqs: List[RequestItemData] = [w.to_data() for w in self._iter_items()]
        self.cfg.requests = reqs
        # 同步保存 config（避免意外退出丢失）
        self.cfg.target_time = self.current_target_time()
        self.cfg.start_immediately = self.cb_immediate.isChecked()
        ConfigManager.save(self.cfg)

    def current_target_time(self) -> str:
        ymd = self.target_date.value()
        h = self.target_h.currentText()
        m = self.target_m.currentText()
        s = self.target_s.currentText()
        return f"{ymd} {h}:{m}:{s}"

    # --- actions ---
    def open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        dlg.saved.connect(lambda _: self._on_settings_saved())
        dlg.exec()

    def _on_settings_saved(self):
        ConfigManager.save(self.cfg)
        QtWidgets.QMessageBox.information(self, "已保存", "设置已保存。")

    def open_cookie(self):
        """打开Cookie设置对话框，提供手动粘贴和自动登录两种方式"""
        if WEBENGINE_AVAILABLE:
            # 提供选择：自动登录 或 手动粘贴
            choice = QtWidgets.QMessageBox()
            choice.setWindowTitle("设置Cookie")
            choice.setText("请选择Cookie获取方式：")
            choice.setInformativeText(
                "【自动登录】- 推荐！打开浏览器窗口自动登录并捕获\n"
                "【手动粘贴】- 从浏览器开发者工具手动复制Cookie"
            )
            btn_auto = choice.addButton("自动登录", QtWidgets.QMessageBox.YesRole)
            btn_manual = choice.addButton("手动粘贴", QtWidgets.QMessageBox.NoRole)
            choice.addButton(QtWidgets.QMessageBox.Cancel)
            choice.setDefaultButton(btn_auto)

            choice.exec()
            clicked = choice.clickedButton()

            if clicked == btn_auto:
                # 自动登录方式
                self._open_auto_login()
            elif clicked == btn_manual:
                # 手动粘贴方式（原有逻辑）
                self._open_manual_cookie()
        else:
            # 如果 QtWebEngine 不可用，只提供手动粘贴
            self._open_manual_cookie()

    def _open_manual_cookie(self):
        """手动粘贴Cookie与代理"""
        dlg = CookieDialog(self.cfg.cookie, self.cfg.proxies, self)
        def _save_cookie_and_proxy(new_cookie: str, new_proxies: str):
            self.cfg.cookie = new_cookie or ""
            self.cfg.cookie_updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.cfg.proxies = new_proxies or ""
            self.cookie_info.setText(self._cookie_summary())
            ConfigManager.save(self.cfg)
        dlg.saved.connect(_save_cookie_and_proxy)
        dlg.exec()

    def _open_auto_login(self):
        """打开自动登录对话框"""
        try:
            dlg = AutoLoginDialog(
                proxies_config=self.cfg.proxies,
                user_id=self.cfg.user_id,
                user_password=self.cfg.user_password,
                parent=self
            )

            def _on_cookie_captured(cookie_str: str):
                self.cfg.cookie = cookie_str
                self.cfg.cookie_updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.cookie_info.setText(self._cookie_summary())
                ConfigManager.save(self.cfg)
                QtWidgets.QMessageBox.information(
                    self, "成功",
                    f"已自动捕获Cookie！\n共{len(cookie_str)}个字符\n更新时间：{self.cfg.cookie_updated_at}"
                )

            dlg.cookie_captured.connect(_on_cookie_captured)
            dlg.exec()
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self, "错误",
                f"自动登录功能启动失败：\n{e}\n\n请使用手动粘贴方式。"
            )
            self._open_manual_cookie()

    def _collect_chunks(self) -> List[Tuple[str, str, str]]:
        """
        将 UI 中的每组请求拆分为若干 <= 2h 的片段。
        返回 [(place, 'YYYY-MM-DD HH:MM','YYYY-MM-DD HH:MM'), ...]
        """
        chunks: List[Tuple[str, str, str]] = []
        for w in self._iter_items():
            d = w.to_data()

            # 基础校验
            place = (d.place or "").strip()
            if not place:
                raise ValueError("存在空的地点（place）。")
            # 时间
            s_minutes = hhmm_to_minutes(d.start)
            e_minutes = hhmm_to_minutes(d.end)
            if e_minutes < s_minutes:
                raise ValueError(f"{place} 的结束时间早于开始时间。")

            # 拆分
            for s_hm, e_hm in split_to_slots(d.start, d.end, max_minutes=120):
                start_ts = f"{d.date} {s_hm}"
                end_ts = f"{d.date} {e_hm}"
                chunks.append((place, start_ts, end_ts))
        return chunks

    def _append_log(self, text: str):
        self.te_log.appendPlainText(text)
        self.te_log.verticalScrollBar().setValue(self.te_log.verticalScrollBar().maximum())

    def _set_controls_enabled(self, enabled: bool):
        self.btn_settings.setEnabled(enabled)
        self.btn_cookie.setEnabled(enabled)
        self.btn_add.setEnabled(enabled)
        for w in self._iter_items():
            w.setEnabled(enabled)
        self.target_date.setEnabled(enabled)
        self.target_h.setEnabled(enabled)
        self.target_m.setEnabled(enabled)
        self.target_s.setEnabled(enabled)
        self.cb_immediate.setEnabled(enabled)
        self.btn_start.setEnabled(enabled)

    def _start_worker_now(self):
        # 防重复：若已触发过则直接返回
        if getattr(self, "_has_started", False):
            return
        self._has_started = True

        # 组装 chunks 并启动线程
        try:
            chunks = self._collect_chunks()
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "参数错误", str(e))
            self._set_controls_enabled(True)
            return

        # 提示 proxies 解析情况
        proxies = parse_proxies(self.cfg.proxies)
        if self.cfg.proxies and proxies is None:
            QtWidgets.QMessageBox.warning(self, "proxies 格式错误", "请在设置中以 JSON 或 YAML 格式填写 proxies，例如：\n"
                                              '{"http":"10.101.28.225:9000","https":"10.101.28.225:9000"}')
            self._set_controls_enabled(True)
            return

        self._append_log(f"开始执行，共 {len(chunks)} 个片段…")
        self.worker = BookingWorker(self.cfg, chunks)
        self.worker.log.connect(self._append_log)
        self.worker.popup.connect(self._on_popup)
        self.worker.finished_all.connect(self._on_worker_finished)
        self.worker.start()

    def _on_popup(self, level: str, message: str):
        if level == "info":
            QtWidgets.QMessageBox.information(self, "提示", message)
        elif level == "warn":
            QtWidgets.QMessageBox.warning(self, "提示", message)
        else:
            QtWidgets.QMessageBox.critical(self, "错误", message)

    def _on_worker_finished(self):
        self._append_log("所有片段执行完毕。")
        self._set_controls_enabled(True)
        # 允许再次启动
        self._has_started = False

    def on_start_clicked(self):
        # 保存一份配置快照
        self.cfg.target_time = self.current_target_time()
        self.cfg.start_immediately = self.cb_immediate.isChecked()
        ConfigManager.save(self.cfg)

        # 必填校验（设置弹窗里的）
        missing = []
        for key in REQUIRED_FIELDS:
            if not getattr(self.cfg, key):
                missing.append(key)
        if missing:
            QtWidgets.QMessageBox.warning(self, "缺少必填项", '请先在"设置"中填写：' + "、".join(missing))
            return

        # 检查是否配置了密码（用于自动登录）
        if not self.cfg.user_password:
            QtWidgets.QMessageBox.warning(
                self, "缺少密码",
                '请先在"设置"中填写密码，以便自动登录获取Cookie。'
            )
            return

        # 每次运行前自动登录获取Cookie
        if WEBENGINE_AVAILABLE:
            self._append_log("正在自动登录获取Cookie...")
            self._auto_login_and_start()
        else:
            # 如果 WebEngine 不可用，检查是否有Cookie
            if not self.cfg.cookie:
                if QtWidgets.QMessageBox.question(
                    self, "未设置Cookie", '尚未设置 Cookie，是否继续？（可能导致"Cookie 过期"）',
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
                ) == QtWidgets.QMessageBox.No:
                    return
            self._set_controls_enabled(False)
            self._schedule_start()

    def _auto_login_and_start(self):
        """自动登录获取Cookie，然后开始预定"""
        try:
            dlg = AutoLoginDialog(
                proxies_config=self.cfg.proxies,
                user_id=self.cfg.user_id,
                user_password=self.cfg.user_password,
                parent=self
            )

            def _on_cookie_captured(cookie_str: str):
                self.cfg.cookie = cookie_str
                self.cfg.cookie_updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.cookie_info.setText(self._cookie_summary())
                ConfigManager.save(self.cfg)
                self._append_log(f"Cookie获取成功！更新时间：{self.cfg.cookie_updated_at}")

                # Cookie获取成功后，继续预定流程
                self._set_controls_enabled(False)
                self._schedule_start()

            def _on_rejected():
                self._append_log("用户取消了自动登录。")
                QtWidgets.QMessageBox.information(
                    self, "已取消",
                    '已取消自动登录。如需继续，请重新点击"开始"按钮。'
                )

            dlg.cookie_captured.connect(_on_cookie_captured)
            dlg.rejected.connect(_on_rejected)
            dlg.exec()

        except Exception as e:
            self._append_log(f"自动登录失败：{e}")
            QtWidgets.QMessageBox.critical(
                self, "自动登录失败",
                f"自动登录功能启动失败：\n{e}\n\n请检查网络连接或代理设置。"
            )

    def _schedule_start(self):
        """调度启动预定任务（立即或定时）"""
        if self.cb_immediate.isChecked():
            self._append_log('"立即启动"已勾选，马上开始…')
            self._start_worker_now()
        else:
            # 使用 Qt 定时器保证触发，并保留核心 timer_run 作为后备
            target = self.current_target_time()
            self._append_log(f"已预约在 {target} 启动…（窗口保持打开即可）")

            # 计算延迟
            try:
                run_dt = datetime.strptime(target, "%Y-%m-%d %H:%M:%S")
                delay_ms = int(max(0, (run_dt - datetime.now()).total_seconds()) * 1000)
            except Exception:
                delay_ms = 0

            if delay_ms == 0:
                self._append_log("目标时间已到或已过，立即开始…")
                self._start_worker_now()
                return

            # 主调度：Qt 单次定时器
            if self.qt_timer is not None:
                try:
                    self.qt_timer.stop()
                except Exception:
                    pass
                try:
                    self.qt_timer.deleteLater()
                except Exception:
                    pass
                self.qt_timer = None
            self._has_started = False
            self.qt_timer = QtCore.QTimer(self)
            self.qt_timer.setSingleShot(True)
            self.qt_timer.timeout.connect(self._start_worker_now)
            self.qt_timer.start(delay_ms)

            # 后备：仍调用用户核心 timer_run（若触发也会因为 _has_started 防重）
            def _go():
                QtCore.QTimer.singleShot(0, self._start_worker_now)
            try:
                self.scheduled_timer = timer_run(target, _go)
            except Exception:
                self.scheduled_timer = None

def main():
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
