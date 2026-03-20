# -*- coding: utf-8 -*-
"""
AutoLoginDialog对话框
从 GUI.py 提取的模块
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets, QtNetwork

try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage, QWebEngineSettings
    WEBENGINE_AVAILABLE = True
except ImportError:
    WEBENGINE_AVAILABLE = False

# 导入工具函数
from utils import parse_proxies


if WEBENGINE_AVAILABLE:
    class AutoLoginDialog(QtWidgets.QDialog):
        """自动登录并捕获Cookie的对话框"""
        cookie_captured = QtCore.Signal(str)  # 发送捕获到的Cookie字符串

        def __init__(self, proxies_config: str = "", user_id: str = "", user_password: str = "", parent=None):
            super().__init__(parent)
            self.setWindowTitle("自动登录 - CUHK 预订系统")
            self.setModal(True)
            self.resize(1024, 768)

            self.user_id = user_id
            self.user_password = user_password

            # 先创建 status_label（因为 _apply_proxy 会用到）
            self.status_label = QtWidgets.QLabel("正在初始化...")
            self.status_label.setStyleSheet("color: #666; font-size: 12px;")

            # ⚠️ 关键：在创建 Profile 之前先设置代理
            # QWebEngineProfile 创建时会使用当前的全局代理设置
            self._apply_proxy(proxies_config)

            # 创建独立的非持久化Profile（每次都是全新的，不保留Cookie）
            # 不作为子对象，避免删除顺序问题
            self.profile = QWebEngineProfile()

            # 启用开发者工具用于调试
            self.profile.settings().setAttribute(QWebEngineSettings.JavascriptEnabled, True)
            self.profile.settings().setAttribute(QWebEngineSettings.LocalStorageEnabled, True)

            # 清除所有Cookie（确保每次都是全新登录）
            self.cookie_store = self.profile.cookieStore()
            self.cookie_store.deleteAllCookies()

            # 创建自定义Page使用独立Profile
            self.page = QWebEnginePage(self.profile, self)

            # 创建WebEngineView并设置Page
            self.browser = QWebEngineView()
            self.browser.setPage(self.page)

            # UI组件（创建其他UI组件）
            self.url_bar = QtWidgets.QLineEdit()
            self.url_bar.setReadOnly(True)
            self.url_bar.setStyleSheet("padding: 4px; font-size: 12px;")

            self.progress_bar = QtWidgets.QProgressBar()
            self.progress_bar.setMaximumHeight(4)
            self.progress_bar.setTextVisible(False)
            self.progress_bar.setStyleSheet("""
                QProgressBar { border: none; background: #f0f0f0; }
                QProgressBar::chunk { background: #3daee9; }
            """)

            self.btn_refresh = QtWidgets.QPushButton("刷新")
            self.btn_refresh.setMaximumWidth(80)

            self.btn_devtools = QtWidgets.QPushButton("开发者工具")
            self.btn_devtools.setMaximumWidth(100)

            self.btn_capture = QtWidgets.QPushButton("手动捕获Cookie并关闭")
            self.btn_capture.setStyleSheet("font-size: 14px; padding: 8px;")

            # 布局
            self._setup_ui()

            # 初始化状态标志
            self.captured_cookies = {}
            self._auto_captured = False
            self._auto_filled = False
            self._fill_retry_count = 0
            self._progressive_form_state = 'init'
            self.devtools_window = None
            self._page_load_timeout_timer = None
            self._auto_capture_fallback_timer = None  # Cookie自动捕获保险计时器

            # 信号连接（在加载页面之前）
            self._connect_signals()

            # Cookie监听
            self.cookie_store.cookieAdded.connect(self._on_cookie_added)

            # 立即开始加载页面（不延迟，因为代理已经设置好了）
            self.status_label.setText("正在连接到预订系统...")
            self.browser.setUrl(QtCore.QUrl("https://booking.cuhk.edu.cn"))

            # 设置页面加载超时（30秒）
            self._page_load_timeout_timer = QtCore.QTimer(self)
            self._page_load_timeout_timer.setSingleShot(True)
            self._page_load_timeout_timer.timeout.connect(self._on_page_load_timeout)
            self._page_load_timeout_timer.start(30000)

            # 设置Cookie自动捕获保险计时器（30秒后，如果还没自动捕获且有Cookie，就强制捕获）
            self._auto_capture_fallback_timer = QtCore.QTimer(self)
            self._auto_capture_fallback_timer.setSingleShot(True)
            self._auto_capture_fallback_timer.timeout.connect(self._fallback_auto_capture)
            self._auto_capture_fallback_timer.start(30000)  # 30秒保险

        def closeEvent(self, event):
            """对话框关闭时，确保正确的资源释放顺序"""
            # 停止所有计时器
            if self._page_load_timeout_timer and self._page_load_timeout_timer.isActive():
                self._page_load_timeout_timer.stop()

            if self._auto_capture_fallback_timer and self._auto_capture_fallback_timer.isActive():
                self._auto_capture_fallback_timer.stop()

            # 断开 Cookie 监听信号，防止关闭后回调
            try:
                self.cookie_store.cookieAdded.disconnect(self._on_cookie_added)
            except (RuntimeError, TypeError):
                pass

            # 先断开 Browser 和 Page 的连接
            self.browser.setPage(None)

            # 删除 Page（必须在 Profile 之前删除）
            if hasattr(self, 'page') and self.page:
                self.page.deleteLater()
                self.page = None

            # 延迟删除 Profile（确保 Page 已经完全删除）
            if hasattr(self, 'profile') and self.profile:
                profile_to_delete = self.profile
                self.profile = None
                QtCore.QTimer.singleShot(100, profile_to_delete.deleteLater)

            super().closeEvent(event)

        def _on_page_load_timeout(self):
            """页面加载超时处理"""
            if self.progress_bar.value() < 100:
                self.status_label.setText("页面加载超时，请检查网络连接")
                QtWidgets.QMessageBox.warning(
                    self, "加载超时",
                    "页面加载超时，可能的原因：\n"
                    "1. 网络连接问题\n"
                    "2. 代理服务器未启动或配置错误\n"
                    "3. VPN未连接\n\n"
                    "请检查后重试或点击「刷新」按钮。"
                )

        def _apply_proxy(self, proxies_config: str):
            """应用代理设置（或明确禁用代理）"""
            try:
                # 如果没有配置代理，明确禁用代理（直接连接）
                if not proxies_config or not proxies_config.strip():
                    proxy = QtNetwork.QNetworkProxy()
                    proxy.setType(QtNetwork.QNetworkProxy.NoProxy)  # 🔑 关键：明确设置为无代理
                    QtNetwork.QNetworkProxy.setApplicationProxy(proxy)
                    # 只有在 status_label 存在时才设置文本
                    if hasattr(self, 'status_label'):
                        self.status_label.setText("使用直接连接（无代理）")
                    print("[AutoLoginDialog] 已设置为直接连接（NoProxy）")
                    return

                # 如果配置了代理，解析并应用
                proxies = parse_proxies(proxies_config)
                if proxies and "http" in proxies:
                    proxy_url = proxies["http"].replace("http://", "").replace("https://", "")
                    if ":" in proxy_url:
                        host, port = proxy_url.split(":", 1)
                        proxy = QtNetwork.QNetworkProxy()
                        proxy.setType(QtNetwork.QNetworkProxy.HttpProxy)
                        proxy.setHostName(host)
                        proxy.setPort(int(port))
                        QtNetwork.QNetworkProxy.setApplicationProxy(proxy)
                        # 只有在 status_label 存在时才设置文本
                        if hasattr(self, 'status_label'):
                            self.status_label.setText(f"已应用代理: {host}:{port}")
                        print(f"[AutoLoginDialog] 已应用代理: {host}:{port}")
                else:
                    # 配置格式错误，禁用代理
                    proxy = QtNetwork.QNetworkProxy()
                    proxy.setType(QtNetwork.QNetworkProxy.NoProxy)
                    QtNetwork.QNetworkProxy.setApplicationProxy(proxy)
                    # 只有在 status_label 存在时才设置文本
                    if hasattr(self, 'status_label'):
                        self.status_label.setText("代理配置无效，使用直接连接")
                    print("[AutoLoginDialog] 代理配置无效，已设置为直接连接")
            except Exception as e:
                # 出错时也禁用代理
                print(f"[Warning] Failed to apply proxy: {e}, using direct connection")
                proxy = QtNetwork.QNetworkProxy()
                proxy.setType(QtNetwork.QNetworkProxy.NoProxy)
                QtNetwork.QNetworkProxy.setApplicationProxy(proxy)
                # 只有在 status_label 存在时才设置文本
                if hasattr(self, 'status_label'):
                    self.status_label.setText("代理设置失败，使用直接连接")

        def _setup_ui(self):
            """布局设置"""
            # 顶部工具栏
            toolbar = QtWidgets.QHBoxLayout()
            toolbar.addWidget(QtWidgets.QLabel("地址:"))
            toolbar.addWidget(self.url_bar, 1)
            toolbar.addWidget(self.btn_refresh)
            toolbar.addWidget(self.btn_devtools)

            # 状态栏
            statusbar = QtWidgets.QHBoxLayout()
            statusbar.addWidget(self.status_label)
            statusbar.addStretch()

            # 主布局
            layout = QtWidgets.QVBoxLayout(self)
            layout.addLayout(toolbar)
            layout.addWidget(self.progress_bar)
            layout.addWidget(self.browser, 1)
            layout.addLayout(statusbar)
            layout.addWidget(self.btn_capture)

        def _connect_signals(self):
            """信号槽连接"""
            self.browser.urlChanged.connect(self._on_url_changed)
            self.browser.loadProgress.connect(self._on_load_progress)
            self.browser.loadFinished.connect(self._on_load_finished)
            self.btn_refresh.clicked.connect(self.browser.reload)
            self.btn_devtools.clicked.connect(self._open_devtools)
            self.btn_capture.clicked.connect(self._capture_and_close)

        def _on_cookie_added(self, cookie):
            """Cookie添加时的回调"""
            domain = cookie.domain()
            if "cuhk.edu.cn" in domain:
                try:
                    name = bytes(cookie.name()).decode('utf-8')
                    value = bytes(cookie.value()).decode('utf-8')
                    self.captured_cookies[name] = value
                    print(f"[AutoLogin] Cookie已捕获: {name}")

                    # 当捕获到足够的Cookie时（至少5个），尝试自动捕获
                    if len(self.captured_cookies) >= 5 and not self._auto_captured:
                        print(f"[AutoLogin] 已捕获 {len(self.captured_cookies)} 个Cookie，准备自动验证...")
                        QtCore.QTimer.singleShot(1000, self._auto_capture)
                except Exception as e:
                    print(f"[Warning] Failed to decode cookie: {e}")

        def _on_url_changed(self, url):
            """URL变化时更新地址栏并检查是否登录成功"""
            url_str = url.toString()
            self.url_bar.setText(url_str)
            print(f"[AutoLogin] URL变化: {url_str}")

            # 检测登录成功的URL特征（扩展的智能登录检测）
            success_patterns = [
                "/field/client/main",
                "/field/book",
                "/sso/code",
                "/a/field/",
                "booking.cuhk.edu.cn/a/",
                "jsessionid=",  # URL中包含session ID
            ]

            is_login_success = any(pattern.lower() in url_str.lower() for pattern in success_patterns)

            if not self._auto_captured and is_login_success:
                print(f"[AutoLogin] 检测到登录成功URL: {url_str}")
                self.status_label.setText("检测到登录成功，正在捕获Cookie...")
                # 延迟2秒等待Cookie完全加载，然后自动捕获
                QtCore.QTimer.singleShot(2000, self._auto_capture)
            elif not self._auto_filled and self.user_id and self.user_password:
                # URL变化时也尝试自动填写（可能跳转到了登录页）
                # 延迟1秒确保页面加载完成
                self._fill_retry_count = 0  # 重置重试计数
                QtCore.QTimer.singleShot(1000, self._try_auto_fill_form)

        def _on_load_progress(self, progress):
            """更新加载进度"""
            self.progress_bar.setValue(progress)
            if progress < 100:
                self.status_label.setText(f"加载中... {progress}%")

        def _on_load_finished(self, ok):
            """页面加载完成/失败"""
            # 停止超时计时器
            if self._page_load_timeout_timer and self._page_load_timeout_timer.isActive():
                self._page_load_timeout_timer.stop()

            if ok:
                self.progress_bar.setValue(100)
                self.status_label.setText("页面加载完成")

                # 如果有用户名密码，且未自动填写过，尝试自动填写
                # 延迟1秒确保页面JavaScript完全执行
                if self.user_id and self.user_password and not self._auto_filled:
                    self.status_label.setText("页面加载完成，准备自动填写表单...")
                    QtCore.QTimer.singleShot(1000, self._try_auto_fill_form)
            else:
                self.progress_bar.setValue(0)
                error_msg = "页面加载失败，请检查：\n" \
                            "1. 网络连接是否正常\n" \
                            "2. 代理服务器是否可用\n" \
                            "3. 是否在校园网内"
                self.status_label.setText("加载失败")
                QtWidgets.QMessageBox.warning(self, "加载错误", error_msg)

        def _try_auto_fill_form(self):
            """尝试自动填写登录表单（支持渐进式表单）"""
            if self._auto_filled:
                return

            # JavaScript代码：支持渐进式表单的自动填写
            js_code = f"""
            (function() {{
                console.log('[AutoLogin] Starting form auto-fill (state: {self._progressive_form_state})...');
                console.log('[AutoLogin] Current URL: ' + window.location.href);

                // 步骤1：查找并点击"Login"按钮（如果表单未显示）
                if ('{self._progressive_form_state}' === 'init') {{
                    console.log('[AutoLogin] Step 1: Looking for Login button...');
                    var loginButtons = document.querySelectorAll('button, a, div[role="button"], span[role="button"]');
                    for (var i = 0; i < loginButtons.length; i++) {{
                        var btnText = (loginButtons[i].textContent || loginButtons[i].innerText || '').toLowerCase().trim();
                        console.log('[AutoLogin] Button [' + i + ']: "' + btnText + '"');
                        if (btnText === 'login' || btnText === '登录' || btnText === 'log in') {{
                            console.log('[AutoLogin] Found Login button, clicking...');
                            loginButtons[i].click();
                            return 'clicked_login';
                        }}
                    }}
                    console.log('[AutoLogin] Login button not found, looking for input fields...');
                }}

                // 步骤2：查找并填写用户名输入框
                var usernameField = document.querySelector('input[type="text"], input[type="email"], input[placeholder*="账号"], input[placeholder*="用户名"], input[placeholder*="学号"], input:not([type="password"]):not([type="hidden"]):not([type="submit"])');
                var passwordField = document.querySelector('input[type="password"]');

                console.log('[AutoLogin] Username field found: ' + (usernameField !== null));
                console.log('[AutoLogin] Password field found: ' + (passwordField !== null));

                // 如果只找到用户名框（没有密码框），先填写用户名
                if (usernameField && !passwordField && '{self._progressive_form_state}' !== 'filled_username') {{
                    console.log('[AutoLogin] Step 2: Filling username only...');

                    // 先聚焦输入框
                    usernameField.focus();
                    usernameField.value = '{self.user_id}';

                    // 触发所有必要的事件
                    ['input', 'change', 'blur', 'keyup'].forEach(function(eventType) {{
                        usernameField.dispatchEvent(new Event(eventType, {{ bubbles: true }}));
                    }});

                    console.log('[AutoLogin] Username filled: "' + usernameField.value + '"');

                    // 再次聚焦确保焦点正确
                    usernameField.focus();
                    usernameField.select();  // 选中文本

                    console.log('[AutoLogin] Looking for buttons on page...');

                    // 查找所有可能的按钮并输出它们的文本
                    var allButtons = document.querySelectorAll('button, a[href], div[onclick], span[onclick], input[type="submit"], input[type="button"]');
                    console.log('[AutoLogin] Found ' + allButtons.length + ' total buttons/links');

                    for (var i = 0; i < allButtons.length; i++) {{
                        var elem = allButtons[i];
                        var btnText = (elem.textContent || elem.innerText || elem.value || '').trim();
                        var btnHref = elem.href || '';
                        var btnOnclick = elem.onclick ? 'has onclick' : '';
                        console.log('[AutoLogin] Button [' + i + ']: text="' + btnText + '", tag=' + elem.tagName + ', href=' + btnHref + ', ' + btnOnclick);

                        // 匹配更多可能的按钮文本（中英文、大小写不敏感）
                        var lowerText = btnText.toLowerCase();
                        if (lowerText.includes('next') || lowerText.includes('下一步') || lowerText.includes('继续') ||
                            lowerText.includes('continue') || lowerText.includes('确定') || lowerText.includes('ok') ||
                            lowerText.includes('登录') || lowerText.includes('login') || lowerText.includes('提交') ||
                            lowerText.includes('submit') || lowerText.includes('进入') || lowerText.includes('enter')) {{
                            console.log('[AutoLogin] ✓ Found matching button! Clicking: "' + btnText + '"');
                            elem.click();
                            return 'filled_username_with_button';
                        }}
                    }}

                    console.log('[AutoLogin] No matching button found. Will press Enter key.');
                    return 'filled_username_need_enter';
                }}

                // 步骤3：如果同时有用户名和密码框，填写两者
                if (usernameField && passwordField) {{
                    console.log('[AutoLogin] Step 3: Filling both username and password...');

                    // 先填写用户名
                    usernameField.focus();
                    usernameField.value = '{self.user_id}';
                    ['input', 'change', 'blur', 'keyup'].forEach(function(eventType) {{
                        usernameField.dispatchEvent(new Event(eventType, {{ bubbles: true }}));
                    }});

                    // 再填写密码
                    passwordField.focus();
                    passwordField.value = '{self.user_password}';
                    ['input', 'change', 'blur', 'keyup'].forEach(function(eventType) {{
                        passwordField.dispatchEvent(new Event(eventType, {{ bubbles: true }}));
                    }});

                    console.log('[AutoLogin] Both fields filled: username="' + usernameField.value + '", password=' + ('*'.repeat(passwordField.value.length)));

                    // 再次聚焦密码框并选中
                    passwordField.focus();
                    passwordField.select();  // 选中文本

                    console.log('[AutoLogin] Looking for all buttons on page...');

                    // 查找所有可能的按钮（扩大范围）
                    var allButtons = document.querySelectorAll('button, a[href], div[onclick], span[onclick], input[type="submit"], input[type="button"], [role="button"]');
                    console.log('[AutoLogin] Found ' + allButtons.length + ' total buttons/links');

                    for (var i = 0; i < allButtons.length; i++) {{
                        var elem = allButtons[i];
                        var btnText = (elem.textContent || elem.innerText || elem.value || '').trim();
                        var btnId = elem.id || '';
                        var btnClass = elem.className || '';

                        console.log('[AutoLogin] Button [' + i + ']: text="' + btnText + '", id="' + btnId + '", class="' + btnClass + '"');

                        // 匹配登录按钮（检查文本、ID、class）
                        var lowerText = btnText.toLowerCase();
                        var lowerIdClass = (btnId + ' ' + btnClass).toLowerCase();

                        if (lowerText.includes('登录') || lowerText.includes('login') ||
                            lowerText.includes('sign in') || lowerText.includes('提交') ||
                            lowerText.includes('submit') || elem.type === 'submit' ||
                            lowerIdClass.includes('login') || lowerIdClass.includes('submit')) {{
                            console.log('[AutoLogin] ✓ Found Login button! Clicking: "' + btnText + '" (id=' + btnId + ')');
                            elem.click();
                            return 'filled_both_with_button';
                        }}
                    }}

                    console.log('[AutoLogin] No Login button found. Will press Enter key.');
                    return 'filled_both_need_enter';
                }}

                console.log('[AutoLogin] No actionable elements found');
                return 'not_found';
            }})();
            """

            def on_result(result):
                if result == 'clicked_login':
                    self._progressive_form_state = 'clicked_login'
                    self.status_label.setText('已点击Login按钮，等待表单加载...')
                    QtCore.QTimer.singleShot(1000, self._try_auto_fill_form)

                elif result == 'filled_username_with_button':
                    self._progressive_form_state = 'filled_username'
                    self.status_label.setText('已填写学号并点击"下一步"按钮，等待密码框出现...')
                    # 等待1秒后检查密码框是否出现
                    QtCore.QTimer.singleShot(1000, self._try_auto_fill_form)

                elif result == 'filled_username_need_enter':
                    self._progressive_form_state = 'filled_username'
                    self.status_label.setText('已填写学号，正在按回车键...')
                    # 延迟500ms后按第一次回车
                    QtCore.QTimer.singleShot(500, lambda: self._press_enter_key(2))

                elif result == 'filled_both_with_button':
                    self._auto_filled = True
                    self._progressive_form_state = 'completed'
                    self.status_label.setText('已填写学号和密码并点击"登录"按钮，等待登录完成...')
                    # 不需要额外操作，等待URL变化自动捕获Cookie

                elif result == 'filled_both_need_enter':
                    self._auto_filled = True
                    self._progressive_form_state = 'completed'
                    self.status_label.setText('已填写学号和密码，正在按回车键登录...')
                    # 延迟500ms后按回车
                    QtCore.QTimer.singleShot(500, lambda: self._press_enter_key(1))

                else:
                    # 未能找到元素时，延迟2秒后重试，最多重试5次
                    self._fill_retry_count += 1
                    if self._fill_retry_count <= 5 and not self._auto_filled:
                        self.status_label.setText(f"未找到可操作元素，{2}秒后重试...（第{self._fill_retry_count}/5次）")
                        QtCore.QTimer.singleShot(2000, self._try_auto_fill_form)
                    else:
                        self.status_label.setText("自动登录失败，请手动操作")

            self.page.runJavaScript(js_code, on_result)

        def _press_enter_key(self, count=1):
            """按回车键（支持多次）"""
            js_code = """
            (function() {{
                console.log('[AutoLogin] Pressing Enter key...');
                var activeElement = document.activeElement;

                if (activeElement && (activeElement.tagName === 'INPUT' || activeElement.tagName === 'TEXTAREA')) {{
                    console.log('[AutoLogin] Active element is: ' + activeElement.tagName + ', type: ' + activeElement.type);

                    // 确保输入框被选中
                    activeElement.select();

                    // 按回车键
                    activeElement.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }}));
                    activeElement.dispatchEvent(new KeyboardEvent('keypress', {{ key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }}));
                    activeElement.dispatchEvent(new KeyboardEvent('keyup', {{ key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }}));

                    console.log('[AutoLogin] Enter key pressed on ' + activeElement.type);
                    return 'pressed';
                }} else {{
                    console.log('[AutoLogin] No active input element found');
                    if (activeElement) {{
                        console.log('[AutoLogin] Current active element: ' + activeElement.tagName);
                    }}
                    return 'no_active_element';
                }}
            }})();
            """

            def on_enter_pressed(result):
                if result == 'pressed':
                    if count > 1:
                        # 如果需要按多次，延迟500ms后继续按
                        self.status_label.setText(f'已按第{2 - count + 1}次回车，准备按第{2 - count + 2}次...')
                        QtCore.QTimer.singleShot(500, lambda: self._press_enter_key(count - 1))
                    else:
                        self.status_label.setText('已按回车，等待登录完成...')
                        # 标记为已填写，避免重复填写
                        self._auto_filled = True
                else:
                    print('[AutoLogin] Failed to press Enter - no active input element')
                    self.status_label.setText('按回车失败，焦点不在输入框上')

            self.page.runJavaScript(js_code, on_enter_pressed)

        def _validate_cookie(self, cookie_dict: dict) -> bool:
            """验证Cookie是否完整（必须包含关键认证字段）"""
            print(f"[AutoLogin] 验证Cookie，当前有 {len(cookie_dict)} 个: {list(cookie_dict.keys())}")

            # 关键认证字段（必须至少有一个）
            critical_auth_fields = ["MSISAuth", "MSISAuthenticated", "JSESSIONID"]

            # 检查是否有关键认证字段
            has_critical_auth = False
            for field in critical_auth_fields:
                if field in cookie_dict:
                    print(f"[AutoLogin] [OK] 找到关键认证Cookie: {field}")
                    has_critical_auth = True
                    break

            if not has_critical_auth:
                print(f"[AutoLogin] [FAIL] 缺少关键认证Cookie (MSISAuth/MSISAuthenticated/JSESSIONID)")
                return False

            # 检查Cookie数量（至少需要5个Cookie才算完整）
            if len(cookie_dict) < 5:
                print(f"[AutoLogin] [FAIL] Cookie数量不足 ({len(cookie_dict)}/5)")
                return False

            print(f"[AutoLogin] [OK] Cookie验证通过！共{len(cookie_dict)}个")
            return True

        def _auto_capture(self):
            """自动捕获Cookie（严格验证，确保完整性）"""
            if self._auto_captured:
                print("[AutoLogin] 已经捕获过Cookie，跳过")
                return

            print(f"[AutoLogin] 尝试自动捕获，当前Cookie数量: {len(self.captured_cookies)}")

            # 严格验证：必须有关键认证字段且至少5个Cookie
            if self.captured_cookies and len(self.captured_cookies) >= 5:
                if self._validate_cookie(self.captured_cookies):
                    cookie_str = "; ".join([f"{k}={v}" for k, v in self.captured_cookies.items()])
                    self._auto_captured = True
                    print(f"[AutoLogin] Cookie捕获成功！共{len(self.captured_cookies)}个Cookie，{len(cookie_str)}个字符")
                    self.status_label.setText(f"Cookie捕获成功！共{len(self.captured_cookies)}个，正在关闭...")
                    self.cookie_captured.emit(cookie_str)
                    # 延迟200ms后自动关闭对话框，让用户看到成功提示
                    QtCore.QTimer.singleShot(200, self.accept)
                else:
                    print("[AutoLogin] Cookie验证失败，等待关键认证Cookie")
                    self.status_label.setText("Cookie不完整，等待关键认证字段...")
            else:
                print(f"[AutoLogin] Cookie数量不足({len(self.captured_cookies)}/5)，等待中...")
                self.status_label.setText(f"正在收集Cookie...({len(self.captured_cookies)}/5)")

        def _fallback_auto_capture(self):
            """保险机制：30秒后强制捕获Cookie（如果还没自动捕获）"""
            if self._auto_captured:
                print("[AutoLogin] Cookie已自动捕获，跳过保险机制")
                return

            if len(self.captured_cookies) >= 5:
                print("[AutoLogin] 保险机制触发：强制捕获Cookie")
                self.status_label.setText("正在强制捕获Cookie...")
                self._auto_capture()
            else:
                print(f"[AutoLogin] 保险机制触发但Cookie不足({len(self.captured_cookies)}/5)")
                self.status_label.setText(f"Cookie数量不足({len(self.captured_cookies)}/5)，请等待或手动操作")

        def _capture_and_close(self):
            """手动捕获Cookie并关闭对话框"""
            if not self.captured_cookies:
                QtWidgets.QMessageBox.warning(
                    self, "未检测到Cookie",
                    "请先登录预订系统，确保已成功访问 booking.cuhk.edu.cn"
                )
                return

            if not self._validate_cookie(self.captured_cookies):
                QtWidgets.QMessageBox.warning(
                    self, "Cookie不完整",
                    "捕获的Cookie缺少必要字段，可能是因为：\n"
                    "1. 尚未成功登录\n"
                    "2. 登录已过期\n\n"
                    "请重新登录后再试。"
                )
                return

            # 拼接Cookie字符串
            cookie_str = "; ".join([f"{k}={v}" for k, v in self.captured_cookies.items()])
            self.cookie_captured.emit(cookie_str)
            self.accept()

        def _open_devtools(self):
            """打开开发者工具窗口"""
            if not hasattr(self, 'devtools_window') or not self.devtools_window:
                # 创建开发者工具窗口
                self.devtools_window = QtWidgets.QDialog(self)
                self.devtools_window.setWindowTitle("开发者工具")
                self.devtools_window.resize(1024, 600)

                # 创建开发者工具视图
                self.devtools_view = QWebEngineView()

                # 设置页面的开发者工具页面
                self.page.setDevToolsPage(self.devtools_view.page())

                # 布局
                layout = QtWidgets.QVBoxLayout(self.devtools_window)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.addWidget(self.devtools_view)

                # 添加说明标签
                info_label = QtWidgets.QLabel("提示：在Console标签中可以看到 [AutoLogin] 开头的调试日志")
                info_label.setStyleSheet("background: #ffffcc; padding: 8px; font-size: 12px;")
                layout.insertWidget(0, info_label)

            # 显示窗口
            self.devtools_window.show()
            self.devtools_window.raise_()
            self.devtools_window.activateWindow()
            self.status_label.setText("开发者工具已打开，请查看Console标签")
else:
    # 如果 QtWebEngine 不可用，创建一个占位类
    class AutoLoginDialog:
        def __init__(self, *args, **kwargs):
            raise ImportError("QtWebEngine 模块不可用，无法使用自动登录功能。请安装：pip install PySide6-WebEngine")
