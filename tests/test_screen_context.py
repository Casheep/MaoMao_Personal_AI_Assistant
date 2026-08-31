from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from assistant_app.agent import PersonalAgent
from assistant_app.config import AppPaths
from assistant_app.database import Database
from assistant_app.providers import KimiResponse
from assistant_app.screen_capture import (
    CapturedScreen,
    capture_for_inspection,
    encode_screen_image,
)
from assistant_app.screen_context import (
    ScreenCommand,
    UIAElement,
    UIAMatch,
    UIASnapshot,
    WindowsUIAFastPath,
    needs_screen_context,
    parse_screen_command,
    screen_visual_plan,
    should_preload_screen_image,
)
from assistant_app.tool_types import ToolResult
from assistant_app.tools import ToolRegistry


class _Rectangle:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class _Pattern:
    def __init__(self, method: str) -> None:
        self.method = method
        self.calls: list[str] = []

    def Invoke(self) -> None:
        self.calls.append("invoke")

    def Toggle(self) -> None:
        self.calls.append("toggle")

    def Select(self) -> None:
        self.calls.append("select")

    def SetValue(self, value: str) -> None:
        self.calls.append(value)


class _Control:
    def __init__(
        self,
        name: str,
        control_type: str,
        *,
        automation_id: str = "",
        visible: bool = True,
        enabled: bool = True,
        rectangle: tuple[int, int, int, int] = (10, 20, 110, 60),
        current_value: str = "private current value",
    ) -> None:
        self._name = name
        self._visible = visible
        self._enabled = enabled
        self._rectangle = _Rectangle(*rectangle)
        self.current_value = current_value
        self.element_info = SimpleNamespace(
            name=name,
            control_type=control_type,
            automation_id=automation_id,
            visible=visible,
            enabled=enabled,
            rectangle=self._rectangle,
        )
        self.iface_invoke = _Pattern("invoke")
        self.iface_toggle = _Pattern("toggle")
        self.iface_selection_item = _Pattern("select")
        self.iface_value = _Pattern("value")
        self.clicks = 0
        self.edit_values: list[str] = []

    def window_text(self) -> str:
        return self._name

    def is_visible(self) -> bool:
        return self._visible

    def is_enabled(self) -> bool:
        return self._enabled

    def rectangle(self) -> _Rectangle:
        return self._rectangle

    def click_input(self) -> None:
        self.clicks += 1

    def set_edit_text(self, value: str) -> None:
        self.edit_values.append(value)


class _Window:
    def __init__(self, controls: list[_Control], title: str = "Example") -> None:
        self.controls = controls
        self.title = title
        self.descendant_calls = 0

    def window_text(self) -> str:
        return self.title

    def descendants(self, **_kwargs) -> list[_Control]:
        self.descendant_calls += 1
        return list(self.controls)


class _Desktop:
    def __init__(self, window: _Window) -> None:
        self._window = window

    def window(self, *, handle: int) -> _Window:
        if handle != 42:
            raise AssertionError(f"unexpected handle {handle}")
        return self._window


def _fast_path(controls: list[_Control]) -> tuple[WindowsUIAFastPath, _Window]:
    window = _Window(controls)
    desktop = _Desktop(window)
    return (
        WindowsUIAFastPath(
            desktop_factory=lambda: desktop,
            foreground_handle=lambda: 42,
        ),
        window,
    )


