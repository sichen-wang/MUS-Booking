# -*- coding: utf-8 -*-
"""
SettingsDialog对话框
从 GUI.py 提取的模块
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

# 导入配置管理
from config import AppConfig
from constants import DEFAULT_THEME


class SettingsDialog(QtWidgets.QDialog):
    saved = QtCore.Signal(object)  # AppConfig

    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setModal(True)
        self.cfg = cfg

        self.setMinimumWidth(520)

        # 字段
        def label(txt: str, required: bool = False):
            lab = QtWidgets.QLabel(txt + ("  " if not required else "  <span style='color:#e53935'>*</span>"))
            lab.setTextFormat(QtCore.Qt.RichText)
            return lab

        # 代理设置已移至 Cookie 对话框，这里不再显示

        self.le_uid = QtWidgets.QLineEdit(cfg.user_id)
        self.le_uid.setPlaceholderText("12XXXXXXX")

        self.le_pwd = QtWidgets.QLineEdit(cfg.user_password)
        self.le_pwd.setPlaceholderText("您的密码")
        self.le_pwd.setEchoMode(QtWidgets.QLineEdit.Password)

        self.le_uname = QtWidgets.QLineEdit(cfg.user_name)
        self.le_uname.setPlaceholderText("XXX")

        self.le_email = QtWidgets.QLineEdit(cfg.user_email)
        self.le_email.setPlaceholderText("example@link.cuhk.edu.cn")

        self.le_phone = QtWidgets.QLineEdit(cfg.user_phone)
        self.le_phone.setPlaceholderText("123456")

        self.le_theme = QtWidgets.QLineEdit(cfg.theme or DEFAULT_THEME)
        self.le_theme.setPlaceholderText(DEFAULT_THEME)

        form = QtWidgets.QFormLayout()
        form.addRow(label("学号", True), self.le_uid)
        form.addRow(label("密码", True), self.le_pwd)
        form.addRow(label("姓名", True), self.le_uname)
        form.addRow(label("邮箱", True), self.le_email)
        form.addRow(label("电话"), self.le_phone)
        form.addRow(label("预定主题"), self.le_theme)

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self.on_save)
        btns.rejected.connect(self.reject)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(btns)

    def on_save(self):
        # 校验必填项
        missing = []
        if not (self.le_uid.text().strip()):
            missing.append("user_id")
        if not (self.le_pwd.text().strip()):
            missing.append("user_password")
        if not (self.le_uname.text().strip()):
            missing.append("user_name")
        if not (self.le_email.text().strip()):
            missing.append("user_email")
        if missing:
            QtWidgets.QMessageBox.warning(self, "缺少必填项", "请填写：" + "、".join(missing))
            return

        # 填充并发射（proxies 已移至 Cookie 对话框，这里不再处理）
        self.cfg.user_id = self.le_uid.text().strip()
        self.cfg.user_password = self.le_pwd.text().strip()
        self.cfg.user_name = self.le_uname.text().strip()
        self.cfg.user_email = self.le_email.text().strip()
        self.cfg.user_phone = self.le_phone.text().strip()
        self.cfg.theme = self.le_theme.text().strip() or DEFAULT_THEME
        self.saved.emit(self.cfg)
        self.accept()
