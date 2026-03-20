# -*- coding: utf-8 -*-
"""
RequestItemWidget控件
从 GUI.py 提取的模块
"""

from __future__ import annotations

from datetime import datetime

from PySide6 import QtCore, QtGui, QtWidgets

# 导入常量
from constants import PLACES

# 导入工具函数
from utils import hhmm_to_minutes, minutes_to_hhmm

# 导入配置管理
from config import RequestItemData

# 导入其他控件
from widgets.date_wheel import DateWheel
from widgets.time_wheel import TimeWheel


class RequestItemWidget(QtWidgets.QFrame):
    removed = QtCore.Signal(object)  # self
    changed = QtCore.Signal()

    def __init__(self, data: RequestItemData | None = None, parent=None):
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setObjectName("RequestItem")
        self.setStyleSheet("""
            QFrame#RequestItem {
                border: 1px solid #dadada; border-radius: 8px; padding: 4px;
                background: #fcfcfc;
            }
            QComboBox { min-height: 26px; }
            QToolButton { min-height: 24px; min-width: 24px; }
        """)
        self.data = data or RequestItemData()

        # 地点：可编辑下拉 + 自动补全
        self.place = QtWidgets.QComboBox()
        self.place.setEditable(True)
        self.place.addItems(PLACES)
        self.place.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        completer = QtWidgets.QCompleter(PLACES, self.place)
        completer.setCaseSensitivity(QtCore.Qt.CaseInsensitive)
        self.place.setCompleter(completer)
        self.place.setCurrentText(self.data.place)

        # 日期轮（默认明天）
        d = datetime.strptime(self.data.date, "%Y-%m-%d").date()
        self.date_wheel = DateWheel(default=d)

        # 双圈时间轮盘（开始/结束）
        self.start_wheel = TimeWheel(default=self.data.start)
        self.end_wheel = TimeWheel(default=self.data.end)
        # 加长未展开时的显示区（小时/分钟开关）
        for w in (self.start_wheel, self.end_wheel):
            w.hh.setMinimumWidth(84)
            w.setMinuteMinWidth(120)

        # 取消时间预览（根据需求）
        self.summary = None

        # 使开始/结束时间控件尺寸一致
        same_w = max(self.start_wheel.sizeHint().width(), self.end_wheel.sizeHint().width()) + 100
        self.start_wheel.setFixedWidth(same_w)
        self.end_wheel.setFixedWidth(same_w)

        self.btn_remove = QtWidgets.QToolButton()
        self.btn_remove.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DialogCancelButton))
        self.btn_remove.setToolTip("删除该组请求")

        # 布局
        g = QtWidgets.QGridLayout(self)
        g.setContentsMargins(4, 4, 4, 4)
        g.setHorizontalSpacing(6)
        g.setVerticalSpacing(2)
        r = 0
        _lbl_place = QtWidgets.QLabel("地点")
        _lbl_place.setStyleSheet("font-size:16px; font-weight:700;")
        g.addWidget(_lbl_place, r, 0, 1, 1)
        g.addWidget(self.place, r, 1, 1, 3)
        g.addWidget(self.btn_remove, r, 4, 1, 1, QtCore.Qt.AlignRight)
        r += 1

        _lbl_date = QtWidgets.QLabel("日期")
        _lbl_date.setStyleSheet("font-size:16px; font-weight:700;")
        g.addWidget(_lbl_date, r, 0)
        g.addWidget(self.date_wheel, r, 1, 1, 4)
        r += 1

        _lbl_start = QtWidgets.QLabel("开始时间")
        _lbl_start.setStyleSheet("font-size:16px; font-weight:700;")
        g.addWidget(_lbl_start, r, 0)
        g.addWidget(self.start_wheel, r, 1, 1, 2)
        _lbl_end = QtWidgets.QLabel("结束时间")
        _lbl_end.setStyleSheet("font-size:16px; font-weight:700;")
        g.addWidget(_lbl_end, r, 3)
        g.addWidget(self.end_wheel, r, 4, 1, 1)
        r += 1

        # 预览行取消
        # g.addWidget(QtWidgets.QLabel("预览"), r, 0)
        # g.addWidget(self.summary, r, 1, 1, 4)

        # 信号
        self.btn_remove.clicked.connect(lambda: self.removed.emit(self))
        self.place.currentTextChanged.connect(self._on_changed)
        self.date_wheel.valueChanged.connect(self._on_changed)
        self.start_wheel.valueChanged.connect(self._on_changed)
        self.end_wheel.valueChanged.connect(self._on_changed)

        self._on_changed()

    def to_data(self) -> RequestItemData:
        return RequestItemData(
            place=self.place.currentText().strip(),
            date=self.date_wheel.value(),
            start=self.start_wheel.value(),
            end=self.end_wheel.value(),
        )

    def _on_changed(self, *args):
        dd = self.to_data()
        # 校验：06:00~23:00 且 开始<=结束
        s = hhmm_to_minutes(dd.start)
        e = hhmm_to_minutes(dd.end)
        s = max(s, 6*60)
        e = min(e, 23*60)
        if e < s:
            e = s
        # 回写 wheel
        self.start_wheel.setValue(minutes_to_hhmm(s))
        self.end_wheel.setValue(minutes_to_hhmm(e))
        self.changed.emit()
