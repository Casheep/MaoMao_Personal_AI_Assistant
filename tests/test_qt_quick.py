from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")

from PySide6.QtGui import QGuiApplication

from assistant_app.qt_quick.app import create_engine
from assistant_app.qt_quick.bridge import AssistantBridge
from assistant_app.version import __version__


class QtQuickMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QGuiApplication.instance() or QGuiApplication([])

    def test_qml_shell_loads_with_skill_catalog(self) -> None:
        engine, bridge = create_engine(self.app)
        try:
            self.assertEqual(len(engine.rootObjects()), 1)
            self.assertGreater(len(bridge.skills), 0)
            self.assertEqual(engine.rootObjects()[0].title(), "MaoMao")
        finally:
            bridge.close()

    def test_bridge_persists_model_and_skill_preferences(self) -> None:
        bridge = AssistantBridge()
        bridge.config = {
            **bridge.config,
            "routing": {**bridge.config.get("routing", {}), "model_mode": "auto"},
            "skills": dict(bridge.config.get("skills", {})),
            "ui": {"favorite_skills": []},
        }
        try:
            with patch("assistant_app.qt_quick.bridge.save_local_settings") as save:
                bridge.setModelMode("k3")
                bridge.setSkillEnabled("wake-word", False)
                bridge.setSkillFavorite("wake-word", True)
            self.assertEqual(bridge.modelMode, "k3")
            wake_word = next(item for item in bridge.skills if item["id"] == "wake-word")
            self.assertFalse(wake_word["enabled"])
            self.assertTrue(wake_word["favorite"])
            self.assertEqual(save.call_count, 3)
        finally:
            bridge.close()

    def test_v002_is_the_single_runtime_version(self) -> None:
        self.assertEqual(__version__, "v0.0.2_beta2")
        project = Path("pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('version = "0.0.2b2"', project)

    def test_launcher_uses_qt_quick_entrypoint(self) -> None:
        source = Path("launcher/MaoMaoLauncher.cs").read_text(encoding="utf-8")
        self.assertIn("-m assistant_app.qt_quick.app", source)

    def test_qt_quick_is_the_only_gui_entrypoint(self) -> None:
        source = Path("assistant_app/cli.py").read_text(encoding="utf-8")
        self.assertNotIn("--legacy-gui", source)
        self.assertNotIn("from .rounded_gui import main", source)

    def test_qt_startup_defers_conversation_and_audio_device_dependencies(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import assistant_app.qt_quick.app; "
                    "names=('assistant_app.agent','httpx','sounddevice'); "
                    "print(','.join(name for name in names if name in sys.modules))"
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.stdout.strip(), "")

    def test_compact_combo_and_form_controls_use_rounded_backgrounds(self) -> None:
        qml = Path("assistant_app/qt_quick/qml/Main.qml").read_text(encoding="utf-8")
        combo = qml[qml.index("component CompactCombo"):qml.index("component SettingTitle")]
        self.assertIn("popup: Popup", combo)
        self.assertIn("delegate: ItemDelegate", combo)
        self.assertIn("radius: 14", combo)
        self.assertIn("component RoundCheckBox", combo)
        self.assertIn("component RoundedProgressBar", combo)
        self.assertIn("background: Rectangle { color: window.cardColor; radius: 24 }", qml)

    def test_developer_attribution_is_consistent(self) -> None:
        qml = Path("assistant_app/qt_quick/qml/Main.qml").read_text(encoding="utf-8")
        project = Path("pyproject.toml").read_text(encoding="utf-8")
        for source in (qml, project):
            self.assertIn("casheep", source)
            self.assertNotIn("MaoMao contributors", source)

    def test_retired_gui_modules_are_not_shipped(self) -> None:
        self.assertFalse(Path("assistant_app/gui.py").exists())
        self.assertFalse(Path("assistant_app/rounded_gui.py").exists())

    def test_current_bridge_owns_wake_parsing_and_conversation_exit_rules(self) -> None:
        self.assertEqual(
            AssistantBridge._parse_wake_words(" 猫猫，喵喵助手；猫 猫\n小七 "),
            ["猫猫", "喵喵助手", "小七"],
        )
        from assistant_app.qt_quick.bridge import EXIT_PATTERN

        self.assertIsNotNone(EXIT_PATTERN.fullmatch("拜拜啦"))
        self.assertIsNone(EXIT_PATTERN.fullmatch("继续帮我看看天气"))

    def test_schedule_ui_preserves_normal_and_silent_modes(self) -> None:
        bridge = AssistantBridge()
        try:
            with patch.object(bridge._ui_database, "create_scheduled_task") as create:
                bridge.createSchedule("整理结果", "2026-08-12 09:00", False, False)
            create.assert_called_once()
            self.assertEqual(create.call_args.args[:3], ("整理结果", create.call_args.args[1], "once"))
            self.assertFalse(create.call_args.kwargs["silent"])

            qml = Path("assistant_app/qt_quick/qml/Main.qml").read_text(encoding="utf-8")
            self.assertIn("scheduleSilent.checked", qml)
            self.assertIn("assistant.defaultScheduleTime", qml)
        finally:
            bridge.close()

    def test_second_instance_requests_the_existing_window(self) -> None:
        source = Path("assistant_app/qt_quick/app.py").read_text(encoding="utf-8")
        self.assertIn("QLocalServer", source)
        self.assertIn("bridge.showWindowRequested.emit()", source)

    def test_qt_shell_preserves_the_previous_three_column_layout(self) -> None:
        qml = Path("assistant_app/qt_quick/qml/Main.qml").read_text(encoding="utf-8")
        self.assertIn("id: skillSlot", qml)
        self.assertIn("id: favoriteSlot", qml)
        self.assertIn("width: 238", qml)
        self.assertIn("width: 208", qml)
        self.assertNotIn("opacity:", qml)

    def test_main_controls_match_the_previous_shell(self) -> None:
        qml = Path("assistant_app/qt_quick/qml/Main.qml").read_text(encoding="utf-8")
        main_controls = qml[qml.index("id: chatView"):qml.index("id: favoriteSlot")]
        self.assertNotIn('text: "连续对话"', main_controls)
        self.assertIn('assistant.preloadLoading ? "加载中"', main_controls)
        self.assertIn('assistant.preloadEnabled ? "开" : "关"', main_controls)
        self.assertIn('implicitWidth: 258', main_controls)
        self.assertIn('住在你电脑里的语音助手喵~', qml)
        self.assertNotIn('model: ["设置", "权限", "定时任务", "关于"]', qml)

    def test_bridge_persists_sidebar_visibility(self) -> None:
        bridge = AssistantBridge()
        try:
            with patch("assistant_app.qt_quick.bridge.save_local_settings") as save:
                bridge.setSkillSidebarExpanded(False)
                bridge.setFavoriteSidebarExpanded(True)
            self.assertFalse(bridge.skillSidebarExpanded)
            self.assertTrue(bridge.favoriteSidebarExpanded)
            self.assertEqual(save.call_count, 2)
        finally:
            bridge.close()


if __name__ == "__main__":
    unittest.main()
