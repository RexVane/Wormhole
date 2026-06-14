"""
pet.py
======
桌宠虫洞挂件(PySide6 + QML)。

形态：黑洞吞噬感 —— 中心深邃黑点 + 乳白色吸积盘/光晕，向内吸卷旋转；
      桌面小图标大小，低调浮在角落，无边框、透明、置顶、可拖动。

交互：
  - 从桌面拖文件到挂件上 -> 黑洞放大"吸入"动画 -> 调 WormholeSync.send_file 上传。
  - 收到对端文件(sync.on_received 回调) -> 黑洞放大"喷出"动画(文件已落在收件箱)。
  - 鼠标拖动窗口可挪到桌面任意位置；右键菜单可退出。

后端：复用 sync.WormholeSync(纯后台同步)。本文件只负责"面子"(动画/拖拽)，
      "里子"(传输)全交给同步引擎。两层解耦，同步引擎已通过自动化测试。

运行(需在有图形界面的机器上，先 pip install PySide6)：
  PYTHONPATH=src python3 -m pyftp_server.wormhole.pet --host 192.168.1.10
"""

from __future__ import annotations
import os
import sys
import argparse

from .sync import WormholeSync, WormholeConfig

def _qml_path() -> str:
    """定位 wormhole.qml。

    源码运行时它就在本模块同级目录；被 PyInstaller 打包成单文件后,数据文件
    会解压到临时目录 sys._MEIPASS,需按打包时的相对路径(pyftp_server/wormhole/)
    去那里找。两种环境都覆盖,打包/源码运行同一份代码。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        bundled = os.path.join(base, "pyftp_server", "wormhole", "wormhole.qml")
        if os.path.exists(bundled):
            return bundled
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "wormhole.qml")


_QML_FILE = _qml_path()


def _default_inbox() -> str:
    """默认收件箱目录,按平台给出常用位置(均可被 --inbox 覆盖)。

    Windows: ~/OneDrive/Desktop/wormhole  (本机即 C:\\Users\\guica\\OneDrive\\Desktop\\wormhole)
    macOS:   ~/Documents/wormhole         (本机即 /Users/kaijimima/Documents/wormhole)
    其他:    ~/Wormhole/收件箱
    """
    if sys.platform == "win32":
        return os.path.expanduser(os.path.join("~", "OneDrive", "Desktop", "wormhole"))
    if sys.platform == "darwin":
        return os.path.expanduser(os.path.join("~", "Documents", "wormhole"))
    return os.path.expanduser(os.path.join("~", "Wormhole", "收件箱"))


def _build_config(argv=None):
    ap = argparse.ArgumentParser(description="虫洞桌宠挂件")
    ap.add_argument("--host", default="127.0.0.1", help="FTP 服务器地址(局域网填服务器内网IP)")
    ap.add_argument("--port", type=int, default=2121)
    ap.add_argument("--user", default="wormhole")
    ap.add_argument("--password", default="wormhole")
    ap.add_argument("--inbox", default=_default_inbox(),
                    help="收件箱目录(收到的文件放这;默认随平台,见 --help)")
    ap.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒")
    ap.add_argument("--no-burn", action="store_true",
                    help="关闭阅后即焚(>2台设备时使用，保留中转副本供其他设备下载)")
    ap.add_argument("--tls", action="store_true", help="FTPS 加密连接(服务器需 --tls 启动)")
    ap.add_argument("--secret", default="", help="端到端加密口令(两台电脑必须一致；需 cryptography 库)")
    ap.add_argument("--size", type=int, default=0,
                    help="挂件边长像素(0=随屏幕自适应，约为系统图标基准的 1.5 倍)")
    args = ap.parse_args(argv)
    cfg = WormholeConfig(host=args.host, port=args.port, user=args.user,
                         password=args.password, inbox=args.inbox, interval=args.interval,
                         burn=not args.no_burn, tls=args.tls, secret=args.secret)
    return cfg, args.size


def _adaptive_pet_size(override: int) -> int:
    """挂件边长 = 系统程序图标尺寸 × 1.5。
    override>0 时直接用该值；否则在 macOS 上读取 Dock 实际图标尺寸
    (com.apple.dock tilesize)作为基准——用户把 Dock 图标调大，挂件随之变大。
    读不到(或非 macOS)则回退到常见图标基准 64。"""
    if override > 0:
        return override
    icon_base = 64
    if sys.platform == "darwin":
        try:
            import subprocess
            out = subprocess.run(["defaults", "read", "com.apple.dock", "tilesize"],
                                 capture_output=True, text=True, timeout=3)
            icon_base = int(float(out.stdout.strip()))
        except Exception:
            icon_base = 64
    return round(icon_base * 1.5)


def _install_crash_log(inbox: str) -> str:
    """让 GUI 版"莫名自己退出"可排查 + 兜底修复打包后的致命陷阱。

    打包成 windowed exe(spec 里 console=False)后,sys.stdout/sys.stderr 会变成
    None;此时任何 print() 都会抛异常,而 sync 后台线程每次状态变化/重连都 print,
    异常逐层上抛会直接打死同步线程——这是 exe 版"自己断掉"的元凶之一。这里把空的
    stdout/stderr 兜底重定向到日志文件,并记录未捕获异常,既消除崩溃源又留下现场。
    源码运行时 stdout 正常,不会被替换。返回日志路径。"""
    import time
    import traceback
    try:
        os.makedirs(inbox, exist_ok=True)
    except OSError:
        pass
    log_path = os.path.join(inbox, "wormhole-pet.log")
    try:
        log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return log_path
    # 仅在为 None(windowed 打包)时替换,避免影响源码运行时的真实控制台
    if sys.stdout is None:
        sys.stdout = log_file
    if sys.stderr is None:
        sys.stderr = log_file

    def _hook(exc_type, exc, tb):
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 未捕获异常:\n")
                traceback.print_exception(exc_type, exc, tb, file=f)
        except Exception:
            pass

    sys.excepthook = _hook    # 槽函数里漏出的异常改为记日志,尽量不让进程直接终止
    return log_path


def main(argv=None) -> None:
    cfg, size_override = _build_config(argv)
    _install_crash_log(cfg.inbox)   # 尽早安装:之后任何崩溃/print 都安全且留痕
    try:
        from PySide6.QtCore import QObject, Signal, Slot, QUrl, Qt, QPointF
        from PySide6.QtGui import (QGuiApplication, QIcon, QPixmap, QPainter,
                                   QColor, QRadialGradient)
        from PySide6.QtQml import QQmlApplicationEngine
        # 托盘菜单需要 QtWidgets(QApplication/QSystemTrayIcon/QMenu);
        # 不可用时降级:仍能跑桌宠,只是没有系统托盘(见 _setup_tray 的容错)
        try:
            from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
            _HAS_WIDGETS = True
        except ImportError:
            _HAS_WIDGETS = False
    except ImportError:
        sys.stderr.write(
            "未安装 PySide6。请先运行：pip install PySide6 --break-system-packages\n"
            "(同步引擎本身无需 GUI，可用 python -m pyftp_server.wormhole.sync 跑命令行版)\n")
        raise SystemExit(1)

    def _draw_icon_pixmap(size: int) -> QPixmap:
        """画单一尺寸的图标位图：圆角黑底 + 居中黑洞。"""
        from PySide6.QtGui import QPainterPath
        from PySide6.QtCore import QRectF
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)                   # 圆角外保持透明
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        radius = size * 0.22                      # macOS 风格圆角(边长 22%)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
        p.setClipPath(clip)
        p.fillPath(clip, QColor(0, 0, 0))         # 圆角黑底
        cx = cy = size / 2
        R = size * 0.42
        g = QRadialGradient(cx, cy, R)            # 中心黑 -> 乳白光晕 -> 融回黑背景
        g.setColorAt(0.00, QColor(0, 0, 0, 255))
        g.setColorAt(0.42, QColor(3, 3, 8, 255))
        g.setColorAt(0.60, QColor(72, 68, 86, 255))
        g.setColorAt(0.80, QColor(238, 236, 244, 255))
        g.setColorAt(1.00, QColor(0, 0, 0, 255))
        p.setBrush(g)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(cx, cy), R, R)
        p.end()
        return pm

    def _make_app_icon() -> QIcon:
        """多分辨率自适应图标：装入多个尺寸，系统按 Dock/任务栏/高分屏自动选最合适的。"""
        icon = QIcon()
        for s in (16, 32, 64, 128, 256, 512, 1024):
            icon.addPixmap(_draw_icon_pixmap(s))
        return icon

    def _setup_tray(app, bridge):
        """构建右键菜单 +(可用时)系统托盘图标。

        关键修复:把"菜单的构建"与"系统托盘是否可用"解耦。以前托盘不可用就整体
        return,bridge._tray_menu 一直是 None,右键桌宠就会走 showMenu 的危险回退直接
        退出程序——这正是桌宠会"自己断掉"的根因之一。现在只要 QtWidgets 在就先把菜单
        建好交给 bridge,无论托盘可用与否,右键永远能弹菜单。QtWidgets 完全缺失才返回 None。"""
        if not _HAS_WIDGETS:
            return None
        menu = QMenu()

        act_open = menu.addAction("打开收件箱")
        act_open.triggered.connect(bridge.openInbox)

        act_pause = menu.addAction("暂停同步")
        def _on_pause():
            paused = bridge.togglePause()
            act_pause.setText("恢复同步" if paused else "暂停同步")
        act_pause.triggered.connect(_on_pause)

        menu.addSeparator()
        act_status = menu.addAction("状态：…")
        act_status.setEnabled(False)                  # 仅显示,不可点
        menu.addSeparator()

        act_quit = menu.addAction("退出")
        act_quit.triggered.connect(bridge.quit)

        # 菜单每次弹出前刷新状态行(读标志,零网络开销),并同步暂停项文字
        def _refresh():
            act_status.setText("状态：" + bridge.connState())
            act_pause.setText("恢复同步" if bridge.isPaused() else "暂停同步")
        menu.aboutToShow.connect(_refresh)
        bridge._tray_menu = menu          # 先交给 Bridge:桌宠右键时弹同一菜单(与托盘无关)

        # 仅当系统托盘可用时才创建并显示托盘图标;不可用也不影响右键菜单
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(_make_app_icon(), app)
        tray.setToolTip("虫洞")
        tray.setContextMenu(menu)
        # 左键点托盘图标也打开收件箱(Win 习惯);菜单靠右键
        tray.activated.connect(
            lambda reason: bridge.openInbox()
            if reason == QSystemTrayIcon.Trigger else None)
        tray.show()
        return tray

    # ---- Python<->QML 桥：把同步引擎的事件转成 QML 信号驱动动画 ----
    class Bridge(QObject):
        absorb = Signal(str)      # 通知 QML 播放"吸入"动画(参数=文件名)
        emit_out = Signal(str)    # 通知 QML 播放"喷出"动画(参数=文件名)
        status = Signal(str)      # 状态文字

        def __init__(self, cfg: WormholeConfig):
            super().__init__()
            self._tray_menu = None        # 由 _setup_tray 注入:桌宠右键时弹出
            self.sync = WormholeSync(
                cfg,
                on_sent=lambda n: self.absorb.emit(n),
                on_received=lambda p: self.emit_out.emit(os.path.basename(p)),
                on_status=lambda s: self.status.emit(s),
            )
            self.sync.start()

        @Slot(str)
        def dropFile(self, url: str):
            """QML DropArea 收到桌面拖来的文件 url，转本地路径后发送。"""
            path = QUrl(url).toLocalFile() if url.startswith("file:") else url
            if path and os.path.isfile(path):
                # 上传放到后台线程，避免卡住动画
                import threading
                threading.Thread(target=self.sync.send_file, args=(path,), daemon=True).start()

        @Slot(result=str)
        def inboxPath(self) -> str:
            return os.path.abspath(self.sync.cfg.inbox)

        @Slot()
        def openInbox(self):
            """在系统文件管理器中打开收件箱目录(跨平台)。"""
            path = os.path.abspath(self.sync.cfg.inbox)
            os.makedirs(path, exist_ok=True)
            try:
                if sys.platform == "win32":
                    os.startfile(path)                                   # Windows 资源管理器
                elif sys.platform == "darwin":
                    import subprocess; subprocess.Popen(["open", path])  # macOS 访达
                else:
                    import subprocess; subprocess.Popen(["xdg-open", path])
            except Exception as e:
                self.status.emit(f"打开收件箱失败")
                print(f"[托盘] 打开收件箱失败: {e}", flush=True)

        @Slot(result=bool)
        def togglePause(self) -> bool:
            """暂停/恢复同步,返回切换后的暂停状态(True=已暂停)。"""
            new_state = not self.sync.is_paused()
            self.sync.set_paused(new_state)
            return new_state

        @Slot(result=bool)
        def isPaused(self) -> bool:
            return self.sync.is_paused()

        @Slot(result=str)
        def connState(self) -> str:
            """给菜单状态行用的当前连接文字(读标志,零网络开销)。"""
            if self.sync.is_paused():
                return "⏸ 已暂停"
            return "✅ 已连接" if self.sync.is_connected() else "⚠️ 未连接"

        @Slot()
        def showMenu(self):
            """桌宠被右键时,在鼠标位置弹出菜单(打开收件箱/暂停/状态/退出)。
            菜单在 _setup_tray 里构建,与系统托盘是否可用无关——只要 QtWidgets 在就有菜单。
            极端情况(QtWidgets 完全缺失,无菜单)退而打开收件箱,绝不静默退出程序。"""
            if self._tray_menu is not None:
                from PySide6.QtGui import QCursor
                self._tray_menu.popup(QCursor.pos())
            else:
                self.openInbox()

        @Slot()
        def quit(self):
            self.sync.stop()
            QGuiApplication.quit()

    # 有 QtWidgets 用 QApplication(支持托盘菜单),否则退回 QGuiApplication
    app = (QApplication if _HAS_WIDGETS else QGuiApplication)(sys.argv)
    app.setApplicationName("虫洞")
    app.setWindowIcon(_make_app_icon())          # Dock/任务栏：黑底居中黑洞
    app.setQuitOnLastWindowClosed(False)         # 关挂件窗口不退出(托盘还在),仅菜单"退出"才退
    bridge = Bridge(cfg)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("bridge", bridge)
    engine.rootContext().setContextProperty("petSizePx", _adaptive_pet_size(size_override))
    engine.load(QUrl.fromLocalFile(_QML_FILE))
    if not engine.rootObjects():
        sys.stderr.write("QML 加载失败\n")
        raise SystemExit(1)

    # 系统托盘(Windows 右下托盘 / macOS 顶部菜单栏,Qt 跨平台一套代码)
    _tray = _setup_tray(app, bridge)   # 返回托盘对象(需持引用防被回收),失败返回 None
    # macOS: 让挂件常驻所有桌面(Spaces)，切换桌面/应用失焦都不消失
    if sys.platform == "darwin":
        try:
            from AppKit import NSApp
            # canJoinAllSpaces(1<<0) | stationary(1<<4) | fullScreenAuxiliary(1<<8)
            # 注意先清掉 Qt.Tool 自带的 moveToActiveSpace(1<<1)，两者互斥
            for w in NSApp.windows():
                behavior = (w.collectionBehavior() & ~(1 << 1)) | (1 << 0) | (1 << 4) | (1 << 8)
                w.setCollectionBehavior_(behavior)
                w.setHidesOnDeactivate_(False)
        except ImportError:
            pass  # 未装 pyobjc 时跳过：功能不受影响，只是切桌面时挂件会隐藏
    os.makedirs(cfg.inbox, exist_ok=True)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
