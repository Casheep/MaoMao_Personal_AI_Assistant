from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")

from PySide6.QtCore import QMetaObject, QObject, QPointF
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest

from assistant_app.qt_quick.app import create_engine
from assistant_app.qt_quick.bridge import AssistantBridge
from assistant_app.skill_categories import SkillCategoryState
from assistant_app.skills import SKILL_CATALOG, SKILL_CATEGORY_ORDER
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

    def test_category_manager_is_created_only_when_opened(self) -> None:
        engine, bridge = create_engine(self.app)
        bridge._startup_timer.stop()
        try:
            root = engine.rootObjects()[0]
            self.assertIsNone(root.findChild(QObject, "categoryManager"))
            self.assertTrue(QMetaObject.invokeMethod(root, "openCategoryManager"))
            QTest.qWait(20)
            manager = root.findChild(QObject, "categoryManager")
            self.assertIsNotNone(manager)
            self.assertTrue(manager.property("opened"))
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

    def test_public_pep440_and_windows_versions_use_their_required_formats(self) -> None:
        self.assertEqual(__version__, "v0.0.3_beta3")
        project = Path("pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('version = "0.0.3b3"', project)
        launcher = Path("launcher/MaoMaoLauncher.cs").read_text(encoding="utf-8")
        self.assertIn('AssemblyVersion("0.0.3.0")', launcher)
        self.assertIn('AssemblyFileVersion("0.0.3.0")', launcher)
        for manifest_path in ("launcher/app.manifest", "launcher/beta.manifest"):
            manifest = Path(manifest_path).read_text(encoding="utf-8")
            self.assertIn('assemblyIdentity version="0.0.3.0"', manifest)

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
                    "import sys; from PySide6.QtWidgets import QApplication; "
                    "from assistant_app.qt_quick.app import create_engine; "
                    "app=QApplication([]); engine,bridge=create_engine(app); "
                    "bridge._startup_timer.stop(); "
                    "names=('assistant_app.agent','httpx','sounddevice','numpy'); "
                    "print(','.join(name for name in names if name in sys.modules)); "
                    "bridge.close()"
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
        self.assertIsNotNone(EXIT_PATTERN.fullmatch("没事了"))
        self.assertIsNotNone(EXIT_PATTERN.fullmatch("没事了，没事了"))
        self.assertIsNotNone(EXIT_PATTERN.fullmatch("没事了，谢谢你"))
        self.assertIsNone(EXIT_PATTERN.fullmatch("继续帮我看看天气"))
        self.assertIsNone(EXIT_PATTERN.fullmatch("没事了，帮我关灯"))

    def test_followup_timeout_returns_to_ready_without_a_failure_message(self) -> None:
        bridge = AssistantBridge()
        try:
            bridge._recording = True
            bridge._continuous_session = True
            with patch.object(bridge, "_resume_wake"):
                bridge._finish_followup_timeout()
            self.assertFalse(bridge.recording)
            self.assertFalse(bridge._continuous_session)
            self.assertEqual(bridge.status, "就绪")
            source = Path("assistant_app/qt_quick/bridge.py").read_text(encoding="utf-8")
            self.assertNotIn(
                'self._transcriptionFailed.emit("等待后续语音超时")',
                source,
            )
        finally:
            bridge.close()

    def test_voice_goodbye_is_displayed_and_saved_as_a_complete_exchange(self) -> None:
        bridge = AssistantBridge()
        try:
            bridge._continuous_session = True
            goodbye = bridge.config["conversation"]["goodbye_text"]
            with (
                patch.object(bridge._ui_database, "add_messages") as add_messages,
                patch.object(bridge._voice_executor, "submit") as submit,
            ):
                bridge._finish_transcription("拜拜啦", 0.8)
            self.assertEqual(
                bridge.messages[-2:],
                [
                    {"role": "user", "text": "拜拜啦", "meta": "语音识别"},
                    {
                        "role": "assistant",
                        "text": goodbye,
                        "meta": "本地结束连续对话",
                    },
                ],
            )
            add_messages.assert_called_once_with(
                bridge.session_id,
                [("user", "拜拜啦"), ("assistant", goodbye)],
            )
            submit.assert_called_once_with(bridge._speak_answer, goodbye, "text")
            self.assertFalse(bridge._continuous_session)
            self.assertEqual(bridge.status, "连续对话已结束")
        finally:
            bridge.close()

    def test_spoken_wake_exchange_is_displayed_and_saved(self) -> None:
        bridge = AssistantBridge()
        try:
            bridge._wake_enabled = True
            bridge.config.setdefault("tts", {})["enabled"] = True
            bridge.config.setdefault("wake_word", {})["response_texts"] = ["我在呢。"]
            bridge._wake_listener.last_detected_phrase = "猫猫"
            with (
                patch.object(bridge._ui_database, "add_messages") as add_messages,
                patch.object(bridge._voice_executor, "submit") as submit,
            ):
                bridge._handle_wake()
            self.assertEqual(
                bridge.messages[-2:],
                [
                    {"role": "user", "text": "猫猫", "meta": "语音识别"},
                    {
                        "role": "assistant",
                        "text": "我在呢。",
                        "meta": "本地唤醒回应",
                    },
                ],
            )
            add_messages.assert_called_once_with(
                bridge.session_id,
                [("user", "猫猫"), ("assistant", "我在呢。")],
            )
            self.assertEqual(
                submit.call_args_list[-1].args,
                (bridge._wake_and_record, "我在呢。"),
            )
        finally:
            bridge.close()

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
            self.assertNotIn('text: "定时任务"; onClicked: window.openManager(2)', qml)
            self.assertIn("modelData.lastResultLabel", qml)
            scheduled = next(item for item in bridge.skills if item["id"] == "scheduled-tasks")
            self.assertTrue(scheduled["hasSettings"])
        finally:
            bridge.close()

    def test_bridge_expires_missed_schedules_before_polling(self) -> None:
        with patch(
            "assistant_app.qt_quick.bridge.Database.expire_missed_scheduled_tasks",
            return_value=2,
        ) as expire:
            bridge = AssistantBridge()
        try:
            expire.assert_called_once_with()
        finally:
            bridge.close()

    def test_answer_history_includes_task_elapsed_time(self) -> None:
        bridge = AssistantBridge()
        try:
            bridge.config.setdefault("tts", {})["enabled"] = False
            bridge._generated_skill_store.match = lambda _text: None
            answer = SimpleNamespace(
                text="完成",
                route=SimpleNamespace(model="kimi-k3", reasoning="low", reasons=()),
                timings=(
                    ("model:kimi-k2.6", 3.36),
                    ("model:kimi-k3", 40.91),
                    ("tools", 0.47),
                ),
            )
            bridge._runtime = (
                SimpleNamespace(run=lambda *_args, **_kwargs: answer),
                None,
                None,
            )
            with patch(
                "assistant_app.qt_quick.bridge.time.perf_counter",
                side_effect=[100.0, 145.5],
            ):
                bridge._run_prompt("测试", "text")
            self.assertEqual(bridge.messages[-1]["text"], "完成")
            self.assertIn("耗时 45.5 秒", bridge.messages[-1]["meta"])
            self.assertIn("K2.6 3.4 秒 / K3 40.9 秒 / 工具 0.5 秒", bridge.messages[-1]["meta"])
        finally:
            bridge._runtime = None
            bridge.close()

    def test_scheduled_task_result_includes_elapsed_time(self) -> None:
        bridge = AssistantBridge()
        emitted: list[tuple[int, bool, str, bool]] = []
        bridge._scheduleFinished.connect(
            lambda task_id, success, result, silent: emitted.append(
                (task_id, success, result, silent)
            )
        )
        fake_agent = SimpleNamespace(
            run=lambda *_args, **_kwargs: SimpleNamespace(text="任务完成"),
            close=lambda: None,
        )
        fake_database = SimpleNamespace(close=lambda: None)
        try:
            with (
                patch(
                    "assistant_app.qt_quick.bridge.build_agent",
                    return_value=(fake_agent, fake_database, None),
                ),
                patch.object(bridge._ui_database, "complete_scheduled_task"),
                patch(
                    "assistant_app.qt_quick.bridge.time.perf_counter",
                    side_effect=[200.0, 203.21],
                ),
            ):
                bridge._run_schedule(
                    {"id": 42, "command": "测试任务", "silent": True},
                    SimpleNamespace(),
                )
            self.assertEqual(emitted[0][:2], (42, True))
            self.assertIn("任务完成", emitted[0][2])
            self.assertIn("耗时 3.2 秒", emitted[0][2])
        finally:
            bridge.close()

    def test_second_instance_requests_the_existing_window(self) -> None:
        source = Path("assistant_app/qt_quick/app.py").read_text(encoding="utf-8")
        self.assertIn("QLocalServer", source)
        self.assertIn("bridge.showWindowRequested.emit()", source)

    def test_wake_word_does_not_force_the_window_to_front(self) -> None:
        source = Path("assistant_app/qt_quick/bridge.py").read_text(encoding="utf-8")
        wake_handler = source[source.index("def _handle_wake"):source.index("def _wake_and_record")]
        self.assertNotIn("showWindowRequested.emit()", wake_handler)
        self.assertIn("self._voice_executor.submit(self._wake_and_record, response)", wake_handler)

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
        self.assertIn('assistant.preloadLoading ? "加载中"', qml)
        self.assertIn('assistant.preloadEnabled ? "开" : "关"', qml)
        self.assertIn('implicitWidth: 258', main_controls)
        self.assertIn('住在你电脑里的语音助手喵~', qml)
        self.assertNotIn('model: ["设置", "权限", "定时任务", "关于"]', qml)
        self.assertIn(
            'color: modelData.role === "user" ? "#A66300"',
            qml,
        )

    def test_every_sidebar_starts_collapsed(self) -> None:
        with patch(
            "assistant_app.qt_quick.bridge.load_config",
            return_value={
                "ui": {
                    "skill_sidebar_expanded": True,
                    "favorite_sidebar_expanded": True,
                },
                "skills": {},
                "audio": {"sample_rate": 16000, "channels": 1},
                "wake_word": {"enabled": False},
                "tts": {"enabled": False},
            },
        ):
            bridge = AssistantBridge()
        try:
            self.assertFalse(bridge.skillSidebarExpanded)
            self.assertFalse(bridge.favoriteSidebarExpanded)
        finally:
            bridge.close()

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

    def test_base_ability_is_folded_into_the_computer_category(self) -> None:
        current_time = next(skill for skill in SKILL_CATALOG if skill.id == "current-time")
        self.assertEqual(current_time.category, "电脑与浏览器")
        self.assertNotIn("基础能力", SKILL_CATEGORY_ORDER)

    def test_bridge_persists_custom_skill_categories_and_assignments(self) -> None:
        bridge = AssistantBridge()
        bridge.config = {
            **bridge.config,
            "ui": {
                **bridge.config.get("ui", {}),
                "custom_skill_categories": [],
                "skill_category_overrides": {"current-time": "基础能力"},
            },
        }
        try:
            current_time = next(item for item in bridge.skills if item["id"] == "current-time")
            self.assertEqual(current_time["category"], "电脑与浏览器")
            with patch("assistant_app.qt_quick.bridge.save_local_settings") as save:
                self.assertTrue(bridge.addSkillCategory("我的工具"))
                self.assertFalse(bridge.addSkillCategory("我的工具"))
                self.assertEqual(bridge.skillCategoryStatus, "这个分类已经存在")
                bridge.setSkillCategory("current-time", "我的工具")
                self.assertEqual(
                    next(item for item in bridge.skills if item["id"] == "current-time")["category"],
                    "我的工具",
                )
                self.assertTrue(bridge.renameSkillCategory("我的工具", "日常"))
                self.assertEqual(
                    next(item for item in bridge.skills if item["id"] == "current-time")["category"],
                    "日常",
                )
                bridge.removeSkillCategory("日常")
            self.assertEqual(bridge.customSkillCategories, [])
            self.assertEqual(
                next(item for item in bridge.skills if item["id"] == "current-time")["category"],
                "电脑与浏览器",
            )
            self.assertEqual(save.call_count, 4)
        finally:
            bridge.close()

    def test_category_state_validates_reorders_and_restores_defaults(self) -> None:
        state = SkillCategoryState.from_ui_config(
            {
                "custom_skill_categories": ["工作", "生活", "工作", None],
                "skill_category_overrides": {"current-time": "工作", "missing": "生活"},
            }
        )
        self.assertEqual(state.custom_categories, ["工作", "生活"])
        self.assertEqual(state.category_for("current-time", "电脑与浏览器"), "工作")
        self.assertEqual(state.move("生活", -1), (True, "已调整“生活”的顺序"))
        self.assertEqual(state.custom_categories, ["生活", "工作"])
        self.assertTrue(state.rename("工作", "效率")[0])
        self.assertEqual(state.category_for("current-time", "电脑与浏览器"), "效率")
        self.assertTrue(state.remove("效率")[0])
        self.assertEqual(state.category_for("current-time", "电脑与浏览器"), "电脑与浏览器")

    def test_skill_and_favorite_panels_support_search_and_category_management(self) -> None:
        qml = Path("assistant_app/qt_quick/qml/Main.qml").read_text(encoding="utf-8")
        category_qml = Path("assistant_app/qt_quick/qml/SkillCategoryManager.qml").read_text(encoding="utf-8")
        search_qml = Path("assistant_app/qt_quick/qml/RoundedSearchField.qml").read_text(encoding="utf-8")
        self.assertIn('id: categoryManagerLoader', qml)
        self.assertIn('active: false', qml)
        self.assertIn('onClicked: window.openCategoryManager()', qml)
        self.assertIn('placeholderText: "搜索技能"', qml)
        self.assertIn('placeholderText: "搜索收藏"', qml)
        self.assertIn('assistantBackend.setSkillCategory', category_qml)
        self.assertIn('assistantBackend.moveSkillCategory', category_qml)
        self.assertIn('id: deleteConfirmation', category_qml)
        self.assertIn('placeholderText: "搜索待归类技能"', category_qml)
        self.assertIn('root.matchesSkill(item, categorySkillSearch.text)', category_qml)
        self.assertIn('control.clear()', search_qml)
        self.assertIn('["全部"].concat(assistant.skillCategories)', qml)
        self.assertIn('contentWidth: availableWidth', qml)
        self.assertIn('Layout.preferredWidth: (skillCategoryScroll.availableWidth - 2) / 2', qml)
        self.assertNotIn('"基础能力"]', qml)

    def test_bottom_creator_toolbar_has_workflow_and_code_generators(self) -> None:
        main_qml = Path("assistant_app/qt_quick/qml/Main.qml").read_text(encoding="utf-8")
        toolbar = Path("assistant_app/qt_quick/qml/CreatorToolbar.qml").read_text(encoding="utf-8")
        bridge = Path("assistant_app/qt_quick/bridge.py").read_text(encoding="utf-8")
        self.assertIn('objectName: "creatorToolbar"', main_qml)
        self.assertIn('text: "组合技能"', toolbar)
        self.assertIn('text: "代码技能"', toolbar)
        self.assertIn("Kimi K3", toolbar)
        self.assertIn("Kimi K3 harness", toolbar)
        self.assertIn('model="kimi-k3"', bridge)
        self.assertIn('getattr(agent.client, "kimi", None)', bridge)
        self.assertNotIn("ask_chatgpt", toolbar)
        self.assertNotIn("Kimi K3 技能生成", toolbar)
        self.assertNotIn("个生成技能", toolbar)
        self.assertIn('"#EEE9FF"', toolbar)
        self.assertIn('text: "工具栏"', toolbar)
        self.assertNotIn('text: root.expanded ?', toolbar)

    def test_expanded_creator_toolbar_stays_inside_the_window(self) -> None:
        engine, bridge = create_engine(self.app)
        bridge._startup_timer.stop()
        try:
            root = engine.rootObjects()[0]
            toolbar = root.findChild(QObject, "creatorToolbar")
            toolbar_toggle = root.findChild(QObject, "creatorToolbarToggle")
            toolbar_drawer = root.findChild(QObject, "creatorToolbarDrawer")
            audio_card = root.findChild(QObject, "audioControlCard")
            history_card = root.findChild(QObject, "historyCard")
            input_card = root.findChild(QObject, "inputCard")
            self.assertIsNotNone(toolbar)
            self.assertIsNotNone(toolbar_toggle)
            self.assertIsNotNone(toolbar_drawer)
            self.assertIsNotNone(audio_card)
            self.assertIsNotNone(history_card)
            self.assertIsNotNone(input_card)
            root.setWidth(1280)
            root.setHeight(800)
            toolbar.setProperty("expanded", False)
            QTest.qWait(180)
            self.assertTrue(toolbar_toggle.property("visible"))
            self.assertFalse(toolbar_drawer.property("visible"))
            self.assertEqual(float(toolbar.property("height")), 36)
            self.assertTrue(input_card.property("visible"))
            collapsed_history_height = float(history_card.property("height"))
            self.assertTrue(QMetaObject.invokeMethod(toolbar_toggle, "click"))
            QTest.qWait(180)
            self.assertTrue(toolbar.property("expanded"))
            self.assertTrue(toolbar.property("visible"))
            self.assertTrue(toolbar_toggle.property("visible"))
            self.assertTrue(toolbar_drawer.property("visible"))
            self.assertTrue(audio_card.property("visible"))
            self.assertFalse(input_card.property("visible"))
            self.assertGreater(float(toolbar.property("height")), 250)
            self.assertLess(float(history_card.property("height")), collapsed_history_height)
            self.assertGreaterEqual(float(toolbar.property("y")), 0)
            self.assertLessEqual(float(toolbar.property("y")) + float(toolbar.property("height")), 800)
            self.assertTrue(QMetaObject.invokeMethod(toolbar_toggle, "click"))
            QTest.qWait(180)
            self.assertFalse(toolbar.property("expanded"))
            self.assertTrue(toolbar_toggle.property("visible"))
            self.assertFalse(toolbar_drawer.property("visible"))
            self.assertTrue(audio_card.property("visible"))
            self.assertTrue(input_card.property("visible"))
        finally:
            bridge.close()

    def test_creator_toolbar_stays_below_input_and_between_sidebars(self) -> None:
        engine, bridge = create_engine(self.app)
        bridge._startup_timer.stop()
        try:
            root = engine.rootObjects()[0]
            toolbar = root.findChild(QObject, "creatorToolbar")
            toggle = root.findChild(QObject, "creatorToolbarToggle")
            drawer = root.findChild(QObject, "creatorToolbarDrawer")
            input_card = root.findChild(QObject, "inputCard")
            skill_slot = root.findChild(QObject, "skillSlot")
            favorite_slot = root.findChild(QObject, "favoriteSlot")
            for width, height in ((1280, 800), (960, 560)):
                root.setWidth(width)
                root.setHeight(height)
                toolbar.setProperty("expanded", False)
                QTest.qWait(170)
                collapsed_toolbar_origin = toolbar.mapToGlobal(QPointF(0, 0))
                collapsed_input_origin = input_card.mapToGlobal(QPointF(0, 0))
                self.assertGreaterEqual(
                    collapsed_toolbar_origin.y(),
                    collapsed_input_origin.y() + float(input_card.property("height")),
                )
                self.assertTrue(QMetaObject.invokeMethod(toggle, "click"))
                QTest.qWait(170)
                self.assertTrue(toggle.property("visible"))
                self.assertTrue(drawer.property("visible"))
                toolbar_origin = toolbar.mapToGlobal(QPointF(0, 0))
                skill_origin = skill_slot.mapToGlobal(QPointF(0, 0))
                favorite_origin = favorite_slot.mapToGlobal(QPointF(0, 0))
                self.assertFalse(input_card.property("visible"))
                self.assertTrue(root.findChild(QObject, "audioControlCard").property("visible"))
                self.assertGreater(
                    toolbar_origin.x(),
                    skill_origin.x() + float(skill_slot.property("width")),
                )
                self.assertLess(
                    toolbar_origin.x() + float(toolbar.property("width")),
                    favorite_origin.x(),
                )
                self.assertGreaterEqual(toolbar_origin.y(), 0)
                self.assertLessEqual(
                    toolbar_origin.y() + float(toolbar.property("height")),
                    height,
                )
        finally:
            bridge.close()

    def test_resizing_to_compact_closes_sidebars_without_reopening_them(self) -> None:
        engine, bridge = create_engine(self.app)
        bridge._startup_timer.stop()
        try:
            root = engine.rootObjects()[0]
            skill_slot = root.findChild(QObject, "skillSlot")
            favorite_slot = root.findChild(QObject, "favoriteSlot")
            self.assertIsNotNone(skill_slot)
            self.assertIsNotNone(favorite_slot)
            root.setWidth(1180)
            QTest.qWait(20)
            root.setProperty("skillOpen", True)
            root.setProperty("favoriteOpen", True)
            root.setWidth(1000)
            QTest.qWait(180)
            self.assertLess(float(skill_slot.property("width")), 80)
            self.assertLess(float(favorite_slot.property("width")), 80)
            root.setWidth(700)
            QTest.qWait(180)
            self.assertLess(float(skill_slot.property("width")), 80)
            self.assertLess(float(favorite_slot.property("width")), 80)
            root.setWidth(1180)
            QTest.qWait(180)
            self.assertLess(float(skill_slot.property("width")), 80)
            self.assertLess(float(favorite_slot.property("width")), 80)
        finally:
            bridge.close()

    def test_compact_audio_controls_keep_preload_and_pause_inside_the_card(self) -> None:
        engine, bridge = create_engine(self.app)
        bridge._startup_timer.stop()
        try:
            root = engine.rootObjects()[0]
            root.setWidth(1000)
            toggle = root.findChild(QObject, "skillSidebarToggle")
            self.assertTrue(QMetaObject.invokeMethod(toggle, "click"))
            QTest.qWait(180)
            card = root.findChild(QQuickItem, "audioControlCard")
            preload = root.findChild(QQuickItem, "compactPreloadButton")
            pause_all = root.findChild(QQuickItem, "pauseAllButton")
            self.assertTrue(bool(card.property("compactAudioControls")))
            self.assertTrue(preload.isVisible())
            for control in (preload, pause_all):
                position = control.mapToItem(card, QPointF(0, 0))
                self.assertGreaterEqual(position.x(), 0)
                self.assertLessEqual(position.x() + control.width(), card.width())
            qml = Path("assistant_app/qt_quick/qml/Main.qml").read_text(encoding="utf-8")
            self.assertNotIn(
                'text: "暂停"; onClicked: assistant.ttsControl("pause")',
                qml,
            )
            self.assertNotIn(
                'text: "继续"; onClicked: assistant.ttsControl("resume")',
                qml,
            )
            self.assertNotIn(
                'text: "停止"; onClicked: assistant.ttsControl("stop")',
                qml,
            )
        finally:
            bridge.close()

    def test_windows_main_keeps_the_default_threaded_render_loop(self) -> None:
        source = Path("assistant_app/qt_quick/app.py").read_text(encoding="utf-8")
        self.assertNotIn('QSG_RENDER_LOOP", "basic"', source)

    def test_windows_main_selects_opengl_before_creating_the_app(self) -> None:
        source = Path("assistant_app/qt_quick/app.py").read_text(encoding="utf-8")
        backend = source.index('os.environ.setdefault("QSG_RHI_BACKEND", "opengl")')
        app_creation = source.index("QApplication.instance() or QApplication(sys.argv)")
        self.assertLess(backend, app_creation)


if __name__ == "__main__":
    unittest.main()
