# -*- coding: utf-8 -*-
"""
TimeWheel控件
从 GUI.py 提取的模块
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

# 导入其他控件
from widgets.wheel_combo import WheelCombo


class TimeWheel(QtWidgets.QWidget):
    """
    时间滚轮：小时-分钟 两列；小时限制在 06~23；分钟 00~59。
    """
    valueChanged = QtCore.Signal(str)  # "HH:MM"

    def __init__(self, default: str = "19:00", parent=None):
        super().__init__(parent)
        h_items = [f"{h:02d}" for h in range(6, 24)]
        self.hh = WheelCombo(h_items)

        # 分钟：左右分栏 00/30 开关
        self._minute_toggle = MinuteToggle()

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(self.hh)
        lay.addWidget(self._minute_toggle)

        self.hh.currentIndexChanged.connect(self._emit)
        self._minute_toggle.valueChanged.connect(self._emit)

        # 初始化
        if ":" in default:
            h, m = default.split(":")
            if h.isdigit() and m.isdigit():
                hi = max(6, min(23, int(h)))
                mi = int(m)
                # 就近修正到 00 或 30（<15 -> 00; 15~44 -> 30; >=45 -> 下一小时 00；23点>=45固定为23:30）
                if mi < 15:
                    mm = "00"
                elif mi < 45:
                    mm = "30"
                else:
                    if hi < 23:
                        hi += 1
                        mm = "00"
                    else:
                        mm = "30"
                self.hh.setCurrentText(f"{hi:02d}")
                self._minute_toggle.setValue(mm)
        self._emit()

        # 对齐分钟开关与小时滚轮的高度
        QtCore.QTimer.singleShot(0, lambda: self._minute_toggle.setButtonHeight(self.hh.sizeHint().height()))

    def value(self) -> str:
        return f"{self.hh.currentText()}:{self._minute_toggle.value()}"

    def setValue(self, hhmm: str):
        if ":" in hhmm:
            h, m = hhmm.split(":")
            try:
                hi = max(6, min(23, int(h)))
                mi = int(m)
            except Exception:
                return
            if mi < 15:
                mm = "00"
            elif mi < 45:
                mm = "30"
            else:
                if hi < 23:
                    hi += 1
                    mm = "00"
                else:
                    mm = "30"
            self.hh.setCurrentText(f"{hi:02d}")
            self._minute_toggle.setValue(mm)
        # 不主动触发 _emit，避免在外层处理 valueChanged 时造成递归；
        # 若值有变化，setCurrentText 自身会触发 currentIndexChanged 从而调用 _emit。

    def _emit(self):
        self.valueChanged.emit(self.value())

    # 供外部设置分钟控件的最小宽度（用于对齐）
    def setMinuteMinWidth(self, w: int):
        self._minute_toggle.setMinimumWidth(w)


class MinuteToggle(QtWidgets.QWidget):
    """
    左右分栏的分钟开关：两个并列按钮，分别为“00 分”和“30 分”，互斥选中。
    """
    valueChanged = QtCore.Signal(str)  # "00" or "30"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.btn00 = QtWidgets.QPushButton("00")
        self.btn30 = QtWidgets.QPushButton("30")
        for b in (self.btn00, self.btn30):
            b.setCheckable(True)
            b.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.btn00.setChecked(True)

        self.group = QtWidgets.QButtonGroup(self)
        self.group.setExclusive(True)
        self.group.addButton(self.btn00, 0)
        self.group.addButton(self.btn30, 1)

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.btn00)
        lay.addWidget(self.btn30)

        # 分段按钮样式（类似 segmented control），选中高亮改为灰色
        self.setStyleSheet(
            """
            QPushButton { border: 1px solid #c8c8c8; padding: 6px 12px; font-size: 16px; }
            QPushButton:checked { background: #d0d0d0; color: #222; }
            QPushButton:!checked { background: #f7f7f7; color: #222; }
            QPushButton:first-child { border-top-left-radius: 6px; border-bottom-left-radius: 6px; }
            QPushButton:last-child { border-top-right-radius: 6px; border-bottom-right-radius: 6px; border-left: none; }
            """
        )

        self.group.idToggled.connect(self._on_toggled)

    def _on_toggled(self, _id: int, checked: bool):
        if not checked:
            return
        self.valueChanged.emit(self.value())

    def value(self) -> str:
        return "30" if self.btn30.isChecked() else "00"

    def setValue(self, v: str):
        v = (v or "00").strip()
        if v == "30":
            self.btn30.setChecked(True)
        else:
            self.btn00.setChecked(True)

    def setButtonHeight(self, h: int):
        try:
            h = int(h)
        except Exception:
            return
        for b in (self.btn00, self.btn30):
            b.setFixedHeight(h)
