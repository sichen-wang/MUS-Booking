# -*- coding: utf-8 -*-
"""
WheelCombo控件
从 GUI.py 提取的模块
"""

from __future__ import annotations

from typing import List

from PySide6 import QtCore, QtGui, QtWidgets


class WheelCombo(QtWidgets.QComboBox):
    """
    一个视觉上更接近“滚轮”的组合框：
      - 行高更大，当前值居中显示；
      - 支持鼠标滚轮连续调节；
      - 键盘上下键也可调整。
    这是“类似 iPhone 闹钟设定的纵向滚轮设计”的近似实现。
    """
    def __init__(self, items: List[str], parent=None):
        super().__init__(parent)
        self.setEditable(False)
        self.addItems(items)
        # 大字号 & 行高 + 中央高亮；下拉高度随项目数自适应（不至过长）
        approx_row_h = 28
        view_min_h = min(300, max(approx_row_h * min(len(items), 8), approx_row_h * 3))
        self.setStyleSheet(f"""
            QComboBox {{
                font-size: 16px; padding: 6px 12px;
            }}
            QComboBox QAbstractItemView {{
                selection-background-color: #3daee9;
                outline: 0;
                font-size: 16px;
                min-height: {view_min_h}px;
            }}
        """)
        self.setMaxVisibleItems(min(10, len(items)))
        self.setInsertPolicy(QtWidgets.QComboBox.NoInsert)

    def wheelEvent(self, e: QtGui.QWheelEvent) -> None:
        delta = e.angleDelta().y()
        if delta < 0:
            self.setCurrentIndex(min(self.currentIndex() + 1, self.count() - 1))
        elif delta > 0:
            self.setCurrentIndex(max(self.currentIndex() - 1, 0))