class ScreenCommandTests(unittest.TestCase):
    def test_explicit_click_commands_are_parsed(self) -> None:
        command = parse_screen_command("请点击“确定”按钮")
        self.assertIsNotNone(command)
        self.assertEqual((command.action, command.target), ("invoke", "确定"))
        english = parse_screen_command("click the Continue button")
        self.assertIsNotNone(english)
        self.assertEqual((english.action, english.target), ("invoke", "Continue"))

    def test_explicit_input_commands_are_parsed(self) -> None:
        command = parse_screen_command("在搜索框里输入 猫猫")
        self.assertIsNotNone(command)
        self.assertEqual(
            (command.action, command.target, command.value),
            ("set_value", "搜索框", "猫猫"),
        )
        reversed_command = parse_screen_command("把 Sydney 输入到城市")
        self.assertIsNotNone(reversed_command)
        self.assertEqual(
            (reversed_command.target, reversed_command.value),
            ("城市", "Sydney"),
        )

    def test_negated_questions_and_multistep_commands_are_rejected(self) -> None:
        for text in (
            "不要点击确定",
            "怎么点击确定？",
            "点击确定然后关闭窗口",
            "屏幕上的确定按钮是什么意思？",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_screen_command(text))

    def test_sensitive_text_entry_never_bypasses_the_model_policy(self) -> None:
        self.assertIsNone(parse_screen_command("在密码框输入 hunter2"))
        self.assertIsNone(parse_screen_command("type 123456 into the passcode field"))

    def test_context_trigger_is_narrowly_screen_related(self) -> None:
        self.assertTrue(needs_screen_context("点击登录按钮"))
        self.assertTrue(needs_screen_context("看看屏幕显示什么"))
        self.assertTrue(needs_screen_context("你能看到吗"))
        self.assertTrue(needs_screen_context("can you see this?"))
        self.assertFalse(needs_screen_context("悉尼今天天气怎么样"))
        self.assertFalse(needs_screen_context("帮我写一个网页页面"))
        self.assertFalse(needs_screen_context("你能看到未来吗"))
        self.assertFalse(needs_screen_context("看看悉尼现在的天气"))

    def test_visual_preload_and_detail_plans_are_task_adaptive(self) -> None:
        self.assertTrue(should_preload_screen_image("你知道现在屏幕是什么吗"))
        self.assertTrue(should_preload_screen_image("截图里是什么"))
        self.assertFalse(should_preload_screen_image("点击确定按钮"))
        self.assertFalse(should_preload_screen_image("解释什么是电脑屏幕"))
        overview = screen_visual_plan("你知道现在屏幕是什么吗")
        self.assertEqual(
            (overview.profile, overview.model, overview.reasoning),
            ("overview", "kimi-k2.6", "disabled"),
        )
        detail = screen_visual_plan("仔细读取屏幕上的小字和表格")
        self.assertEqual(
            (detail.profile, detail.model, detail.reasoning),
            ("detail", "kimi-k3", "low"),
        )


class WindowsUIAFastPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.log_patch = patch(
            "assistant_app.performance._log_path",
            return_value=Path(self.temporary.name) / "performance.jsonl",
        )
        self.log_patch.start()

    def tearDown(self) -> None:
        self.log_patch.stop()
        self.temporary.cleanup()

    def test_unique_exact_match_invokes_without_vision(self) -> None:
        confirm = _Control("确定", "Button", rectangle=(100, 200, 180, 240))
        fast_path, _window = _fast_path([confirm, _Control("取消", "Button")])
        match = fast_path.match("点击确定按钮")
        self.assertIsNotNone(match)
        self.assertEqual(match.element.center, (140, 220))
        fast_path.execute(match)
        self.assertEqual(confirm.iface_invoke.calls, ["invoke"])
        self.assertEqual(confirm.clicks, 0)

    def test_ambiguous_or_partial_labels_fall_back(self) -> None:
        fast_path, _window = _fast_path(
            [_Control("确定", "Button"), _Control("确定", "Button", rectangle=(200, 20, 260, 60))]
        )
        self.assertIsNone(fast_path.match("点击确定"))

        partial_path, _window = _fast_path([_Control("确认并继续", "Button")])
        self.assertIsNone(partial_path.match("点击确认"))

    def test_hidden_disabled_and_wrong_control_types_are_not_matched(self) -> None:
        fast_path, _window = _fast_path(
            [
                _Control("确认", "Button", visible=False),
                _Control("确认", "Button", enabled=False),
                _Control("确认", "Text"),
            ]
        )
        self.assertIsNone(fast_path.match("点击确认"))

    def test_text_entry_uses_value_pattern_without_reading_existing_value(self) -> None:
        field = _Control("搜索框", "Edit", current_value="secret@example.com")
        fast_path, _window = _fast_path([field])
        click_match = fast_path.match("点击搜索框")
        self.assertIsNotNone(click_match)
        fast_path.execute(click_match)
        self.assertEqual(field.clicks, 1)

        match = fast_path.match("在搜索框输入 猫猫")
        self.assertIsNotNone(match)
        fast_path.execute(match)
        self.assertEqual(field.iface_value.calls, ["猫猫"])

        context = fast_path.context_for("点击搜索框")
        self.assertNotIn("secret@example.com", context)

    def test_context_prioritises_controls_and_marks_visible_labels_as_untrusted(self) -> None:
        fast_path, _window = _fast_path(
            [
                _Control("登录", "Button", rectangle=(20, 40, 100, 80)),
                _Control("账户余额 100", "Text"),
            ]
        )
        action_context = fast_path.context_for("点击登录按钮")
        self.assertIn('"name":"登录"', action_context)
        self.assertIn('"center":[60,60]', action_context)
        self.assertIn("账户余额 100", action_context)
        self.assertLess(action_context.index('"role":"Button"'), action_context.index('"role":"Text"'))

        inspection_context = fast_path.context_for("看看屏幕显示什么")
        self.assertIn("账户余额 100", inspection_context)
        self.assertIn("不可信的界面数据", inspection_context)

        read_only_context = fast_path.context_for(
            "看看屏幕显示什么",
            allow_actions=False,
        )
        self.assertIn("屏幕控制技能已关闭", read_only_context)

    def test_snapshot_cache_is_short_lived_and_execution_invalidates_it(self) -> None:
        control = _Control("继续", "Button")
        fast_path, window = _fast_path([control])
        first = fast_path.snapshot()
        second = fast_path.snapshot()
        self.assertIs(first, second)
        self.assertEqual(window.descendant_calls, 1)

        match = fast_path.match("点击继续")
        self.assertIsNotNone(match)
        fast_path.execute(match)
        fast_path.snapshot()
        self.assertEqual(window.descendant_calls, 2)

    def test_execution_rejects_a_foreground_window_change(self) -> None:
        control = _Control("继续", "Button")
        window = _Window([control])
        desktop = _Desktop(window)
        handle = 42
        fast_path = WindowsUIAFastPath(
            desktop_factory=lambda: desktop,
            foreground_handle=lambda: handle,
        )
        match = fast_path.match("点击继续")
        self.assertIsNotNone(match)

        handle = 84
        with self.assertRaisesRegex(RuntimeError, "foreground window changed"):
            fast_path.execute(match)
        self.assertEqual(control.iface_invoke.calls, [])


