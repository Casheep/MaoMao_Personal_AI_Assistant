from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QAction, QFont, QFontDatabase, QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from ..version import __version__
from .bridge import AssistantBridge


_instance_mutex = None
_ACTIVATION_SERVER = f"MaoMao.PersonalAssistant.{__version__}"


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def configure_windows_app_identity() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "MaoMao.LocalVoiceAssistant"
        )
    except (AttributeError, OSError):
        pass


def acquire_single_instance() -> bool:
    global _instance_mutex
    if sys.platform != "win32":
        return True
    _instance_mutex = ctypes.windll.kernel32.CreateMutexW(
        None, False, f"Local\\MaoMao.PersonalAssistant.{__version__}"
    )
    return ctypes.windll.kernel32.GetLastError() != 183


def notify_existing_instance() -> bool:
    for _attempt in range(4):
        socket = QLocalSocket()
        socket.connectToServer(_ACTIVATION_SERVER)
        if socket.waitForConnected(150):
            socket.write(b"show")
            socket.waitForBytesWritten(150)
            socket.disconnectFromServer()
            return True
    return False


def start_activation_server(bridge: AssistantBridge) -> QLocalServer | None:
    QLocalServer.removeServer(_ACTIVATION_SERVER)
    server = QLocalServer(bridge)
    if not server.listen(_ACTIVATION_SERVER):
        return None

    def activate() -> None:
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            if socket is not None:
                socket.disconnectFromServer()
                socket.deleteLater()
        bridge.showWindowRequested.emit()

    server.newConnection.connect(activate)
    return server


def create_engine(app: QGuiApplication) -> tuple[QQmlApplicationEngine, AssistantBridge]:
    families = set(QFontDatabase.families())
    family = next(
        (name for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI") if name in families),
        app.font().family(),
    )
    app.setFont(QFont(family, 10))
    bridge = AssistantBridge()
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("assistant", bridge)
    qml_path = Path(__file__).with_name("qml") / "Main.qml"
    engine.load(QUrl.fromLocalFile(str(qml_path)))
    if not engine.rootObjects():
        bridge.close()
        raise RuntimeError(f"无法加载 Qt Quick 界面：{qml_path}")
    app.aboutToQuit.connect(bridge.close)
    return engine, bridge


def _install_tray(
    app: QApplication,
    engine: QQmlApplicationEngine,
    bridge: AssistantBridge,
    icon: QIcon,
) -> QSystemTrayIcon | None:
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None
    window = engine.rootObjects()[0]
    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip(f"MaoMao · {__version__}")
    menu = QMenu()
    show_action = QAction("显示猫猫", menu)
    hide_action = QAction("隐藏到托盘", menu)
    restart_action = QAction("重启猫猫", menu)
    quit_action = QAction("退出猫猫", menu)

    def show_window() -> None:
        window.show()
        window.raise_()
        window.requestActivate()

    def hide_window() -> None:
        window.hide()

    def restart() -> None:
        subprocess.Popen(
            [sys.executable, "-m", "assistant_app.restart_helper", str(os.getpid())],
            cwd=str(Path(__file__).resolve().parents[2]),
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        quit_app()

    def quit_app() -> None:
        window.setProperty("forceClose", True)
        tray.hide()
        window.close()
        app.quit()

    show_action.triggered.connect(show_window)
    hide_action.triggered.connect(hide_window)
    restart_action.triggered.connect(restart)
    quit_action.triggered.connect(quit_app)
    menu.addAction(show_action)
    menu.addAction(hide_action)
    menu.addSeparator()
    menu.addAction(restart_action)
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: show_window()
        if reason in {QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick}
        else None
    )
    bridge.showWindowRequested.connect(show_window)
    bridge.notificationRequested.connect(
        lambda title, message: tray.showMessage(title, message, icon, 5000)
    )
    tray.show()
    return tray


def main() -> int:
    enable_dpi_awareness()
    configure_windows_app_identity()
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("MaoMao")
    app.setOrganizationName("MaoMao")
    app.setQuitOnLastWindowClosed(False)
    if not acquire_single_instance():
        notify_existing_instance()
        return 0
    icon_path = Path(__file__).resolve().parents[2] / "miao-icon.png"
    icon = QIcon(str(icon_path)) if icon_path.is_file() else QIcon()
    app.setWindowIcon(icon)
    engine, bridge = create_engine(app)
    activation_server = start_activation_server(bridge)
    tray = _install_tray(app, engine, bridge, icon)
    _ = (engine, bridge, tray, activation_server)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
