# -*- coding: utf-8 -*-
"""
测试自动获取Cookie功能
"""

import sys
import os

# 添加 src 目录到 path 以便导入模块化结构
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

try:
    from PySide6 import QtWidgets
    from dialogs.auto_login_dialog import WEBENGINE_AVAILABLE, AutoLoginDialog
    from config import ConfigManager
    from utils import parse_proxies

    print("[OK] GUI module imported successfully")
    print(f"[INFO] QtWebEngine available: {WEBENGINE_AVAILABLE}")

    if not WEBENGINE_AVAILABLE:
        print("[WARNING] QtWebEngine is not available. Auto-login feature will not work.")
        print("[INFO] Please install: pip install PySide6-WebEngine")
        sys.exit(1)

    # 创建应用
    app = QtWidgets.QApplication(sys.argv)

    # 测试配置加载
    cfg = ConfigManager.load()
    print(f"[OK] Config loaded: proxies={cfg.proxies}")

    # 测试 AutoLoginDialog 创建
    print("[INFO] Creating AutoLoginDialog...")
    dlg = AutoLoginDialog(proxies_config=cfg.proxies)

    # 连接信号
    def on_cookie_captured(cookie_str):
        print(f"[OK] Cookie captured: {len(cookie_str)} characters")
        print(f"[INFO] Cookie preview: {cookie_str[:100]}...")
        dlg.close()
        app.quit()

    dlg.cookie_captured.connect(on_cookie_captured)

    print("[OK] AutoLoginDialog created successfully")
    print("[INFO] Browser window will open. Please login to test.")
    print("[INFO] The dialog will auto-close after successful login.")

    # 显示对话框
    dlg.show()

    sys.exit(app.exec())

except ImportError as e:
    print(f"[ERROR] Import failed: {e}")
    print("[INFO] Make sure you are in the correct directory")
    sys.exit(1)
except Exception as e:
    print(f"[ERROR] Unexpected error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