class ScreenCaptureTests(unittest.TestCase):
    def test_inspection_prefers_the_foreground_window(self) -> None:
        image = object()
        with (
            patch(
                "assistant_app.screen_capture.foreground_window_bbox",
                return_value=(10, 20, 1010, 720),
            ),
            patch("PIL.ImageGrab.grab", return_value=image) as grab,
        ):
            captured, mode = capture_for_inspection()
        self.assertIs(captured, image)
        self.assertEqual(mode, "foreground")
        grab.assert_called_once_with(
            bbox=(10, 20, 1010, 720),
            all_screens=True,
        )

    def test_inspection_falls_back_to_all_screens(self) -> None:
        image = object()
        with (
            patch(
                "assistant_app.screen_capture.foreground_window_bbox",
                return_value=(10, 20, 1010, 720),
            ),
            patch(
                "PIL.ImageGrab.grab",
                side_effect=[OSError("window changed"), image],
            ) as grab,
        ):
            captured, mode = capture_for_inspection()
        self.assertIs(captured, image)
        self.assertEqual(mode, "all_screens")
        self.assertEqual(grab.call_args_list[-1], call(all_screens=True))

    def test_adaptive_jpeg_profiles_bound_visual_resolution(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            source = Image.new("RGB", (2400, 1400), "white")
            overview_path = Path(temporary) / "overview.jpg"
            detail_path = Path(temporary) / "detail.jpg"
            overview = encode_screen_image(source, overview_path, "overview")
            detail = encode_screen_image(source, detail_path, "detail")
        self.assertEqual(overview.source_size, (2400, 1400))
        self.assertLessEqual(max(overview.output_size), 896)
        self.assertLessEqual(max(detail.output_size), 1536)
        self.assertGreater(max(detail.output_size), max(overview.output_size))
        self.assertGreater(overview.byte_count, 0)


class AgentFastScreenIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "test.db")
        self.log_patch = patch(
            "assistant_app.performance._log_path",
            return_value=self.root / "performance.jsonl",
        )
        self.log_patch.start()

    def tearDown(self) -> None:
        self.log_patch.stop()
        self.database.close()
        self.temporary.cleanup()

    @staticmethod
    def _config() -> dict:
        return {
            "memory": {"recent_messages": 2, "max_retrieved_memories": 2},
            "routing": {
                "model_mode": "auto",
                "k3_low_threshold": 6,
                "k3_high_threshold": 9,
                "max_tool_calls_per_task": 4,
                "max_k3_calls_per_task": 2,
                "max_screenshots_per_task": 2,
            },
            "skills": {
                "screen-inspection": True,
                "screen-control": True,
                "visual-action-shortcuts": False,
                "desk-lamp": False,
                "starrail-dailies": False,
                "browser-navigation": False,
            },
        }

    def test_unique_uia_match_bypasses_the_model(self) -> None:
        match = SimpleNamespace(command=SimpleNamespace(action="invoke"))
        calls: list[str] = []

        class FakeClient:
            budget = SimpleNamespace(currency="CNY")

            def chat(self, **_kwargs):
                raise AssertionError("unique UIA action should not call the model")

        class FakeTools:
            schemas: list[dict] = []

            def match_fast_screen_action(self, text: str):
                calls.append(f"match:{text}")
                return match

            def execute_fast_screen_action(self, matched, _task_id: str):
                self_outer.assertIs(matched, match)
                calls.append("execute")
                return ToolResult(True, "已通过 UIA 点击。")

        self_outer = self
        progress: list[str] = []
        agent = PersonalAgent(
            self._config(),
            self.database,
            FakeClient(),  # type: ignore[arg-type]
            FakeTools(),  # type: ignore[arg-type]
            "uia-direct",
            progress_callback=progress.append,
        )
        answer = agent.run("点击确定按钮", input_mode="voice")
        self.assertTrue(answer.silent)
        self.assertEqual(calls, ["match:点击确定按钮", "execute"])
        self.assertEqual(progress, ["好，我现在操作一下。"])

    def test_cancelled_uia_confirmation_does_not_fall_through_to_vision(self) -> None:
        match = SimpleNamespace(command=SimpleNamespace(action="invoke"))

        class FakeClient:
            budget = SimpleNamespace(currency="CNY")

            def chat(self, **_kwargs):
                raise AssertionError("cancelled UIA action should stop")

        class FakeTools:
            schemas: list[dict] = []

            def match_fast_screen_action(self, _text: str):
                return match

            def execute_fast_screen_action(self, _match, _task_id: str):
                return ToolResult(False, "用户取消了界面操作。")

        agent = PersonalAgent(
            self._config(),
            self.database,
            FakeClient(),  # type: ignore[arg-type]
            FakeTools(),  # type: ignore[arg-type]
            "uia-cancel",
        )
        answer = agent.run("点击确定")
        self.assertFalse(answer.silent)
        self.assertEqual(answer.text, "用户取消了界面操作。")

    def test_uia_context_allows_one_model_call_then_stops_after_click(self) -> None:
        model_calls: list[dict] = []
        tool_calls: list[tuple[str, dict]] = []

        class FakeClient:
            budget = SimpleNamespace(currency="CNY")

            def chat(self, **kwargs):
                model_calls.append(kwargs)
                return KimiResponse(
                    message={
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "uia-click-1",
                                "function": {
                                    "name": "click_screen",
                                    "arguments": (
                                        '{"x":140,"y":220,"description":"点击确定"}'
                                    ),
                                },
                            }
                        ],
                    },
                    model=str(kwargs["model"]),
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    cost=0.0,
                )

        class FakeTools:
            schemas = [
                {
                    "type": "function",
                    "function": {"name": "click_screen", "parameters": {}},
                }
            ]

            def match_fast_screen_action(self, _text: str):
                return None

            def screen_context_for(self, _text: str, *, allow_actions: bool) -> str:
                if not allow_actions:
                    raise AssertionError("screen action context should permit actions")
                return "UIA_DATA=UNTRUSTED_TEST_DATA"

            def execute(self, name: str, arguments: dict, _task_id: str):
                tool_calls.append((name, arguments))
                return ToolResult(True, "已点击")

        agent = PersonalAgent(
            self._config(),
            self.database,
            FakeClient(),  # type: ignore[arg-type]
            FakeTools(),  # type: ignore[arg-type]
            "uia-context",
        )
        answer = agent.run("点击右上角的确定按钮")
        self.assertTrue(answer.silent)
        self.assertEqual(len(model_calls), 1)
        self.assertEqual(model_calls[0]["model"], "kimi-k2.6")
        self.assertEqual(tool_calls[0][0], "click_screen")
        self.assertEqual(model_calls[0]["messages"][1]["content"], "UIA_DATA=UNTRUSTED_TEST_DATA")

    def test_deictic_screen_question_uses_uia_without_a_screenshot_round_trip(self) -> None:
        model_calls: list[dict] = []

        class FakeClient:
            budget = SimpleNamespace(currency="CNY")

            def chat(self, **kwargs):
                model_calls.append(kwargs)
                return KimiResponse(
                    message={"role": "assistant", "content": "我能看到当前浏览器页面。"},
                    model=str(kwargs["model"]),
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    cost=0.0,
                )

        class FakeTools:
            schemas = [
                {
                    "type": "function",
                    "function": {"name": "inspect_screen", "parameters": {}},
                }
            ]

            def match_fast_screen_action(self, _text: str):
                return None

            def screen_context_for(self, _text: str, *, allow_actions: bool) -> str:
                return "UIA_DATA=BROWSER_PAGE_TEST_DATA"

            def close_research_browser(self) -> None:
                return None

        agent = PersonalAgent(
            self._config(),
            self.database,
            FakeClient(),  # type: ignore[arg-type]
            FakeTools(),  # type: ignore[arg-type]
            "uia-deictic",
        )
        answer = agent.run("你能看到吗")
        self.assertEqual(answer.text, "我能看到当前浏览器页面。")
        self.assertEqual(len(model_calls), 1)
        self.assertEqual(model_calls[0]["model"], "kimi-k2.6")
        self.assertIn("BROWSER_PAGE_TEST_DATA", str(model_calls[0]["messages"]))

    def test_screen_observation_preloads_one_small_k2_visual_request(self) -> None:
        model_calls: list[dict] = []
        prepared_profiles: list[str] = []
        screenshot = self.root / "overview.jpg"
        screenshot.write_bytes(b"small-jpeg")

        class FakeClient:
            budget = SimpleNamespace(currency="CNY")

            def chat(self, **kwargs):
                model_calls.append(kwargs)
                return KimiResponse(
                    message={"role": "assistant", "content": "当前是浏览器页面。"},
                    model=str(kwargs["model"]),
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    cost=0.0,
                )

        class FakeTools:
            schemas = [
                {"type": "function", "function": {"name": "inspect_screen", "parameters": {}}},
                {"type": "function", "function": {"name": "inspect_screen_region", "parameters": {}}},
            ]

            def match_fast_screen_action(self, _text: str):
                return None

            def screen_context_for(self, _text: str, *, allow_actions: bool) -> str:
                return "UIA_DATA=BROWSER_PAGE_TEST_DATA"

            def prepare_screen_prompt(self, text: str, _task_id: str):
                prepared_profiles.append(screen_visual_plan(text).profile)
                return ToolResult(
                    True,
                    "已截取概览",
                    image_path=screenshot,
                    image_prompt="ADAPTIVE_OVERVIEW",
                )

            def close_research_browser(self) -> None:
                return None

        agent = PersonalAgent(
            self._config(),
            self.database,
            FakeClient(),  # type: ignore[arg-type]
            FakeTools(),  # type: ignore[arg-type]
            "adaptive-overview",
        )
        answer = agent.run("你知道现在屏幕是什么吗")
        self.assertEqual(answer.text, "当前是浏览器页面。")
        self.assertEqual(prepared_profiles, ["overview"])
        self.assertEqual(len(model_calls), 1)
        self.assertEqual(model_calls[0]["model"], "kimi-k2.6")
        self.assertEqual(
            [item["function"]["name"] for item in model_calls[0]["tools"]],
            ["inspect_screen_region"],
        )
        self.assertIn("data:image/jpeg;base64", str(model_calls[0]["messages"]))
        self.assertIn("ADAPTIVE_OVERVIEW", str(model_calls[0]["messages"]))

    def test_precise_screen_read_uses_one_detailed_k3_request(self) -> None:
        model_calls: list[dict] = []
        screenshot = self.root / "detail.jpg"
        screenshot.write_bytes(b"detail-jpeg")

        class FakeClient:
            budget = SimpleNamespace(currency="CNY")

            def chat(self, **kwargs):
                model_calls.append(kwargs)
                return KimiResponse(
                    message={"role": "assistant", "content": "小字已经读取。"},
                    model=str(kwargs["model"]),
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    cost=0.0,
                )

        class FakeTools:
            schemas: list[dict] = []

            def match_fast_screen_action(self, _text: str):
                return None

            def screen_context_for(self, _text: str, *, allow_actions: bool) -> str:
                return "UIA_DATA=SMALL_TEXT"

            def prepare_screen_prompt(self, text: str, _task_id: str):
                self_outer.assertEqual(screen_visual_plan(text).profile, "detail")
                return ToolResult(True, "已截取精细图", screenshot, "DETAIL_IMAGE")

            def close_research_browser(self) -> None:
                return None

        self_outer = self
        agent = PersonalAgent(
            self._config(),
            self.database,
            FakeClient(),  # type: ignore[arg-type]
            FakeTools(),  # type: ignore[arg-type]
            "adaptive-detail",
        )
        answer = agent.run("仔细读取屏幕上的小字和表格")
        self.assertEqual(answer.text, "小字已经读取。")
        self.assertEqual(len(model_calls), 1)
        self.assertEqual(model_calls[0]["model"], "kimi-k3")

    def test_region_zoom_crops_the_cached_source_instead_of_recapturing(self) -> None:
        from PIL import Image

        screenshots = self.root / "screenshots"
        screenshots.mkdir(exist_ok=True)
        tools = ToolRegistry(
            AppPaths(
                self.root,
                self.root,
                self.root / "test.db",
                screenshots,
                self.root / "api_key.txt",
            ),
            self.database,
            "region-zoom",
            lambda *_args: True,
            skills_config={"screen-inspection": True},
        )
        try:
            source = Image.new("RGB", (1792, 896), "white")
            with patch(
                "assistant_app.screen_capture.capture_screen_frame",
                return_value=CapturedScreen(
                    source,
                    "foreground",
                    (100, 200, 1892, 1096),
                ),
            ):
                overview = tools.execute(
                    "inspect_screen",
                    {"profile": "overview"},
                    "region-task",
                )
            self.assertTrue(overview.success, overview.content)
            result = tools.execute(
                "inspect_screen_region",
                {"left": 250, "top": 100, "right": 500, "bottom": 300},
                "region-task",
            )
            self.assertTrue(result.success, result.content)
            self.assertTrue(result.image_path.is_file())
            self.assertIn("[600,400,1100,800]", result.image_prompt)
            self.assertIn("按需高清局部", result.image_prompt)
            stale = tools.execute(
                "inspect_screen_region",
                {"left": 250, "top": 100, "right": 500, "bottom": 300},
                "different-task",
            )
            self.assertFalse(stale.success)
            self.assertIn("先调用 inspect_screen", stale.content)
        finally:
            tools.close()

    def test_tool_registry_reuses_existing_confirmation_boundary_for_uia(self) -> None:
        screenshots = self.root / "screenshots"
        screenshots.mkdir()
        paths = AppPaths(
            self.root,
            self.root,
            self.root / "test.db",
            screenshots,
            self.root / "api_key.txt",
        )
        confirmations: list[tuple[str, dict]] = []
        tools = ToolRegistry(
            paths,
            self.database,
            "uia-tool",
            lambda name, arguments: confirmations.append((name, arguments)) or True,
            skills_config={"screen-inspection": True, "screen-control": True},
        )
        control = _Control("确定", "Button", rectangle=(100, 200, 180, 240))
        match = UIAMatch(
            ScreenCommand("invoke", "确定"),
            UIASnapshot(42, "Example", (), 0.0),
            UIAElement("确定", "Button", "Confirm", (100, 200, 180, 240), control),
        )

        class FakeFastScreen:
            def execute(self, received: UIAMatch) -> None:
                self_outer.assertIs(received, match)
                control.iface_invoke.Invoke()

        self_outer = self
        tools._fast_screen = FakeFastScreen()  # type: ignore[assignment]
        with patch.object(tools, "_window_context", return_value=("example.exe", "Example")):
            result = tools.execute_fast_screen_action(match, "uia-task")

        self.assertTrue(result.success)
        self.assertEqual(control.iface_invoke.calls, ["invoke"])
        self.assertEqual(confirmations[0][0], "click_screen")
        self.assertEqual(confirmations[0][1]["_target_app"], "example.exe")
        self.assertEqual(confirmations[0][1]["description"], "点击“确定”")

    def test_disabled_screen_inspection_never_collects_uia_context(self) -> None:
        config = self._config()
        config["skills"]["screen-inspection"] = False
        model_calls: list[dict] = []

        class FakeClient:
            budget = SimpleNamespace(currency="CNY")

            def chat(self, **kwargs):
                model_calls.append(kwargs)
                return KimiResponse(
                    message={"role": "assistant", "content": "请描述要查看的内容。"},
                    model=str(kwargs["model"]),
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    cost=0.0,
                )

        class FakeTools:
            schemas: list[dict] = []

            def match_fast_screen_action(self, _text: str):
                raise AssertionError("disabled inspection must not enumerate UIA")

            def screen_context_for(self, _text: str):
                raise AssertionError("disabled inspection must not expose UIA context")

            def close_research_browser(self) -> None:
                return None

        agent = PersonalAgent(
            config,
            self.database,
            FakeClient(),  # type: ignore[arg-type]
            FakeTools(),  # type: ignore[arg-type]
            "uia-disabled",
        )
        answer = agent.run("看看屏幕显示什么")
        self.assertEqual(answer.text, "请描述要查看的内容。")
        self.assertEqual(len(model_calls), 1)
        self.assertFalse(any("UIA_DATA" in str(message) for message in model_calls[0]["messages"]))

    def test_task_tool_boundary_marks_uia_context_as_read_only(self) -> None:
        model_calls: list[dict] = []
        context_permissions: list[bool] = []

        class FakeClient:
            budget = SimpleNamespace(currency="CNY")

            def chat(self, **kwargs):
                model_calls.append(kwargs)
                return KimiResponse(
                    message={"role": "assistant", "content": "当前窗口包含一个确定按钮。"},
                    model=str(kwargs["model"]),
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    cost=0.0,
                )

        class FakeTools:
            schemas: list[dict] = []

            def match_fast_screen_action(self, _text: str):
                raise AssertionError("read-only task must not match direct actions")

            def screen_context_for(self, _text: str, *, allow_actions: bool) -> str:
                context_permissions.append(allow_actions)
                return "UIA_DATA=READ_ONLY_TEST_DATA"

            def close_research_browser(self) -> None:
                return None

        agent = PersonalAgent(
            self._config(),
            self.database,
            FakeClient(),  # type: ignore[arg-type]
            FakeTools(),  # type: ignore[arg-type]
            "uia-read-only",
        )
        answer = agent.run(
            "看看屏幕显示什么",
            allowed_tools={"inspect_screen"},
        )
        self.assertEqual(answer.text, "当前窗口包含一个确定按钮。")
        self.assertEqual(context_permissions, [False])
        self.assertIn("READ_ONLY_TEST_DATA", str(model_calls[0]["messages"]))


if __name__ == "__main__":
    unittest.main()
