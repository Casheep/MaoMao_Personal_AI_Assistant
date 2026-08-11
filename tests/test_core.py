from __future__ import annotations

import json
import tempfile
import threading
import unittest
import sqlite3
import wave
import zipfile
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from assistant_app.budget import BudgetManager
from assistant_app.agent import OperationCancelled, PersonalAgent
from assistant_app.database import Database
from assistant_app.router import choose_route
from assistant_app.audio import (
    APIReconnectFailed,
    ButtonAudioRecorder,
    SpeechTranscriber,
    WakeVoiceVerifier,
    WakeWordListener,
    play_wav_file,
)
from assistant_app.tts import SpeechSynthesizer
from assistant_app.workers.tts_worker import AlternateTTSWorker
from assistant_app.workers.cosyvoice_worker import CosyVoiceWorker
from assistant_app.config import AppPaths, save_local_settings
from assistant_app.components import (
    COMPONENTS,
    component_installed,
    distribution_edition,
    extract_component_archive,
    missing_startup_components,
)
from assistant_app.providers import HybridModelClient, KimiResponse
from assistant_app.secrets import (
    key_configuration_status,
    load_kimi_key,
    load_mimo_key,
    save_api_keys,
)
from assistant_app.skills import LOCAL_SKILLS, match_local_skill
from assistant_app.skills import SKILL_CATALOG, SKILL_CATEGORY_ORDER
from assistant_app.tools import ToolRegistry, ToolResult
from assistant_app.gui import AssistantWindow
from assistant_app.rounded_gui import RoundedAssistantWindow


ROUTING = {
    "k3_low_threshold": 6,
    "k3_high_threshold": 9,
}


class LearnedActionsAndPermissionTests(unittest.TestCase):
    def test_optional_component_status_uses_required_model_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertFalse(component_installed("wake-word-model", root))
            model = root / "engines/vosk/vosk-model-small-cn-0.22/am/final.mdl"
            model.parent.mkdir(parents=True)
            model.write_bytes(b"model")
            self.assertTrue(component_installed("wake-word-model", root))

    def test_lite_edition_downloads_missing_startup_components(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "release-manifest.json").write_text(
                json.dumps({"edition": "lite"}), encoding="utf-8"
            )
            self.assertEqual(distribution_edition(root), "lite")
            self.assertEqual(missing_startup_components(root), ["wake-word-model"])
            model = root / "engines/vosk/vosk-model-small-cn-0.22/am/final.mdl"
            model.parent.mkdir(parents=True)
            model.write_bytes(b"model")
            self.assertEqual(missing_startup_components(root), [])

    def test_full_edition_never_downloads_startup_components(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "release-manifest.json").write_text(
                json.dumps({"edition": "full"}), encoding="utf-8"
            )
            self.assertEqual(missing_startup_components(root), [])

    def test_component_archive_rejects_executable_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "component.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("vosk-model-small-cn-0.22/payload.exe", b"not executable")
            with self.assertRaisesRegex(RuntimeError, "不允许的文件类型"):
                extract_component_archive(archive_path, root / "extracted", COMPONENTS[0])

    def test_empty_public_key_file_is_valid_but_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "api_key.txt"
            path.write_text("kimi_key=\nmimo_key=\n", encoding="utf-8")
            status = key_configuration_status(path)
            self.assertFalse(status["kimi_key"])
            self.assertFalse(status["mimo_key"])

    def test_api_key_settings_preserve_the_provider_left_blank(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "api_key.txt"
            path.write_text("kimi_key=test-old-kimi\nmimo_key=test-old-mimo\n", encoding="utf-8")
            save_api_keys(path, mimo_key="test-new-mimo")
            self.assertEqual(load_kimi_key(path), "test-old-kimi")
            self.assertEqual(load_mimo_key(path), "test-new-mimo")
            status = key_configuration_status(path)
            self.assertTrue(status["kimi_key"])
            self.assertTrue(status["mimo_key"])

    def test_visual_action_database_upserts_and_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "assistant.db")
            try:
                first = database.remember_visual_action(
                    "点击开始匹配按钮",
                    "game.exe",
                    "竞技平台",
                    0.75,
                    0.80,
                    (0, 0, 1920, 1080),
                )
                second = database.remember_visual_action(
                    "点击开始匹配按钮",
                    "game.exe",
                    "竞技平台",
                    0.70,
                    0.78,
                    (0, 0, 1920, 1080),
                )
                self.assertEqual(first, second)
                saved = database.visual_action(first)
                self.assertIsNotNone(saved)
                self.assertAlmostEqual(float(saved["x_ratio"]), 0.70)
                database.mark_visual_action_used(first)
                self.assertEqual(database.visual_action(first)["use_count"], 1)
                database.delete_visual_action(first)
                self.assertIsNone(database.visual_action(first))
            finally:
                database.close()

    def test_click_uses_native_windows_path_and_learns_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            screenshots = root / "screenshots"
            screenshots.mkdir()
            database = Database(root / "assistant.db")
            confirmations: list[dict] = []
            tools = ToolRegistry(
                AppPaths(root, root, root / "assistant.db", screenshots, root / "api_key.txt"),
                database,
                "click-test",
                lambda _name, arguments: confirmations.append(arguments) or True,
            )
            try:
                with (
                    patch.object(tools, "_window_context", return_value=("game.exe", "竞技平台")),
                    patch.object(tools, "_virtual_screen_bounds", return_value=(0, 0, 1920, 1080)),
                    patch.object(tools, "_restore_foreground"),
                    patch.object(tools, "_native_click") as native_click,
                ):
                    result = tools.execute(
                        "click_screen",
                        {"x": 1175, "y": 742, "description": "点击开始匹配按钮"},
                        "click-task",
                    )
                self.assertTrue(result.success, result.content)
                native_click.assert_called_once_with(1175, 742)
                self.assertEqual(confirmations[0]["_target_app"], "game.exe")
                self.assertEqual(len(database.list_visual_actions()), 1)
            finally:
                database.close()

    def test_later_thinking_cue_never_reuses_first_stage_phrase(self) -> None:
        window = AssistantWindow.__new__(AssistantWindow)
        window.thinking_first_filler_texts = ["我看看。"]
        window.thinking_followup_texts = ["稍等一下哦。"]
        window.thinking_filler_texts = ["我看看。", "稍等一下哦。"]
        window.thinking_filler_max_count = 2
        window.thinking_filler_interval = 0.0
        window.thinking_long_wait_after_count = 99
        window.thinking_long_wait_texts = {}
        window._thinking_progress_task_started = 1.0
        window._thinking_progress_count = 0
        window._thinking_action_context = ""
        window._last_progress_speech_at = 0.0
        window.closing = False
        window._active_task_started = 1.0
        window.root = Mock()
        window.status = Mock()
        window._thinking_audio_done = threading.Event()
        spoken: list[str] = []
        window._thinking_voice_clip = lambda text, *_args: spoken.append(text) or Path("cue.wav")
        with patch("assistant_app.gui.play_wav_file"):
            window._thinking_filler_worker(1.0, threading.Event())
        self.assertEqual(spoken, ["我看看。", "稍等一下哦。"])


class RouterTests(unittest.TestCase):
    def test_routine_uses_k2_without_thinking(self) -> None:
        route = choose_route("现在几点", ROUTING)
        self.assertEqual((route.model, route.reasoning), ("kimi-k2.6", "disabled"))

    def test_complex_screen_work_uses_k3(self) -> None:
        route = choose_route("仔细分析这个报错，然后点击界面按钮", ROUTING)
        self.assertEqual((route.model, route.reasoning), ("kimi-k3", "low"))

    def test_simple_image_and_screen_requests_force_k3_in_auto_mode(self) -> None:
        for request in (
            "帮我看看这张图片",
            "截图里是什么？",
            "看看屏幕上显示了什么",
            "点击界面右上角的按钮",
            "Please inspect this screenshot",
        ):
            route = choose_route(request, ROUTING)
            self.assertEqual((route.model, route.reasoning), ("kimi-k3", "low"))
            self.assertIn("图像", route.reasons[0])

    def test_manual_k2_still_overrides_visual_auto_routing(self) -> None:
        route = choose_route(
            "看看这张图片",
            {**ROUTING, "model_mode": "k2.6"},
        )
        self.assertEqual((route.model, route.reasoning), ("kimi-k2.6", "disabled"))

    def test_explicit_max_wins(self) -> None:
        route = choose_route("请用最强模型处理", ROUTING)
        self.assertEqual((route.model, route.reasoning), ("kimi-k3", "max"))

    def test_manual_k2_mode_stays_on_k2(self) -> None:
        route = choose_route("请仔细分析这个复杂代码库", {**ROUTING, "model_mode": "k2.6"})
        self.assertEqual((route.model, route.reasoning), ("kimi-k2.6", "disabled"))

    def test_manual_k3_mode_uses_k3_for_routine_chat(self) -> None:
        route = choose_route("你好", {**ROUTING, "model_mode": "k3"})
        self.assertEqual((route.model, route.reasoning), ("kimi-k3", "low"))

    def test_manual_mimo_modes_are_separate_from_kimi(self) -> None:
        pro = choose_route("你好", {**ROUTING, "model_mode": "mimo-pro"})
        omni = choose_route("你好", {**ROUTING, "model_mode": "mimo-omni"})
        self.assertEqual((pro.model, pro.reasoning), ("mimo-v2.5-pro", "enabled"))
        self.assertEqual((omni.model, omni.reasoning), ("mimo-v2.5", "disabled"))


class TTSConfigurationTests(unittest.TestCase):
    def test_beta_hides_local_tts_engines(self) -> None:
        tts = SpeechSynthesizer(
            {
                "enabled": True,
                "backend": "mimo-api",
                "allow_local_engines": False,
                "mimo_voice": "冰糖",
            }
        )
        self.assertEqual(tts.available_engines(), [tts.engine_label])

    def test_beta_asr_does_not_construct_a_local_fallback(self) -> None:
        transcriber = SpeechTranscriber(
            {
                "backend": "mimo-api",
                "allow_local_fallback": False,
                "mimo_base_url": "https://api.xiaomimimo.com/v1",
            }
        )
        try:
            self.assertIsNone(transcriber.fallback)
        finally:
            transcriber.close()

    def test_api_asr_never_constructs_local_fallback_even_if_old_setting_is_true(self) -> None:
        transcriber = SpeechTranscriber(
            {
                "backend": "mimo-api",
                "allow_local_fallback": True,
                "mimo_base_url": "https://api.xiaomimimo.com/v1",
            }
        )
        try:
            self.assertIsNone(transcriber.fallback)
        finally:
            transcriber.close()

    def test_api_asr_rebuilds_connection_and_retries_same_recording(self) -> None:
        transcriber = SpeechTranscriber(
            {"backend": "mimo-api", "mimo_base_url": "https://api.xiaomimimo.com/v1"}
        )
        primary = Mock()
        primary.transcribe.side_effect = [RuntimeError("stale"), "重新连接成功"]
        transcriber.primary = primary
        notices: list[str] = []
        transcriber.set_reconnect_callback(notices.append)

        result = transcriber.transcribe(Path("recording.wav"))

        self.assertEqual(result, "重新连接成功")
        primary.reconnect.assert_called_once_with()
        self.assertEqual(primary.transcribe.call_count, 2)
        self.assertEqual(notices, ["语音转写"])

    def test_api_asr_asks_host_after_reconnect_fails_without_loading_qwen(self) -> None:
        transcriber = SpeechTranscriber(
            {"backend": "mimo-api", "mimo_base_url": "https://api.xiaomimimo.com/v1"}
        )
        primary = Mock()
        primary.transcribe.side_effect = [RuntimeError("offline"), RuntimeError("still offline")]
        transcriber.primary = primary
        with patch("assistant_app.audio.QwenASRTranscriber") as qwen:
            with self.assertRaises(APIReconnectFailed):
                transcriber.transcribe(Path("recording.wav"))
        qwen.assert_not_called()

    def test_mimo_tts_reconnects_without_sapi_fallback(self) -> None:
        tts = SpeechSynthesizer(
            {
                "enabled": True,
                "backend": "mimo-api",
                "mimo_voice": "冰糖",
                "fallback_to_sapi": True,
            }
        )
        notices: list[str] = []
        tts.set_reconnect_callback(notices.append)
        stats = {"chunks": 1, "first_audio_seconds": 0.2, "generation_seconds": 0.4}
        with (
            patch.object(tts, "_send_worker_request", side_effect=[RuntimeError("stale"), stats]),
            patch.object(tts, "_restart_api_worker") as restart,
            patch.object(tts, "_speak_sapi") as sapi,
        ):
            result = tts.speak("你好")
        restart.assert_called_once_with()
        sapi.assert_not_called()
        self.assertEqual(notices, ["语音生成"])
        self.assertEqual(result["chunks"], 1)

    def test_beta_config_contains_no_personal_device_settings(self) -> None:
        config = json.loads(Path("config.json").read_text(encoding="utf-8"))
        overrides = json.loads(Path("release/beta-overrides.json").read_text(encoding="utf-8"))
        serialized = json.dumps([config, overrides], ensure_ascii=False)
        self.assertEqual(config["distribution"]["channel"], "beta")
        self.assertEqual(config["audio"]["backend"], "mimo-api")
        self.assertEqual(config["tts"]["backend"], "mimo-api")
        self.assertFalse(config["audio"]["allow_local_fallback"])
        self.assertIn("vosk-model-small-cn", config["wake_word"]["model_path"])
        self.assertNotIn("192.168.", serialized)
        self.assertNotIn("PERSONAL_INSTALL_PATH", serialized)

    def test_public_beta_does_not_bundle_publisher_keys(self) -> None:
        build_script = Path("build-beta-packages.ps1").read_text(encoding="utf-8")
        self.assertNotIn("[string]$KeyFile", build_script)
        self.assertNotIn("Copy-Item -LiteralPath $resolvedKeyFile", build_script)
        self.assertIn("bundled_keys = $false", build_script)
        self.assertIn('"kimi_key=`nmimo_key=`n"', build_script)
        self.assertNotIn("release\\README.md", build_script)
        self.assertFalse(Path("release/README.md").exists())

    def test_beta_packages_are_local_only_and_support_lite_and_full_editions(self) -> None:
        build_script = Path("build-beta-packages.ps1").read_text(encoding="utf-8")
        self.assertFalse(Path(".github/workflows/build-beta.yml").exists())
        self.assertIn("[ValidateSet('lite', 'full')]", build_script)
        self.assertIn('MaoMao-beta-$Edition', build_script)
        self.assertIn("startup_component_policy", build_script)
        self.assertIn("$runtimeLibrary = Join-Path $runtimeTarget 'Lib'", build_script)
        self.assertIn("Local launcher compilation is disabled by default.", build_script)
        self.assertNotIn("gh release create", build_script)
        self.assertNotIn("refs/tags", build_script)

    def setUp(self) -> None:
        self.tts = SpeechSynthesizer(
            {
                "enabled": False,
                "backend": "cosyvoice3",
                "reference_audio": "voices/default.wav",
            }
        )

    def test_uses_continuous_text_instead_of_manual_sentence_chunks(self) -> None:
        cleaned = self.tts._clean_text("好的。 我会先检查文件，然后告诉你结果。")
        self.assertEqual(cleaned, "好的。 我会先检查文件，然后告诉你结果。")

    def test_tts_reads_slashes_as_or_by_default(self) -> None:
        self.assertEqual(
            self.tts._clean_text("暂停/继续，开／关"),
            "暂停或者继续，开或者关",
        )
        self.assertEqual(
            self.tts._clean_text("详情见 https://example.com/a/b 或 A/B"),
            "详情见 链接 或 A或者B",
        )

    def test_worker_request_uses_cosyvoice_model(self) -> None:
        request = self.tts._request("speak", "测试")
        self.assertEqual(request["model_dir"], "pretrained_models/Fun-CosyVoice3-0.5B")
        self.assertEqual(request["speed"], 1.0)

    def test_cosyvoice_is_the_default_engine(self) -> None:
        tts = SpeechSynthesizer({"enabled": False})
        self.assertEqual(tts.backend, "cosyvoice3")
        self.assertEqual(tts.available_voices(), ["default"])

    def test_all_local_tts_engines_are_selectable(self) -> None:
        tts = SpeechSynthesizer({"enabled": False})
        for backend, label in (
            ("f5tts", "本地 · F5-TTS（约 0.7 GB 显存）"),
            ("cosyvoice3", "本地 · CosyVoice 3（约 4.2 GB 显存）"),
            ("mimo-api", "API · MiMo-V2.5-TTS（0 GB 显存）"),
        ):
            tts.set_engine(backend)
            self.assertEqual(tts.engine_label, label)

    def test_alternate_worker_request_has_engine_specific_settings(self) -> None:
        tts = SpeechSynthesizer({"enabled": False, "backend": "f5tts"})
        request = tts._request("speak", "测试")
        self.assertEqual(request["backend"], "f5tts")
        self.assertEqual(request["f5_nfe_steps"], 24)
        self.assertEqual(request["leading_silence_ms"], 220)
        self.assertEqual(request["trailing_silence_ms"], 180)

    def test_alternate_synthesis_adds_leading_and_trailing_silence(self) -> None:
        worker = AlternateTTSWorker()
        audio = np.full(2400, 0.25, dtype=np.float32)
        stats = {"chunks": 1, "first_audio_seconds": 0.1, "generation_seconds": 0.2}
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "padded.wav"
            with patch.object(worker, "_generate", return_value=([audio], 24000, stats)):
                should_exit, result = worker.handle(
                    {
                        "op": "synthesize",
                        "output_path": str(output),
                        "leading_silence_ms": 220,
                        "trailing_silence_ms": 180,
                    }
                )
            import soundfile as sf

            rendered, sample_rate = sf.read(output, dtype="float32")
            self.assertFalse(should_exit)
            self.assertEqual(result, stats)
            self.assertEqual(sample_rate, 24000)
            self.assertTrue(np.all(rendered[: 24 * 220] == 0))
            self.assertTrue(np.all(rendered[-24 * 180 :] == 0))
            self.assertGreater(np.max(np.abs(rendered[24 * 220 : -24 * 180])), 0.2)

    def test_thinking_clip_uses_extra_front_and_back_padding(self) -> None:
        tts = SpeechSynthesizer({"enabled": False, "backend": "mimo-api", "mimo_voice": "冰糖"})
        requests: list[dict] = []

        def render(request):
            requests.append(request)
            rate = 16000
            leading = np.zeros(
                int(rate * request["leading_silence_ms"] / 1000), dtype=np.int16
            )
            voice = np.full(1600, 800, dtype=np.int16)
            trailing = np.zeros(
                int(rate * request["trailing_silence_ms"] / 1000), dtype=np.int16
            )
            with wave.open(request["output_path"], "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(rate)
                output.writeframes(np.concatenate([leading, voice, trailing]).tobytes())
            return {}

        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(tts, "_send_worker_request", side_effect=render):
                path = tts.synthesize_cached_voice_clip(
                    "嗯，我想一下。",
                    Path(temporary),
                    "thinking-filler-01",
                    leading_silence_ms=320,
                    trailing_silence_ms=420,
                )
            self.assertTrue(path.is_file())
        self.assertEqual(requests[0]["leading_silence_ms"], 320)
        self.assertEqual(requests[0]["trailing_silence_ms"], 420)

    def test_preload_prepares_all_thinking_fillers_for_selected_voice(self) -> None:
        window = AssistantWindow.__new__(AssistantWindow)
        window.tts = Mock()
        window.goodbye_text = "拜拜。"
        window.goodbye_cache_dir = Path("voice-cache")
        window.wake_response_texts = ["我在呢。"]
        window.interrupt_response_texts = ["嗯？"]
        window.thinking_filler_texts = ["我想想。", "稍等一下哦。"]
        window.thinking_long_wait_texts = {"general": "还需要一点时间。"}
        window.thinking_completion_texts = ["我处理好了。"]

        window._prepare_voice_clips()

        calls = window.tts.synthesize_cached_voice_clip.call_args_list
        self.assertEqual(len(calls), 9)
        self.assertEqual(calls[0].args[2], "api-reconnecting")
        self.assertEqual(calls[1].args[2], "api-unavailable")
        self.assertEqual(calls[5].args[2], "thinking-filler-01")
        self.assertEqual(calls[6].args[2], "thinking-filler-02")
        self.assertEqual(calls[7].args[2], "thinking-long-general")
        self.assertEqual(calls[8].args[2], "thinking-complete-01")
        self.assertEqual(calls[-1].kwargs["leading_silence_ms"], 320)
        self.assertEqual(calls[-1].kwargs["trailing_silence_ms"], 420)

    def test_slow_voice_task_plays_prepared_filler_but_fast_task_does_not(self) -> None:
        window = AssistantWindow.__new__(AssistantWindow)
        window.thinking_filler_texts = ["我想想。"]
        window.thinking_filler_max_count = 1
        window.thinking_filler_interval = 0.0
        window.thinking_long_wait_after_count = 99
        window.thinking_long_wait_texts = {}
        window._thinking_progress_task_started = 12.0
        window._thinking_progress_count = 0
        window._thinking_action_context = ""
        window._last_progress_speech_at = 0.0
        window.closing = False
        window._active_task_started = 12.0
        window.goodbye_cache_dir = Path("voice-cache")
        window.tts = Mock()
        window.tts.cached_voice_clip_path.return_value = Path("prepared.wav")
        window.tts._cached_clip_is_audible.return_value = True
        window.root = Mock()
        window.status = Mock()
        window._thinking_audio_done = threading.Event()
        window._thinking_audio_done.set()

        with (
            patch("assistant_app.gui.Path.is_file", return_value=True),
            patch("assistant_app.gui.play_wav_file") as play,
        ):
            window._thinking_filler_worker(12.0, threading.Event())
            play.assert_called_once_with(Path("prepared.wav"))

            stopped = threading.Event()
            stopped.set()
            window._thinking_filler_worker(12.0, stopped)
            play.assert_called_once()
        self.assertTrue(window._thinking_audio_done.is_set())

    def test_thinking_fillers_wait_full_interval_after_each_spoken_clip(self) -> None:
        window = AssistantWindow.__new__(AssistantWindow)
        window.thinking_filler_texts = ["我想想。", "稍等一下。"]
        window.thinking_filler_max_count = 2
        window.thinking_filler_interval = 4.0
        window.thinking_long_wait_after_count = 99
        window.thinking_long_wait_texts = {}
        window._thinking_progress_task_started = 20.0
        window._thinking_progress_count = 0
        window._thinking_action_context = ""
        window._last_progress_speech_at = 0.0
        window.closing = False
        window._active_task_started = 20.0
        window.goodbye_cache_dir = Path("voice-cache")
        window.tts = Mock()
        window.tts.cached_voice_clip_path.side_effect = [
            Path("first.wav"),
            Path("second.wav"),
        ]
        window.tts._cached_clip_is_audible.return_value = True
        window.root = Mock()
        window.status = Mock()
        window._thinking_audio_done = threading.Event()
        window._thinking_audio_done.set()

        class RecordingStop:
            def __init__(self):
                self.waits = []
                self.clock = 0.0

            def wait(self, seconds):
                self.waits.append(seconds)
                self.clock += seconds
                return False

            def is_set(self):
                return False

        stop = RecordingStop()
        with (
            patch("assistant_app.gui.random.shuffle", side_effect=lambda values: None),
            patch("assistant_app.gui.time.monotonic", side_effect=lambda: stop.clock),
            patch("assistant_app.gui.Path.is_file", return_value=True),
            patch("assistant_app.gui.play_wav_file") as play,
        ):
            window._thinking_filler_worker(20.0, stop)  # type: ignore[arg-type]

        self.assertAlmostEqual(sum(stop.waits), 8.0)
        self.assertEqual(
            play.call_args_list,
            [call(Path("first.wav")), call(Path("second.wav"))],
        )

    def test_screen_action_notice_resets_the_silence_interval(self) -> None:
        window = AssistantWindow.__new__(AssistantWindow)
        window.thinking_filler_texts = ["我想想。"]
        window.thinking_filler_max_count = 1
        window.thinking_filler_interval = 4.0
        window.thinking_long_wait_after_count = 99
        window.thinking_long_wait_texts = {}
        window._thinking_progress_task_started = 30.0
        window._thinking_progress_count = 0
        window._thinking_action_context = "我先看一下屏幕。"
        window._last_progress_speech_at = 5.0
        window.closing = False
        window._active_task_started = 30.0
        window._action_notice_task_started = 30.0
        window._action_notice_done = threading.Event()
        window._action_notice_done.set()
        window.goodbye_cache_dir = Path("voice-cache")
        window.tts = Mock()
        window.tts.cached_voice_clip_path.return_value = Path("second.wav")
        window.tts._cached_clip_is_audible.return_value = True
        window.root = Mock()
        window.status = Mock()
        window._thinking_audio_done = threading.Event()
        window._thinking_audio_done.set()

        class RecordingStop:
            def __init__(self):
                self.waits = []
                self.clock = 5.0

            def wait(self, seconds):
                self.waits.append(seconds)
                self.clock += seconds
                return False

            def is_set(self):
                return False

        stop = RecordingStop()
        with (
            patch("assistant_app.gui.random.shuffle", side_effect=lambda values: None),
            patch("assistant_app.gui.time.monotonic", side_effect=lambda: stop.clock),
            patch("assistant_app.gui.Path.is_file", return_value=True),
            patch("assistant_app.gui.play_wav_file") as play,
        ):
            window._thinking_filler_worker(30.0, stop)  # type: ignore[arg-type]

        self.assertAlmostEqual(sum(stop.waits), 4.0)
        play.assert_called_once_with(Path("second.wav"))
        window.tts.cached_voice_clip_path.assert_called_once()

    def test_long_wait_reason_matches_current_task_context(self) -> None:
        self.assertEqual(
            AssistantWindow._thinking_context_kind("我先看一下屏幕。"),
            "screen",
        )
        self.assertEqual(
            AssistantWindow._thinking_context_kind("我去查查网页资料。"),
            "web",
        )
        self.assertEqual(
            AssistantWindow._thinking_context_kind("正在执行电脑操作。"),
            "action",
        )
        self.assertEqual(AssistantWindow._thinking_context_kind("我想一想。"), "general")

    def test_live_generated_progress_clip_is_kept_for_future_reuse(self) -> None:
        window = AssistantWindow.__new__(AssistantWindow)
        window.goodbye_cache_dir = Path("voice-cache")
        window.tts = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            clip = Path(temporary) / "thinking-dynamic.wav"
            window.tts.cached_voice_clip_path.return_value = clip
            window.tts._cached_clip_is_audible.return_value = True

            def generate(*_args, **_kwargs):
                clip.write_bytes(b"persistent voice clip")
                return clip

            window.tts.synthesize_cached_voice_clip.side_effect = generate
            first = window._thinking_voice_clip(
                "这个步骤需要额外检查。",
                "thinking-dynamic-check",
            )
            second = window._thinking_voice_clip(
                "这个步骤需要额外检查。",
                "thinking-dynamic-check",
            )

        self.assertEqual(first, clip)
        self.assertEqual(second, clip)
        window.tts.synthesize_cached_voice_clip.assert_called_once()
        self.assertEqual(
            window.tts.synthesize_cached_voice_clip.call_args.kwargs["trailing_silence_ms"],
            420,
        )

    def test_completed_long_task_announces_completion_before_answer(self) -> None:
        window = AssistantWindow.__new__(AssistantWindow)
        task_started = 45.0
        window._thinking_audio_done = threading.Event()
        window._thinking_audio_done.set()
        window._action_notice_done = threading.Event()
        window._action_notice_done.set()
        window._action_notice_task_started = task_started
        window._thinking_progress_task_started = task_started
        window._thinking_progress_count = 1
        window.thinking_completion_texts = ["我处理好了。"]
        window.closing = False
        window._active_task_started = task_started
        window.root = Mock()
        window.tts = Mock()
        window.tts.speak.return_value = {
            "chunks": 1,
            "first_audio_seconds": 0.1,
            "generation_seconds": 0.2,
        }
        events = []
        window._thinking_voice_clip = Mock(
            side_effect=lambda *_args, **_kwargs: events.append("prepare") or Path("done.wav")
        )
        window.tts.speak.side_effect = lambda *_args, **_kwargs: events.append("answer") or {
            "chunks": 1,
            "first_audio_seconds": 0.1,
            "generation_seconds": 0.2,
        }

        with (
            patch("assistant_app.gui.random.randrange", return_value=0),
            patch("assistant_app.gui.play_wav_file", side_effect=lambda _path: events.append("complete")),
        ):
            window._speak_worker("这是最终答案。", "coherent", "voice", task_started, 0.2, 3.0)

        self.assertEqual(events, ["prepare", "complete", "answer"])

    def test_mimo_preload_announces_completion_with_selected_engine(self) -> None:
        worker = AlternateTTSWorker()
        stats = {
            "chunks": 1,
            "first_audio_seconds": 0.5,
            "generation_seconds": 0.6,
            "peak_vram_gb": 0.0,
            "resident_vram_gb": 0.0,
        }
        with patch.object(worker, "_generate", return_value=([], 24000, stats)) as generate:
            should_exit, result = worker.handle(
                {
                    "op": "warmup",
                    "backend": "mimo-api",
                    "preload_text": "猫猫已经预热完成啦！",
                }
            )
        self.assertFalse(should_exit)
        self.assertEqual(result, stats)
        request = generate.call_args.args[0]
        self.assertEqual(request["text"], "猫猫已经预热完成啦！")
        self.assertTrue(generate.call_args.kwargs["play"])

    def test_cosy_preload_uses_the_same_spoken_announcement(self) -> None:
        worker = CosyVoiceWorker()
        stats = {"chunks": 1, "first_audio_seconds": 0.5, "generation_seconds": 0.6}
        with patch.object(worker, "_generate", return_value=([], 24000, stats)) as generate:
            should_exit, result = worker.handle(
                {
                    "op": "warmup",
                    "backend": "cosyvoice3",
                    "preload_text": "猫猫已经预热完成啦！",
                }
            )
        self.assertFalse(should_exit)
        self.assertEqual(result, stats)
        self.assertEqual(generate.call_args.args[0]["text"], "猫猫已经预热完成啦！")
        self.assertTrue(generate.call_args.kwargs["play"])

    def test_cosy_synthesis_adds_leading_and_trailing_silence(self) -> None:
        worker = CosyVoiceWorker()
        audio = np.full(2400, 2000, dtype=np.int16)
        stats = {"chunks": 1, "first_audio_seconds": 0.1, "generation_seconds": 0.2}
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "cosy-padded.wav"
            with patch.object(worker, "_generate", return_value=([audio], 24000, stats)):
                should_exit, result = worker.handle(
                    {
                        "op": "synthesize",
                        "output_path": str(output),
                        "leading_silence_ms": 220,
                        "trailing_silence_ms": 180,
                    }
                )
            import soundfile as sf

            rendered, sample_rate = sf.read(output, dtype="int16")
            self.assertFalse(should_exit)
            self.assertEqual(result, stats)
            self.assertEqual(sample_rate, 24000)
            self.assertTrue(np.all(rendered[: 24 * 220] == 0))
            self.assertTrue(np.all(rendered[-24 * 180 :] == 0))
            self.assertGreater(np.max(np.abs(rendered[24 * 220 : -24 * 180])), 1000)

    def test_title_bar_close_is_bound_to_tray_minimize(self) -> None:
        source = Path("assistant_app/gui.py").read_text(encoding="utf-8")
        self.assertIn(
            'self.root.protocol("WM_DELETE_WINDOW", self._minimize_to_tray)',
            source,
        )
        self.assertIn('self.root.bind("<Unmap>"', source)
        self.assertIn('self.root.bind("<Map>"', source)
        self.assertIn("self.root.after(10, self._ensure_tray_icon)", source)

    def test_tray_icon_remains_while_window_is_visible_or_hidden(self) -> None:
        window = AssistantWindow.__new__(AssistantWindow)
        persistent_icon = object()
        window.closing = False
        window._tray_icon = persistent_icon
        window._ensure_tray_icon = Mock()
        window.root = Mock()
        window.status = Mock()

        window._minimize_to_tray()
        window.root.withdraw.assert_called_once_with()
        self.assertIs(window._tray_icon, persistent_icon)

        window._restore_from_tray()
        self.assertIs(window._tray_icon, persistent_icon)
        window.root.deiconify.assert_called_once_with()

    def test_minimized_window_is_revealed_after_layout_finishes(self) -> None:
        window = AssistantWindow.__new__(AssistantWindow)
        window.closing = False
        window._window_restore_pending = False
        window._window_restore_job = None
        window.root = Mock()
        window.root.state.return_value = "iconic"
        event = SimpleNamespace(widget=window.root)

        window._prepare_window_restore(event)

        self.assertTrue(window._window_restore_pending)
        window.root.attributes.assert_called_with("-alpha", 0.0)

        window.root.after.return_value = "restore-job"
        window._complete_window_restore(event)
        window.root.after.assert_called_with(32, window._show_restored_window)

        window._show_restored_window()
        window.root.attributes.assert_called_with("-alpha", 1.0)
        self.assertFalse(window._window_restore_pending)

    def test_skill_sidebar_lists_all_skills_and_optional_adapters_are_disabled(self) -> None:
        production = json.loads(Path("config.json").read_text(encoding="utf-8"))
        expected = {definition.id for definition in SKILL_CATALOG}
        self.assertEqual(set(production["skills"]), expected)
        optional = {"desk-lamp", "starrail-dailies"}
        self.assertTrue(all(not production["skills"][name] for name in optional))
        self.assertTrue(all(
            enabled for name, enabled in production["skills"].items() if name not in optional
        ))
        source = Path("assistant_app/rounded_gui.py").read_text(encoding="utf-8")
        self.assertIn('text="技能"', source)
        self.assertIn("CTkScrollableFrame", source)
        self.assertIn("for definition in SKILL_CATALOG", source)
        self.assertIn('text=f"已学习 {learned}"', source)
        self.assertIn('text=f"技能库 {library}"', source)
        self.assertIn("_render_skill_sidebar", source)
        self.assertIn("_render_favorite_sidebar", source)
        self.assertIn("_toggle_skill_sidebar_visibility", source)
        self.assertIn("_toggle_favorite_sidebar_visibility", source)
        self.assertIn("_toggle_skill_favorite", source)
        self.assertIn("_select_skill_category", source)
        self.assertIn("self._skill_sidebar_category", source)
        self.assertIn('(\"全部\", *SKILL_CATEGORY_ORDER)', source)
        self.assertIn("_schedule_skill_panel_refresh", source)
        self.assertIn("self._skill_cards = {}", source)
        self.assertIn("self._favorite_cards = {}", source)
        self.assertIn("_schedule_skill_scroll_reset", source)
        self.assertIn("canvas.yview_moveto(0.0)", source)
        self.assertIn("_save_ui_setting_async", source)
        self.assertIn("_skill_count_label", source)
        self.assertIn("_open_wake_word_settings", source)
        self.assertIn("_open_xiaomi_home_settings", source)
        self.assertIn("_open_starrail_settings", source)
        self.assertIn("_open_continuous_conversation_settings", source)
        self.assertNotIn("def _open_skill_manager", source)
        self.assertNotIn("def _open_component_page", source)
        self.assertNotIn("self.schedule_button", source)
        self.assertNotIn("self.permissions_button", source)
        self.assertNotIn("self.continuous_button", source)
        self.assertNotIn("xiaomi-token-setup", expected)
        self.assertEqual(
            production["ui"],
            {
                "skill_sidebar_expanded": True,
                "favorite_sidebar_expanded": False,
                "favorite_skills": [],
            },
        )
        self.assertEqual(
            set(SKILL_CATEGORY_ORDER),
            {definition.category for definition in SKILL_CATALOG},
        )
        xiaomi = next(skill for skill in SKILL_CATALOG if skill.id == "desk-lamp")
        self.assertEqual(xiaomi.name, "小米智能家居")
        self.assertIn("setup_xiaomi_token", xiaomi.tools)
        starrail = next(skill for skill in SKILL_CATALOG if skill.id == "starrail-dailies")
        self.assertEqual(
            starrail.project_url,
            "https://github.com/moesnow/March7thAssistant",
        )

    def test_both_skill_sidebars_can_stay_open(self) -> None:
        window = RoundedAssistantWindow.__new__(RoundedAssistantWindow)
        window._skill_sidebar_expanded = True
        window._favorite_sidebar_expanded = False
        window.config = {"ui": {}}
        window._animate_sidebar_visibility = Mock()
        window._save_ui_setting_async = Mock()

        window._toggle_favorite_sidebar_visibility()

        self.assertTrue(window._skill_sidebar_expanded)
        self.assertTrue(window._favorite_sidebar_expanded)
        window._animate_sidebar_visibility.assert_called_once_with("favorite", True)
        window._save_ui_setting_async.assert_called_once_with(
            "favorite_sidebar_expanded", True
        )

    def test_api_key_manager_is_a_primary_window_action(self) -> None:
        source = Path("assistant_app/rounded_gui.py").read_text(encoding="utf-8")
        self.assertIn("self.api_keys_button = ctk.CTkButton", source)
        self.assertIn("command=self._open_api_key_page", source)
        self.assertNotIn('text="管理 API 密钥"', source)

    def test_local_settings_batch_is_written_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("assistant_app.config.PROJECT_ROOT", root):
                save_local_settings(
                    {
                        "skills": {"wake-word": False},
                        "ui": {"favorite_skills": ["wake-word"]},
                    }
                )
            stored = json.loads(
                (root / "config.local.json").read_text(encoding="utf-8")
            )

        self.assertFalse(stored["skills"]["wake-word"])
        self.assertEqual(stored["ui"]["favorite_skills"], ["wake-word"])

    def test_gui_workers_skip_duplicate_tts_initialization(self) -> None:
        gui_source = Path("assistant_app/gui.py").read_text(encoding="utf-8")
        cli_source = Path("assistant_app/cli.py").read_text(encoding="utf-8")
        self.assertEqual(gui_source.count("include_tts=False"), 2)
        self.assertIn(
            'SpeechSynthesizer(config["tts"]) if include_tts else None',
            cli_source,
        )

    def test_gui_prepares_and_reuses_background_runtime(self) -> None:
        source = Path("assistant_app/gui.py").read_text(encoding="utf-8")
        self.assertIn('name="maomao-agent"', source)
        self.assertIn("self._ensure_agent_runtime()", source)
        self.assertIn("self._agent_task_queue.put", source)
        self.assertIn("agent.tools.cleanup_temporary_screenshots()", source)

    def test_gui_reuses_one_database_for_ui_queries(self) -> None:
        source = Path("assistant_app/gui.py").read_text(encoding="utf-8")
        self.assertIn("self._ui_database = Database(app_paths().database)", source)
        self.assertIn("self._ui_database.due_scheduled_tasks(limit=1)", source)
        self.assertIn("self._ui_database.usage_summary", source)
        self.assertIn("ui_database.close()", source)

    def test_xiaomi_settings_lists_device_without_exposing_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "xiaomi_token.txt"
            token_file.write_text(
                "did=1234567890\n"
                "name=米家台灯2\n"
                "model=yeelink.light.lamp4\n"
                "ip=192.0.2.10\n"
                f"token={'a' * 32}\n",
                encoding="utf-8",
            )
            devices = RoundedAssistantWindow._read_xiaomi_devices(token_file)

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["name"], "米家台灯2")
        self.assertEqual(devices[0]["ip"], "192.0.2.10")
        self.assertNotIn("token", devices[0])

    def test_schedule_manager_has_daily_and_silent_controls(self) -> None:
        source = Path("assistant_app/rounded_gui.py").read_text(encoding="utf-8")
        self.assertIn('text="定时任务"', source)
        self.assertIn('values=["仅一次", "每天"]', source)
        self.assertIn('text="静默执行（不播报、不弹出窗口）"', source)
        self.assertIn("_refresh_schedule_manager", source)

    def test_silent_schedule_completion_never_enters_chat_or_tts(self) -> None:
        window = AssistantWindow.__new__(AssistantWindow)
        window._scheduled_task_running = True
        window._append_chat = Mock()
        window._refresh_schedule_manager = Mock()

        window._finish_scheduled_task(3, "关灯", True, True, "操作已完成")

        self.assertFalse(window._scheduled_task_running)
        window._append_chat.assert_not_called()
        window._refresh_schedule_manager.assert_called_once_with()

        window._refresh_schedule_manager.reset_mock()
        window._finish_scheduled_task(4, "关灯", False, True, "操作已完成")
        window._append_chat.assert_called_once()
        self.assertNotIn("speak", str(window._append_chat.mock_calls))

    def test_multiple_wake_words_are_parsed_and_reset_old_voice_templates(self) -> None:
        self.assertEqual(
            AssistantWindow._parse_wake_words(" 猫猫，喵喵助手；猫 猫\n小七 "),
            ["猫猫", "喵喵助手", "小七"],
        )
        with self.assertRaises(ValueError):
            AssistantWindow._parse_wake_words("一,二,三,四,五,六")

        with tempfile.TemporaryDirectory() as temporary:
            template_path = Path(temporary) / "wake-templates.npz"
            template_path.write_bytes(b"old templates")
            listener = WakeWordListener(
                {
                    "enabled": False,
                    "keyword": "猫猫",
                    "aliases": ["猫猫"],
                    "template_path": str(template_path),
                },
                lambda: None,
            )
            listener.verifier.templates = [np.zeros((3, 26), dtype=np.float32)] * 3
            self.assertTrue(listener.enrolled)

            listener.update_keywords(["猫猫", "喵喵助手"], clear_enrollment=True)

            self.assertEqual(listener.keyword, "猫猫")
            self.assertEqual(listener.aliases, ["猫猫", "喵喵助手"])
            self.assertFalse(listener.enrolled)
            self.assertFalse(template_path.exists())

    def test_skill_total_counts_enabled_items(self) -> None:
        window = AssistantWindow.__new__(AssistantWindow)
        window.config = {
            "skills": {
                definition.id: definition.id != "wake-word"
                for definition in SKILL_CATALOG
            }
        }
        window._skill_count_label = Mock()

        window._refresh_skill_count()

        self.assertEqual(
            window._skill_count_label.configure.call_args.kwargs["text"],
            f"已开启 {len(SKILL_CATALOG) - 1} / 共 {len(SKILL_CATALOG)} 个",
        )

    def test_tray_menu_can_restart_without_rebuilding_the_launcher(self) -> None:
        gui_source = Path("assistant_app/gui.py").read_text(encoding="utf-8")
        helper_source = Path("assistant_app/restart_helper.py").read_text(encoding="utf-8")
        self.assertIn('"重启猫猫"', gui_source)
        self.assertIn('"assistant_app.restart_helper"', gui_source)
        self.assertNotIn('project_root / "MaoMao.exe"', gui_source)
        self.assertIn("WaitForSingleObject", helper_source)
        self.assertIn('[sys.executable, "-m", "assistant_app.qt_quick.app"]', helper_source)

    def test_preload_button_keeps_loading_label_until_completion(self) -> None:
        window = AssistantWindow.__new__(AssistantWindow)
        window.preload_button = Mock()
        window.preload_enabled = Mock()
        window.preload_enabled.get.return_value = True
        window.config = {"skills": {"wake-word": True}}
        window._preload_loading = True
        window._refresh_preload_button()
        self.assertEqual(
            window.preload_button.configure.call_args.kwargs["text"],
            "加载中",
        )

        window._preload_loading = False
        window._refresh_preload_button()
        self.assertEqual(window.preload_button.configure.call_args.kwargs["text"], "开")

    def test_startup_waits_for_preload_before_starting_wake_listener(self) -> None:
        window = AssistantWindow.__new__(AssistantWindow)
        window.preload_enabled = Mock()
        window.preload_enabled.get.return_value = True
        window.config = {"skills": {"wake-word": True}}
        window._set_preload_loading = Mock()
        window._start_tts_warmup = Mock()
        window.wake_listener = Mock()
        window.root = Mock()

        window._start_background_services(warmup=True)

        window._set_preload_loading.assert_called_once_with(True)
        window.root.after.assert_called_once_with(100, window._start_tts_warmup)
        window.wake_listener.start.assert_not_called()

        window._finish_tts_warmup_status = AssistantWindow._finish_tts_warmup_status.__get__(
            window, AssistantWindow
        )
        window.status = Mock()
        window.asr_mode = Mock()
        window._wake_status_value = "正在监听猫猫"
        window.wake_capture_enabled = Mock()
        window.wake_capture_enabled.get.return_value = True
        window._finish_tts_warmup_status()
        window.wake_listener.start.assert_called_once_with()
        window.wake_listener.resume.assert_called_once_with()

    def test_wake_capture_button_updates_listener_and_persists_setting(self) -> None:
        class BooleanValue:
            def __init__(self, value: bool) -> None:
                self.value = value

            def get(self) -> bool:
                return self.value

            def set(self, value: bool) -> None:
                self.value = value

        window = AssistantWindow.__new__(AssistantWindow)
        window.wake_capture_enabled = BooleanValue(True)
        window.wake_listener = SimpleNamespace(
            keyword="猫猫",
            set_capture_enabled=Mock(),
        )
        window.config = {"wake_word": {}, "skills": {}}
        window.status = Mock()

        with patch("assistant_app.gui.save_local_settings") as save:
            window._toggle_wake_capture()

        self.assertFalse(window.wake_capture_enabled.get())
        window.wake_listener.set_capture_enabled.assert_called_once_with(False)
        self.assertFalse(window.config["wake_word"]["capture_enabled"])
        save.assert_called_once_with(
            {
                "wake_word": {"capture_enabled": False},
                "skills": {"wake-word": False},
            }
        )
        window.status.set.assert_called_once_with("唤醒监听已关闭 · 手动录音仍可使用")

    def test_rounded_wake_status_refreshes_settings_controls(self) -> None:
        window = RoundedAssistantWindow.__new__(RoundedAssistantWindow)
        window.wake_capture_enabled = SimpleNamespace(get=lambda: False)
        window._sync_wake_settings_controls = Mock()

        window._refresh_wake_capture_button()

        window._sync_wake_settings_controls.assert_called_once_with()

    def test_goodbye_cache_is_unique_per_engine_and_voice(self) -> None:
        tts = SpeechSynthesizer({"enabled": False, "backend": "mimo-api", "mimo_voice": "冰糖"})
        first = tts.cached_voice_clip_path(Path("cache"), "goodbye")
        tts.set_speaker("Mia")
        second = tts.cached_voice_clip_path(Path("cache"), "goodbye")
        tts.set_engine("f5tts")
        third = tts.cached_voice_clip_path(Path("cache"), "goodbye")
        self.assertEqual(first, Path("cache/mimo-api/冰糖/goodbye.wav"))
        self.assertEqual(second, Path("cache/mimo-api/Mia/goodbye.wav"))
        self.assertEqual(third, Path("cache/f5tts/default/goodbye.wav"))

    def test_mimo_tts_exposes_all_official_preset_voices(self) -> None:
        tts = SpeechSynthesizer({"enabled": False, "backend": "mimo-api"})
        self.assertEqual(
            tts.available_voices(),
            ["冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"],
        )

    def test_removed_engine_is_rejected(self) -> None:
        for backend in ("gpt-sovits", "indextts2"):
            with self.assertRaises(ValueError):
                SpeechSynthesizer({"enabled": False, "backend": backend})

    def test_wake_word_matches_spacing_and_alias(self) -> None:
        listener = WakeWordListener(
            {"enabled": False, "keyword": "猫猫", "aliases": ["猫猫"]},
            on_wake=lambda: None,
        )
        self.assertTrue(listener._matches("猫 猫"))
        self.assertFalse(listener._matches("请叫猫猫过来"))
        self.assertFalse(listener._matches("喵喵"))
        self.assertFalse(listener._matches("你好"))
        self.assertTrue(listener._keyword_hint("猫"))
        self.assertFalse(listener._keyword_hint("喵"))
        self.assertEqual(listener._grammar_phrases(), ["猫猫", "猫 猫"])

    def test_lightweight_voice_confirmation_understands_yes_and_no(self) -> None:
        for phrase in ("是", "是，也可以", "可以", "好的", "用吧", "yes", "ok"):
            self.assertTrue(WakeWordListener._confirmation_decision(phrase))
        for phrase in ("不用", "不要", "不可以", "否", "取消", "no"):
            self.assertFalse(WakeWordListener._confirmation_decision(phrase))
        self.assertIsNone(WakeWordListener._confirmation_decision("我再想想"))

    def test_one_time_local_asr_is_closed_immediately_after_use(self) -> None:
        transcriber = SpeechTranscriber(
            {"backend": "mimo-api", "mimo_base_url": "https://api.xiaomimimo.com/v1"}
        )
        local = Mock()
        local.transcribe.return_value = "本地转写成功"
        with patch("assistant_app.audio.QwenASRTranscriber", return_value=local):
            result = transcriber.transcribe_once_with_local(Path("recording.wav"))
        self.assertEqual(result, "本地转写成功")
        local.close.assert_called_once_with()

    def test_wake_word_requires_confident_final_result(self) -> None:
        listener = WakeWordListener(
            {"enabled": False, "keyword": "猫猫", "aliases": ["猫猫"], "min_confidence": 0.86},
            on_wake=lambda: None,
        )
        self.assertTrue(listener._result_matches({"text": "猫猫", "result": [{"conf": 0.94}]}))
        self.assertFalse(listener._result_matches({"text": "猫猫", "result": [{"conf": 0.61}]}))
        self.assertFalse(listener._result_matches({"text": "猫猫"}))

    def test_voice_similarity_cannot_wake_without_keyword(self) -> None:
        listener = WakeWordListener(
            {"enabled": False, "keyword": "猫猫", "aliases": ["猫猫"], "require_voice_match": True},
            on_wake=lambda: None,
        )
        listener.verifier.threshold = 0.60
        self.assertFalse(listener._voice_and_keyword_match(0.20, False, False))
        self.assertTrue(listener._voice_and_keyword_match(0.61, True, True))
        self.assertFalse(listener._voice_and_keyword_match(0.70, True, True))

    def test_one_partial_keyword_can_wake_with_matching_voice(self) -> None:
        listener = WakeWordListener(
            {
                "enabled": False,
                "keyword": "猫猫",
                "aliases": ["猫猫"],
                "require_voice_match": True,
                "partial_hits_required": 1,
                "partial_voice_multiplier": 1.05,
            },
            on_wake=lambda: None,
        )
        listener.verifier.threshold = 0.60
        self.assertTrue(listener._partial_keyword_match("猫猫", 1, 0.62))
        self.assertFalse(listener._partial_keyword_match("猫猫", 1, 0.65))
        self.assertFalse(listener._partial_keyword_match("你好", 1, 0.20))

    def test_single_cat_fragment_waits_for_audio_then_uses_stricter_voice_match(self) -> None:
        listener = WakeWordListener(
            {
                "enabled": False,
                "keyword": "猫猫",
                "aliases": ["猫猫"],
                "require_voice_match": True,
                "fragment_hits_required": 2,
                "fragment_voice_multiplier": 1.0,
            },
            on_wake=lambda: None,
        )
        listener.verifier.threshold = 0.60
        self.assertFalse(listener._partial_keyword_match("猫", 1, 0.50))
        self.assertTrue(listener._partial_keyword_match("猫", 2, 0.59))
        self.assertFalse(listener._partial_keyword_match("猫", 2, 0.61))

    def test_partial_keyword_without_voice_match_still_requires_exact_keyword(self) -> None:
        listener = WakeWordListener(
            {
                "enabled": False,
                "keyword": "猫猫",
                "aliases": ["猫猫"],
                "require_voice_match": False,
                "partial_hits_required": 1,
            },
            on_wake=lambda: None,
        )
        self.assertTrue(listener._partial_keyword_match("猫 猫", 1, float("inf")))
        self.assertFalse(listener._partial_keyword_match("喵喵", 1, 0.0))

    def test_user_disabled_wake_capture_survives_internal_pause_resume(self) -> None:
        listener = WakeWordListener(
            {"enabled": False, "capture_enabled": True},
            on_wake=lambda: None,
        )
        self.assertTrue(listener.capture_enabled)
        listener.set_capture_enabled(False)
        listener.pause()
        listener.resume()
        self.assertFalse(listener.capture_enabled)
        listener.set_capture_enabled(True)
        self.assertTrue(listener.capture_enabled)

    def test_five_wake_samples_ignore_one_noisy_outlier(self) -> None:
        templates = [np.full((2, 2), index, dtype=np.float32) for index in range(5)]

        def distance(first, second):
            return 0.90 if 4 in {int(first[0, 0]), int(second[0, 0])} else 0.40

        with patch.object(WakeVoiceVerifier, "_distance", side_effect=distance):
            threshold = WakeVoiceVerifier._calibrated_threshold(templates)
        self.assertGreaterEqual(threshold, 0.50)
        self.assertLess(threshold, 0.65)


class ProviderConfigurationTests(unittest.TestCase):
    def test_key_file_supports_kimi_and_mimo_without_exposing_either(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "api_key.txt"
            path.write_text(
                "kimi_key=test-kimi-placeholder\nmimo_key=test-mimo-placeholder\n",
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"MOONSHOT_API_KEY": "", "MIMO_API_KEY": ""},
            ):
                self.assertEqual(load_kimi_key(path), "test-kimi-placeholder")
                self.assertEqual(load_mimo_key(path), "test-mimo-placeholder")

    def test_automatic_model_mode_prefers_kimi_and_falls_back_to_mimo(self) -> None:
        kimi = Mock()
        mimo = Mock()
        kimi.budget = SimpleNamespace()
        kimi.chat.side_effect = RuntimeError("temporary Kimi failure")
        expected = KimiResponse({}, "mimo-v2.5", 0, 0, 0, 0.0)
        mimo.chat.return_value = expected
        client = HybridModelClient(kimi, mimo, {"routing": {"model_mode": "auto"}})
        result = client.chat([], [], "kimi-k2.6", "disabled", "task")
        self.assertIs(result, expected)
        mimo.chat.assert_called_once_with([], [], "mimo-v2.5", "disabled", "task")

    def test_mimo_pricing_is_recorded_in_cny(self) -> None:
        self.assertAlmostEqual(
            BudgetManager.estimate_cost("mimo-v2.5", 1_000_000, 0, 100_000),
            1.2,
        )


class StorageAndBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "test.db")

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def test_chinese_memory_retrieval(self) -> None:
        self.database.remember("我常用的浏览器是 Edge")
        found = self.database.search_memories("打开浏览器")
        self.assertEqual(found[0]["content"], "我常用的浏览器是 Edge")

    def test_stable_memory_key_updates_instead_of_duplicating(self) -> None:
        first = self.database.remember(
            "用户所在地：悉尼",
            "位置,所在地,城市,住址,天气",
            "user.location",
        )
        second = self.database.remember(
            "用户所在地：墨尔本",
            "位置,所在地,城市,住址,天气",
            "user.location",
        )
        self.assertEqual(first, second)
        memories = self.database.list_memories()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["content"], "用户所在地：墨尔本")

    def test_profile_memory_is_available_without_keyword_overlap(self) -> None:
        self.database.remember(
            "用户所在地：悉尼",
            "位置,所在地,城市,住址,天气",
            "user.location",
        )
        agent = PersonalAgent(
            {
                "memory": {
                    "recent_messages": 2,
                    "max_retrieved_memories": 2,
                    "profile_memories": 20,
                }
            },
            self.database,
            client=None,  # type: ignore[arg-type]
            tools=None,  # type: ignore[arg-type]
            session_id="profile-memory-test",
        )
        system = agent._messages("随便聊点别的", "text")[0]["content"]
        self.assertIn("用户所在地：悉尼", system)

    def test_weather_search_uses_english_query_and_saved_location(self) -> None:
        self.database.remember(
            "用户所在地：悉尼",
            "位置,所在地,城市,住址,天气",
            "user.location",
        )
        root = Path(self.temp.name)
        screenshots = root / "screenshots"
        screenshots.mkdir(exist_ok=True)
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        tools = ToolRegistry(paths, self.database, "weather-test", lambda *_: True)
        self.assertEqual(tools._weather_search_query("今天天气如何"), "Sydney weather today")
        self.assertEqual(tools._weather_search_query("明天会下雨吗"), "Sydney weather tomorrow")
        self.assertEqual(
            tools._weather_search_query("未来一周天气"),
            "Sydney 7 day weather forecast",
        )
        self.assertEqual(tools._weather_search_query("搜索猫猫图片"), "搜索猫猫图片")

    def test_clear_current_conversation_preserves_other_data(self) -> None:
        self.database.add_message("current", "user", "清除我")
        self.database.add_message("other", "user", "保留我")
        self.database.remember("长期记忆也保留")
        self.assertEqual(self.database.clear_messages("current"), 1)
        self.assertEqual(self.database.recent_messages("current", 10), [])
        self.assertEqual(self.database.recent_messages("other", 10)[0]["content"], "保留我")
        self.assertEqual(self.database.list_memories(1)[0]["content"], "长期记忆也保留")

    def test_agent_tool_can_save_local_memory(self) -> None:
        root = Path(self.temp.name)
        screenshots = root / "screenshots"
        screenshots.mkdir(exist_ok=True)
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        tools = ToolRegistry(paths, self.database, "test", lambda *_: True)
        result = tools.execute(
            "save_memory",
            {"content": "我住在悉尼", "tags": "位置"},
            "memory-test",
        )
        self.assertTrue(result.success)
        self.assertEqual(self.database.list_memories(1)[0]["content"], "我住在悉尼")

    def test_web_search_and_memory_tools_are_exposed_but_chatgpt_is_direct_only(self) -> None:
        root = Path(self.temp.name)
        screenshots = root / "screenshots"
        screenshots.mkdir(exist_ok=True)
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        tools = ToolRegistry(paths, self.database, "test", lambda *_: True)
        names = {item["function"]["name"] for item in tools.schemas}
        self.assertTrue(
            {
                "search_web",
                "open_search_result",
                "save_memory",
                "add_app_to_allowlist",
                "list_allowed_apps",
                "control_system_volume",
                "create_scheduled_task",
                "list_scheduled_tasks",
                "cancel_scheduled_task",
            } <= names
        )
        self.assertNotIn("control_desk_lamp", names)
        self.assertNotIn("run_starrail_dailies", names)
        self.assertNotIn("ask_chatgpt", names)
        self.assertEqual(
            next(skill for skill in SKILL_CATALOG if skill.id == "browser-navigation").tools,
            ("open_url", "ask_chatgpt"),
        )

    def test_once_and_daily_scheduled_tasks_are_claimed_only_once(self) -> None:
        first_run = (datetime.now() + timedelta(minutes=2)).replace(microsecond=0)
        once_id = self.database.create_scheduled_task(
            "关灯", first_run, repeat_rule="once", silent=True
        )
        daily_id = self.database.create_scheduled_task(
            "帮我过星铁日常", first_run, repeat_rule="daily", silent=True
        )
        due_at = first_run + timedelta(seconds=1)
        self.assertEqual(
            {task["id"] for task in self.database.due_scheduled_tasks(due_at)},
            {once_id, daily_id},
        )

        once = self.database.claim_scheduled_task(once_id, due_at)
        daily = self.database.claim_scheduled_task(daily_id, due_at)
        self.assertIsNotNone(once)
        self.assertIsNotNone(daily)
        self.assertEqual(once["enabled"], 0)
        self.assertEqual(daily["enabled"], 1)
        self.assertEqual(
            datetime.fromisoformat(daily["next_run_at"]),
            first_run + timedelta(days=1),
        )
        self.assertIsNone(self.database.claim_scheduled_task(once_id, due_at))
        self.assertEqual(self.database.due_scheduled_tasks(due_at), [])

        self.database.complete_scheduled_task(daily_id, True, "操作已完成")
        stored = {task["id"]: task for task in self.database.list_scheduled_tasks()}
        self.assertEqual(stored[daily_id]["last_status"], "succeeded")
        self.assertEqual(stored[daily_id]["last_result"], "操作已完成")

    def test_create_schedule_tool_confirms_and_persists_silent_mode(self) -> None:
        root = Path(self.temp.name)
        screenshots = root / "screenshots"
        screenshots.mkdir(exist_ok=True)
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        confirmed: list[tuple[str, dict]] = []
        tools = ToolRegistry(
            paths,
            self.database,
            "schedule-test",
            lambda name, arguments: confirmed.append((name, arguments)) or True,
        )
        run_at = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        result = tools.execute(
            "create_scheduled_task",
            {
                "command": "帮我过星铁日常",
                "run_at": run_at,
                "repeat": "daily",
                "silent": True,
            },
            "create-schedule-task",
        )
        self.assertTrue(result.success)
        self.assertEqual(confirmed[0][0], "create_scheduled_task")
        saved = self.database.list_scheduled_tasks()[0]
        self.assertEqual(saved["command"], "帮我过星铁日常")
        self.assertEqual(saved["repeat_rule"], "daily")
        self.assertEqual(saved["silent"], 1)

    def test_system_volume_uses_windows_audio_endpoint(self) -> None:
        root = Path(self.temp.name)
        screenshots = root / "screenshots"
        screenshots.mkdir(exist_ok=True)
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        tools = ToolRegistry(paths, self.database, "volume-test", lambda *_: True)
        endpoint = Mock()
        endpoint.GetMasterVolumeLevelScalar.side_effect = [0.40, 0.25]
        endpoint.GetMute.return_value = 0
        with patch.object(tools, "_system_volume_endpoint", return_value=endpoint):
            result = tools.execute(
                "control_system_volume",
                {"action": "set", "level": 25},
                "volume-tool-task",
            )
        self.assertTrue(result.success)
        endpoint.SetMasterVolumeLevelScalar.assert_called_once_with(0.25, None)
        self.assertIn("25%", result.content)

    def test_local_volume_skill_does_not_steal_scheduled_requests(self) -> None:
        immediate = match_local_skill("把系统音量降低一点", {"skills": {"system-volume": True}})
        scheduled = match_local_skill(
            "每天晚上十一点把系统音量调到20%",
            {"skills": {"system-volume": True}},
        )
        self.assertIsNotNone(immediate)
        self.assertEqual(immediate.tool_name, "control_system_volume")
        self.assertEqual(immediate.arguments, {"action": "down", "level": 10})
        self.assertIsNone(scheduled)

    def test_daily_starrail_schedule_is_parsed_locally_without_bad_asr_aliases(self) -> None:
        invocation = match_local_skill(
            "呃，设置一个定时任务，每天早上八点，静默帮我刷星铁。",
            {"skills": {"scheduled-tasks": True}},
        )
        self.assertIsNotNone(invocation)
        self.assertEqual(invocation.tool_name, "create_scheduled_task")
        self.assertEqual(invocation.arguments["command"], "帮我过星铁日常")
        self.assertEqual(invocation.arguments["run_at"][11:16], "08:00")
        self.assertEqual(invocation.arguments["repeat"], "daily")
        self.assertTrue(invocation.arguments["silent"])
        self.assertIsNone(
            match_local_skill(
                "每天早上八点静默帮我刷新题",
                {"skills": {"scheduled-tasks": True}},
            )
        )

    def test_local_desk_lamp_control_reads_secret_without_exposing_it(self) -> None:
        root = Path(self.temp.name)
        screenshots = root / "screenshots"
        screenshots.mkdir(exist_ok=True)
        secret = "a" * 32
        (root / "xiaomi_token.txt").write_text(
            f"did=1234567890\nmodel=xiaomi.light.lamp31\nip=192.0.2.10\ntoken={secret}\n",
            encoding="utf-8",
        )
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        tools = ToolRegistry(
            paths,
            self.database,
            "lamp-control-test",
            lambda *_: True,
            smart_home_config={
                "desk_lamp": {
                    "enabled": True,
                    "ip": "192.0.2.10",
                    "model": "xiaomi.light.lamp31",
                    "token_file": "xiaomi_token.txt",
                }
            },
            skills_config={"desk-lamp": True},
        )
        fake_device = SimpleNamespace(
            raw_command=Mock(
                side_effect=[
                    [
                        {"did": "light:on", "code": 0},
                        {"did": "light:brightness", "code": 0},
                        {"did": "light:mode", "code": 0},
                    ],
                    [
                        {"did": "light:on", "code": 0, "value": True},
                        {"did": "light:brightness", "code": 0, "value": 50},
                        {"did": "light:mode", "code": 0, "value": 1},
                    ],
                ]
            )
        )
        with patch("miio.Device", return_value=fake_device) as device_class:
            result = tools.execute(
                "control_desk_lamp",
                {"brightness": 50, "mode": "reading"},
                "lamp-control-task",
            )
        self.assertTrue(result.success)
        self.assertNotIn(secret, result.content)
        device_class.assert_called_once_with(
            "192.0.2.10", secret, timeout=4, model="xiaomi.light.lamp31"
        )
        expected = [
            {"did": "light:on", "siid": 2, "piid": 1, "value": True},
            {"did": "light:brightness", "siid": 2, "piid": 2, "value": 50},
            {"did": "light:mode", "siid": 2, "piid": 15, "value": 1},
        ]
        self.assertEqual(fake_device.raw_command.call_count, 2)
        fake_device.raw_command.assert_any_call("set_properties", expected)
        fake_device.raw_command.assert_any_call(
            "get_properties",
            [{"did": item["did"], "siid": item["siid"], "piid": item["piid"]} for item in expected],
        )

    def test_local_desk_lamp_status_is_parsed(self) -> None:
        root = Path(self.temp.name)
        screenshots = root / "screenshots"
        screenshots.mkdir(exist_ok=True)
        (root / "xiaomi_token.txt").write_text(
            "ip=192.0.2.10\ntoken=" + "b" * 32 + "\n",
            encoding="utf-8",
        )
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        tools = ToolRegistry(
            paths,
            self.database,
            "lamp-status-test",
            lambda *_: True,
            smart_home_config={
                "desk_lamp": {"enabled": True, "token_file": "xiaomi_token.txt"}
            },
            skills_config={"desk-lamp": True},
        )
        response = [
            {"did": "light:on", "code": 0, "value": True},
            {"did": "light:brightness", "code": 0, "value": 46},
            {"did": "light:color-temperature", "code": 0, "value": 3006},
            {"did": "light:mode", "code": 0, "value": 0},
        ]
        fake_device = SimpleNamespace(raw_command=lambda *_args: response)
        with patch("miio.Device", return_value=fake_device):
            result = tools.execute("get_desk_lamp_status", {}, "lamp-status-task")
        self.assertTrue(result.success)
        status = json.loads(result.content)
        self.assertEqual(status["power"], "on")
        self.assertEqual(status["brightness"], 46)
        self.assertEqual(status["color_temperature"], 3006)
        self.assertEqual(status["mode"], "auto")

    def test_cancel_event_stops_agent_and_tools(self) -> None:
        cancelled = threading.Event()
        cancelled.set()
        root = Path(self.temp.name)
        screenshots = root / "screenshots"
        screenshots.mkdir(exist_ok=True)
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        tools = ToolRegistry(
            paths,
            self.database,
            "cancel-test",
            lambda *_: True,
            cancel_event=cancelled,
        )
        self.assertFalse(tools.execute("get_current_time", {}, "cancel-test").success)
        agent = PersonalAgent(
            {"memory": {"recent_messages": 2, "max_retrieved_memories": 2}},
            self.database,
            client=None,  # type: ignore[arg-type]
            tools=tools,
            session_id="cancel-test",
            cancel_event=cancelled,
        )
        with self.assertRaises(OperationCancelled):
            agent._ensure_not_cancelled()

    def test_voice_added_app_is_saved_and_existing_window_is_focused(self) -> None:
        root = Path(self.temp.name)
        screenshots = root / "screenshots"
        screenshots.mkdir(exist_ok=True)
        executable = root / "WeChat.exe"
        executable.touch()
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        confirmations: list[tuple[str, dict]] = []
        tools = ToolRegistry(
            paths,
            self.database,
            "test",
            lambda name, arguments: confirmations.append((name, arguments)) or True,
            application_config={"allowlist": {}},
        )
        with patch("assistant_app.tools.save_local_setting") as save_setting:
            added = tools.execute(
                "add_app_to_allowlist",
                {"name": "微信", "path": str(executable)},
                "app-add-test",
            )
        self.assertTrue(added.success)
        self.assertEqual(confirmations[0][0], "add_app_to_allowlist")
        save_setting.assert_called_once()
        with patch.object(tools, "_focus_running_app", return_value="微信"):
            opened = tools.execute("open_app", {"app": "微信"}, "app-open-test")
        self.assertTrue(opened.success)
        self.assertIn("切换到前台", opened.content)

    def test_browser_navigation_reuses_current_tab_with_keyboard(self) -> None:
        root = Path(self.temp.name)
        screenshots = root / "screenshots"
        screenshots.mkdir(exist_ok=True)
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        tools = ToolRegistry(paths, self.database, "browser-test", lambda *_: True)
        fake_window = SimpleNamespace(handle=123, set_focus=lambda: None)
        with (
            patch.object(tools, "_visible_browser_window", return_value=fake_window),
            patch.object(tools, "_restore_foreground") as restore,
            patch.object(tools, "_open_in_default_browser") as system_open,
            patch("pyautogui.hotkey") as hotkey,
            patch("pyautogui.write") as write,
            patch("pyautogui.press") as press,
        ):
            result = tools._navigate_browser_humanlike("https://example.com/天气")
        self.assertIn("当前浏览器标签页", result)
        restore.assert_called_once_with(123)
        system_open.assert_not_called()
        hotkey.assert_called_once_with("ctrl", "l")
        self.assertIn("%E5%A4%A9%E6%B0%94", write.call_args.args[0])
        press.assert_called_once_with("enter")

    def test_browser_cold_start_replaces_initial_tab(self) -> None:
        root = Path(self.temp.name)
        screenshots = root / "screenshots"
        screenshots.mkdir(exist_ok=True)
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        tools = ToolRegistry(paths, self.database, "browser-cold-test", lambda *_: True)
        fake_window = SimpleNamespace(handle=456, set_focus=lambda: None)
        with (
            patch.object(tools, "_visible_browser_window", side_effect=[None, fake_window]),
            patch.object(tools, "_default_browser_executable", return_value=r"C:\Edge\msedge.exe"),
            patch.object(tools, "_restore_foreground") as restore,
            patch.object(tools, "_open_in_default_browser") as system_open,
            patch("assistant_app.tools.subprocess.Popen") as popen,
            patch("assistant_app.tools.time.sleep"),
            patch("pyautogui.hotkey") as hotkey,
            patch("pyautogui.write") as write,
            patch("pyautogui.press") as press,
        ):
            result = tools._navigate_browser_humanlike("https://example.com/")
        self.assertIn("当前浏览器标签页", result)
        popen.assert_called_once_with([r"C:\Edge\msedge.exe"])
        system_open.assert_not_called()
        restore.assert_called_once_with(456)
        hotkey.assert_called_once_with("ctrl", "l")
        write.assert_called_once()
        press.assert_called_once_with("enter")

    def test_research_uses_temporary_tab_and_closes_only_that_tab(self) -> None:
        root = Path(self.temp.name)
        screenshots = root / "screenshots"
        screenshots.mkdir(exist_ok=True)
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        tools = ToolRegistry(paths, self.database, "research-tab-test", lambda *_: True)
        fake_window = SimpleNamespace(handle=789, set_focus=lambda: None)
        with (
            patch.object(tools, "_visible_browser_window", return_value=fake_window),
            patch.object(tools, "_restore_foreground") as restore,
            patch("pyautogui.hotkey") as hotkey,
            patch("pyautogui.write") as write,
            patch("pyautogui.press") as press,
        ):
            result = tools._navigate_research_browser("https://example.com/search")
            tools.close_research_browser()
        self.assertIn("临时浏览器页面", result)
        self.assertEqual(
            hotkey.call_args_list,
            [
                unittest.mock.call("ctrl", "t"),
                unittest.mock.call("ctrl", "l"),
                unittest.mock.call("ctrl", "w"),
            ],
        )
        self.assertEqual(restore.call_count, 3)
        write.assert_called_once()
        press.assert_called_once_with("enter")
        self.assertFalse(tools._research_browser_active)

    def test_starrail_tool_clicks_complete_run_by_accessible_text(self) -> None:
        root = Path(self.temp.name)
        screenshots = root / "screenshots"
        screenshots.mkdir(exist_ok=True)
        executable = root / "March7th Launcher.exe"
        executable.touch()
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        tools = ToolRegistry(
            paths,
            self.database,
            "starrail-tool-test",
            lambda *_: True,
            automation_config={
                "starrail_dailies": {
                    "enabled": True,
                    "executable": str(executable),
                    "button_text": "完整运行",
                }
            },
            skills_config={"starrail-dailies": True},
        )
        control = SimpleNamespace(
            window_text=lambda: "完整运行",
            is_visible=lambda: True,
            is_enabled=lambda: True,
        )
        window = SimpleNamespace(
            handle=2468,
            set_focus=lambda: None,
            descendants=lambda: [control],
        )
        with (
            patch.object(tools, "_march7th_window", return_value=window),
            patch.object(tools, "_restore_foreground"),
            patch.object(tools, "_click_march7th_task_card") as click_card,
        ):
            result = tools.execute("run_starrail_dailies", {}, "starrail-tool-task")
        self.assertTrue(result.success)
        self.assertIn("完整运行", result.content)
        click_card.assert_called_once_with(window, control)

    def test_admin_starrail_tool_delegates_to_elevated_helper(self) -> None:
        root = Path(self.temp.name)
        screenshots = root / "screenshots"
        screenshots.mkdir(exist_ok=True)
        executable = root / "March7th Launcher.exe"
        executable.touch()
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        tools = ToolRegistry(
            paths,
            self.database,
            "starrail-admin-test",
            lambda *_: True,
            automation_config={
                "starrail_dailies": {
                    "enabled": True,
                    "executable": str(executable),
                    "button_text": "完整运行",
                    "requires_admin": True,
                }
            },
            skills_config={"starrail-dailies": True},
        )
        expected = ToolResult(True, "管理员辅助进程已点击完整运行。")
        with (
            patch.object(tools, "_is_process_elevated", return_value=False),
            patch.object(
                tools,
                "_run_elevated_march7th_helper",
                return_value=expected,
            ) as elevated,
            patch.object(tools, "_march7th_window") as find_window,
        ):
            result = tools.execute("run_starrail_dailies", {}, "starrail-admin-task")
        self.assertEqual(result, expected)
        elevated.assert_called_once_with(executable.resolve(), "完整运行")
        find_window.assert_not_called()

    def test_running_tray_app_is_activated_without_relaunch(self) -> None:
        root = Path(self.temp.name)
        screenshots = root / "screenshots"
        screenshots.mkdir(exist_ok=True)
        shortcut = root / "WeChat.lnk"
        shortcut.touch()
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        tools = ToolRegistry(
            paths,
            self.database,
            "tray-open-test",
            lambda *_: True,
            application_config={
                "allowlist": {
                    "wechat": {
                        "path": str(shortcut),
                        "process_name": "Weixin.exe",
                        "tray_labels": ["微信", "WeChat"],
                        "aliases": ["微信", "WeChat"],
                    }
                }
            },
        )
        with (
            patch.object(tools, "_focus_running_app", return_value=""),
            patch.object(tools, "_app_process_is_running", return_value=True),
            patch.object(tools, "_activate_tray_app", return_value="微信") as activate,
            patch("assistant_app.tools.os.startfile") as startfile,
        ):
            opened = tools.execute("open_app", {"app": "微信"}, "tray-open-test")
        self.assertTrue(opened.success)
        self.assertIn("系统托盘", opened.content)
        activate.assert_called_once()
        startfile.assert_not_called()

    def test_running_tray_app_failure_does_not_open_duplicate(self) -> None:
        root = Path(self.temp.name)
        screenshots = root / "screenshots"
        screenshots.mkdir(exist_ok=True)
        shortcut = root / "WeChat.lnk"
        shortcut.touch()
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        tools = ToolRegistry(
            paths,
            self.database,
            "tray-failure-test",
            lambda *_: True,
            application_config={
                "allowlist": {
                    "wechat": {
                        "path": str(shortcut),
                        "process_name": "Weixin.exe",
                        "tray_labels": ["微信"],
                        "aliases": ["微信"],
                    }
                }
            },
        )
        with (
            patch.object(tools, "_focus_running_app", return_value=""),
            patch.object(tools, "_app_process_is_running", return_value=True),
            patch.object(tools, "_activate_tray_app", return_value=""),
            patch("assistant_app.tools.os.startfile") as startfile,
        ):
            opened = tools.execute("open_app", {"app": "微信"}, "tray-failure-test")
        self.assertFalse(opened.success)
        self.assertIn("避免重复启动", opened.content)
        startfile.assert_not_called()

    def test_web_search_is_announced_before_tool_execution(self) -> None:
        events: list[str] = []

        class FakeClient:
            budget = SimpleNamespace(currency="CNY")

            def __init__(self) -> None:
                self.calls = 0

            def chat(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return KimiResponse(
                        message={
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{
                                "id": "lookup-1",
                                "function": {
                                    "name": "search_web",
                                    "arguments": '{"query":"悉尼天气"}',
                                },
                            }],
                        },
                        model="kimi-k2.6",
                        input_tokens=0,
                        cached_input_tokens=0,
                        output_tokens=0,
                        cost=0.0,
                    )
                return KimiResponse(
                    message={"role": "assistant", "content": "今天是晴天。"},
                    model="kimi-k2.6",
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    cost=0.0,
                )

        class FakeTools:
            schemas: list[dict] = []

            def execute(self, name, _arguments, _task_id):
                events.append(f"tool:{name}")
                return ToolResult(True, "搜索完成")

            def close_research_browser(self):
                events.append("browser:closed")

        config = {
            "memory": {"recent_messages": 2, "max_retrieved_memories": 2},
            "routing": {
                **ROUTING,
                "max_tool_calls_per_task": 4,
                "max_k3_calls_per_task": 2,
                "max_screenshots_per_task": 2,
            },
        }
        agent = PersonalAgent(
            config,
            self.database,
            FakeClient(),  # type: ignore[arg-type]
            FakeTools(),  # type: ignore[arg-type]
            "lookup-test",
            progress_callback=lambda message: events.append(f"announce:{message}"),
        )
        agent.run("悉尼今天天气怎么样")
        self.assertEqual(events[0], "announce:这个我需要查一下，我去查查。")
        self.assertEqual(events[1], "tool:search_web")
        self.assertEqual(events[-1], "browser:closed")

    def test_screenshot_tool_result_upgrades_next_auto_call_to_k3(self) -> None:
        requested_models: list[str] = []
        screenshot = Path(self.temp.name) / "screen.png"
        screenshot.write_bytes(b"fake-png")

        class FakeClient:
            budget = SimpleNamespace(currency="CNY")

            def chat(self, **kwargs):
                requested_models.append(str(kwargs["model"]))
                if len(requested_models) == 1:
                    return KimiResponse(
                        message={
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{
                                "id": "screen-1",
                                "function": {
                                    "name": "inspect_screen",
                                    "arguments": "{}",
                                },
                            }],
                        },
                        model=str(kwargs["model"]),
                        input_tokens=0,
                        cached_input_tokens=0,
                        output_tokens=0,
                        cost=0.0,
                    )
                return KimiResponse(
                    message={"role": "assistant", "content": "我看到了。"},
                    model=str(kwargs["model"]),
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    cost=0.0,
                )

        class FakeTools:
            schemas: list[dict] = []

            def execute(self, *_args):
                return ToolResult(True, "已截取屏幕", image_path=screenshot)

        agent = PersonalAgent(
            {
                "memory": {"recent_messages": 2, "max_retrieved_memories": 2},
                "routing": {
                    **ROUTING,
                    "model_mode": "auto",
                    "max_tool_calls_per_task": 4,
                    "max_k3_calls_per_task": 2,
                    "max_screenshots_per_task": 2,
                },
                "skills": {"desk-lamp": True},
            },
            self.database,
            FakeClient(),  # type: ignore[arg-type]
            FakeTools(),  # type: ignore[arg-type]
            "visual-upgrade-test",
        )
        answer = agent.run("帮我看看现在是什么情况")
        self.assertEqual(requested_models, ["kimi-k2.6", "kimi-k3"])
        self.assertEqual(answer.route.model, "kimi-k3")
        self.assertIn("获取图像后自动切换 K3", answer.route.reasons)

    def test_simple_open_stops_after_success_without_second_model_call(self) -> None:
        class FakeClient:
            budget = SimpleNamespace(currency="CNY")

            def __init__(self) -> None:
                self.calls = 0

            def chat(self, **_kwargs):
                self.calls += 1
                return KimiResponse(
                    message={
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "open-1",
                            "function": {
                                "name": "open_app",
                                "arguments": '{"app":"calculator"}',
                            },
                        }],
                    },
                    model="kimi-k2.6",
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    cost=0.0,
                )

        class FakeTools:
            schemas: list[dict] = []

            def execute(self, *_args):
                return ToolResult(True, "已启动 calculator")

        client = FakeClient()
        agent = PersonalAgent(
            {
                "memory": {"recent_messages": 2, "max_retrieved_memories": 2},
                "routing": {
                    **ROUTING,
                    "max_tool_calls_per_task": 4,
                    "max_k3_calls_per_task": 2,
                    "max_screenshots_per_task": 2,
                },
                "skills": {"desk-lamp": True},
            },
            self.database,
            client,  # type: ignore[arg-type]
            FakeTools(),  # type: ignore[arg-type]
            "simple-open-test",
        )
        answer = agent.run("打开计算器")
        self.assertTrue(answer.silent)
        self.assertEqual(client.calls, 1)

    def test_simple_lamp_power_bypasses_model_and_executes_locally(self) -> None:
        class FakeClient:
            budget = SimpleNamespace(currency="CNY")

            def chat(self, **_kwargs):
                raise AssertionError("明确的开关灯命令不应调用模型")

        calls: list[tuple[str, dict]] = []

        class FakeTools:
            schemas: list[dict] = []

            def execute(self, name, arguments, _task_id):
                calls.append((name, arguments))
                return ToolResult(True, "米家台灯2已执行并确认：开灯。")

        agent = PersonalAgent(
            {
                "memory": {"recent_messages": 2, "max_retrieved_memories": 2},
                "routing": {
                    **ROUTING,
                    "max_tool_calls_per_task": 4,
                    "max_k3_calls_per_task": 2,
                    "max_screenshots_per_task": 2,
                },
                "skills": {"desk-lamp": True},
            },
            self.database,
            FakeClient(),  # type: ignore[arg-type]
            FakeTools(),  # type: ignore[arg-type]
            "direct-lamp-test",
        )
        answer = agent.run("猫猫，开灯", input_mode="voice")
        self.assertTrue(answer.silent)
        self.assertEqual(calls, [("control_desk_lamp", {"power": "on"})])

    def test_contextual_lamp_power_command_is_recognised(self) -> None:
        agent = PersonalAgent(
            {
                "memory": {"recent_messages": 2, "max_retrieved_memories": 2},
                "routing": ROUTING,
                "skills": {"desk-lamp": True},
            },
            self.database,
            client=None,  # type: ignore[arg-type]
            tools=None,  # type: ignore[arg-type]
            session_id="lamp-context-test",
        )
        self.assertEqual(agent._direct_lamp_power("帮我把灯关了"), "off")
        self.assertEqual(agent._direct_lamp_power("请开灯"), "on")
        self.assertIsNone(agent._direct_lamp_power("灯现在开着吗"))
        self.database.add_message("lamp-context-test", "assistant", "已经帮你把灯关了。")
        self.assertEqual(agent._direct_lamp_power("还是帮我打开吧"), "on")

    def test_starrail_daily_command_bypasses_model(self) -> None:
        class FakeClient:
            budget = SimpleNamespace(currency="CNY")

            def chat(self, **_kwargs):
                raise AssertionError("明确的星铁日常命令不应调用模型")

        calls: list[tuple[str, dict]] = []

        class FakeTools:
            schemas: list[dict] = []

            def execute(self, name, arguments, _task_id):
                calls.append((name, arguments))
                return ToolResult(True, "已点击完整运行。")

        agent = PersonalAgent(
            {
                "memory": {"recent_messages": 2, "max_retrieved_memories": 2},
                "routing": ROUTING,
                "skills": {"starrail-dailies": True},
            },
            self.database,
            FakeClient(),  # type: ignore[arg-type]
            FakeTools(),  # type: ignore[arg-type]
            "starrail-direct-test",
        )
        answer = agent.run("猫猫，帮我过星铁的日常", input_mode="voice")
        self.assertTrue(answer.silent)
        self.assertFalse(answer.continue_listening)
        self.assertEqual(calls, [("run_starrail_dailies", {})])
        self.assertTrue(PersonalAgent._direct_starrail_dailies("帮我过星穹铁道日常"))
        self.assertTrue(PersonalAgent._direct_starrail_dailies("帮我过新铁日常"))
        self.assertFalse(PersonalAgent._direct_starrail_dailies("先别做星铁日常"))

    def test_explicit_chatgpt_request_uses_browser_source_then_cat_summary(self) -> None:
        model_calls: list[dict] = []

        class FakeClient:
            budget = SimpleNamespace(currency="CNY")

            def chat(self, **kwargs):
                model_calls.append(kwargs)
                return KimiResponse(
                    message={"role": "assistant", "content": "这是猫猫整理后的结论。"},
                    model="kimi-k2.6",
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    cost=0.0,
                )

        calls: list[tuple[str, dict]] = []
        progress: list[str] = []

        class FakeTools:
            schemas: list[dict] = []

            def execute(self, name, arguments, _task_id):
                calls.append((name, arguments))
                return ToolResult(
                    True,
                    json.dumps(
                        {
                            "source": "ChatGPT 网页",
                            "source_text": "这里是网页返回的较长原始资料，一二三四。",
                        },
                        ensure_ascii=False,
                    ),
                )

        agent = PersonalAgent(
            {
                "memory": {"recent_messages": 2, "max_retrieved_memories": 2},
                "routing": ROUTING,
            },
            self.database,
            FakeClient(),  # type: ignore[arg-type]
            FakeTools(),  # type: ignore[arg-type]
            "chatgpt-direct-test",
            progress_callback=progress.append,
        )
        answer = agent.run(
            "嗯，那你帮我用 Chat GPT 查一下，呃，一二三四。",
            input_mode="voice",
        )

        self.assertEqual(calls, [("ask_chatgpt", {"question": "一二三四。"})])
        self.assertEqual(answer.text, "这是猫猫整理后的结论。")
        self.assertEqual(answer.route.model, "kimi-k2.6")
        self.assertEqual(answer.task_cost, 0.0)
        self.assertEqual(progress, ["好呀，我现在就问问他。"])
        self.assertEqual(model_calls[0]["tools"], [])
        self.assertIn("用户原问题：一二三四。", model_calls[0]["messages"][1]["content"])
        self.assertIn("较长原始资料", model_calls[0]["messages"][1]["content"])
        self.assertIsNone(PersonalAgent._direct_chatgpt_question("不要用 ChatGPT，自己回答"))
        self.assertIsNone(PersonalAgent._direct_chatgpt_question("ChatGPT 是什么？"))
        self.assertIsNone(PersonalAgent._direct_chatgpt_question("GPT。"))
        self.assertEqual(
            PersonalAgent._direct_chatgpt_question(
                "不知道今天吃啥，要不你帮我问问GPT。"
            ),
            "不知道今天吃啥",
        )
        agent.run("不知道今天吃啥，要不你帮我问问GPT。", input_mode="voice")
        self.assertEqual(calls[-1], ("ask_chatgpt", {"question": "不知道今天吃啥"}))
        self.assertEqual(progress[-1], "好呀，我现在就问问他。")
        self.assertEqual(len(model_calls), 2)

    def test_chatgpt_web_skill_requests_source_material_for_cat(self) -> None:
        prompt = ToolRegistry._chatgpt_source_prompt("不知道今天吃什么")
        self.assertIn("资料检索任务", prompt)
        self.assertIn("另一个助手整理", prompt)
        self.assertIn("用户的问题：不知道今天吃什么", prompt)
        raw = (
            "### 建议\n"
            "1. 今天可以优先吃一碗清淡的牛肉面。\n"
            "2. 如果不想吃面，就选附近评价较好的简餐。\n"
            "3. 还可以继续比较十家餐厅。"
        )
        self.assertEqual(
            ToolRegistry._concise_chatgpt_answer(raw),
            "建议 今天可以优先吃一碗清淡的牛肉面。如果不想吃面，就选附近评价较好的简餐。",
        )

    def test_chatgpt_web_reuses_current_tab_instead_of_launching_edge_url(self) -> None:
        root = Path(self.temp.name)
        screenshots = root / "screenshots"
        screenshots.mkdir(exist_ok=True)
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        tools = ToolRegistry(paths, self.database, "chatgpt-tab-test", lambda *_: True)
        entered: list[str] = []

        class ElementInfo:
            def __init__(self, automation_id=""):
                self.automation_id = automation_id

        class Editor:
            element_info = ElementInfo("prompt-textarea")

            def set_edit_text(self, value):
                entered.append(value)

        class Submit:
            element_info = ElementInfo("composer-submit-button")

            def invoke(self):
                return None

        editor = Editor()
        submit = Submit()

        class FakeWindow:
            handle = 321

            def set_focus(self):
                return None

            def descendants(self, *_, **kwargs):
                if kwargs.get("title") == "新聊天":
                    return []
                if kwargs.get("control_type") == "Edit":
                    return [editor]
                if kwargs.get("control_type") == "Button":
                    return [submit]
                return []

        window = FakeWindow()

        class FakeWindowSpec:
            def wrapper_object(self):
                return window

        class FakeDesktop:
            def __init__(self, **_):
                pass

            def windows(self, **_):
                return [window]

            def window(self, **_):
                return FakeWindowSpec()

        clock = [0.0]

        def monotonic():
            clock[0] += 4.0
            return clock[0]

        with (
            patch.object(
                tools,
                "_navigate_browser_humanlike",
                return_value="已在当前浏览器标签页打开页面",
            ) as navigate,
            patch.object(tools, "_chatgpt_response_text", return_value="网页原始资料。"),
            patch("pywinauto.Desktop", FakeDesktop),
            patch("assistant_app.tools.time.monotonic", side_effect=monotonic),
            patch("assistant_app.tools.time.sleep"),
            patch("assistant_app.tools.subprocess.Popen") as popen,
        ):
            result = tools._tool_ask_chatgpt({"question": "今天吃什么"})

        self.assertTrue(result.success)
        navigate.assert_called_once_with("https://chatgpt.com/")
        popen.assert_not_called()
        self.assertIn("资料检索任务", entered[0])
        payload = json.loads(result.content)
        self.assertEqual(payload["source_text"], "网页原始资料。")
        self.assertIn("当前浏览器标签页", payload["navigation"])

    def test_close_browser_is_a_loaded_local_skill_and_bypasses_models(self) -> None:
        self.assertIn("close-current-browser", {skill.name for skill in LOCAL_SKILLS})
        self.assertEqual(
            match_local_skill("嗯，帮我关掉现在的浏览器。").tool_name,
            "close_browser",
        )
        self.assertIsNone(match_local_skill("先别关浏览器"))
        self.assertIsNone(match_local_skill("浏览器应该怎么关？"))

        class FakeClient:
            budget = SimpleNamespace(currency="CNY")

            def chat(self, **_kwargs):
                raise AssertionError("本地关闭浏览器技能不应调用 Kimi 或 MiMo")

        calls: list[tuple[str, dict]] = []
        progress: list[str] = []

        class FakeTools:
            schemas: list[dict] = []

            def execute(self, name, arguments, _task_id):
                calls.append((name, arguments))
                return ToolResult(True, "已关闭当前浏览器窗口。")

        agent = PersonalAgent(
            {
                "memory": {"recent_messages": 2, "max_retrieved_memories": 2},
                "routing": ROUTING,
            },
            self.database,
            FakeClient(),  # type: ignore[arg-type]
            FakeTools(),  # type: ignore[arg-type]
            "close-browser-skill-test",
            progress_callback=progress.append,
        )
        answer = agent.run("嗯，帮我关掉现在的浏览器。", input_mode="voice")
        self.assertEqual(calls, [("close_browser", {})])
        self.assertEqual(progress, ["好，我关掉当前浏览器。"])
        self.assertEqual(answer.route.model, "local-skill")
        self.assertEqual(answer.task_cost, 0.0)
        self.assertTrue(answer.silent)

    def test_close_browser_tool_closes_only_the_selected_window(self) -> None:
        root = Path(self.temp.name)
        screenshots = root / "screenshots"
        screenshots.mkdir(exist_ok=True)
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        tools = ToolRegistry(paths, self.database, "close-browser-tool-test", lambda *_: True)
        window = Mock()
        window.handle = 2468
        window.window_text.return_value = "测试页面 - Microsoft Edge"
        with patch.object(tools, "_visible_browser_window", return_value=window):
            result = tools.execute("close_browser", {}, "close-browser-tool-test")
        self.assertTrue(result.success)
        window.close.assert_called_once_with()
        self.assertIn("测试页面", result.content)

    def test_disabled_skill_is_hidden_and_rejected_by_tool_registry(self) -> None:
        root = Path(self.temp.name)
        screenshots = root / "screenshots-disabled"
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        tools = ToolRegistry(
            paths,
            self.database,
            "disabled-skill-test",
            lambda *_: True,
            skills_config={"close-current-browser": False},
        )
        names = {schema["function"]["name"] for schema in tools.schemas}
        self.assertNotIn("close_browser", names)
        result = tools.execute("close_browser", {}, "disabled-skill-test")
        self.assertFalse(result.success)
        self.assertIn("技能已关闭", result.content)

    def test_temporary_screenshots_are_deleted_but_saved_ones_remain(self) -> None:
        from PIL import Image

        root = Path(self.temp.name)
        screenshots = root / "screenshots-lifecycle"
        paths = AppPaths(root, root, root / "test.db", screenshots, root / "api_key.txt")
        tools = ToolRegistry(paths, self.database, "screenshot-lifecycle-test", lambda *_: True)
        with patch("assistant_app.tools.ImageGrab.grab", return_value=Image.new("RGB", (64, 48))):
            temporary = tools.execute("inspect_screen", {}, "temporary-shot")
            self.assertTrue(temporary.image_path.is_file())
            saved = tools.execute("save_screenshot", {}, "saved-shot")
        saved_path = Path(saved.content.split("：", 1)[1])
        self.assertTrue(saved_path.is_file())
        self.assertIn("saved", saved_path.parts)
        tools.close()
        self.assertFalse(temporary.image_path.exists())
        self.assertTrue(saved_path.exists())

    def test_visible_actions_have_natural_announcements(self) -> None:
        self.assertEqual(
            PersonalAgent._tool_announcement("open_url", {"url": "https://example.com"}),
            "好，我现在打开这个网站。",
        )
        self.assertEqual(
            PersonalAgent._tool_announcement("open_app", {"app": "calculator"}),
            "好，我现在打开计算器。",
        )
        self.assertEqual(
            PersonalAgent._tool_announcement("open_app", {"app": "微信"}),
            "好，我现在打开微信。",
        )
        self.assertEqual(
            PersonalAgent._tool_announcement("ask_chatgpt", {}),
            "好呀，我现在就问问他。",
        )
        self.assertEqual(PersonalAgent._tool_announcement("read_text_file", {}), "")
        self.assertEqual(
            PersonalAgent._tool_announcement("control_desk_lamp", {"power": "off"}),
            "好，我关灯。",
        )
        self.assertEqual(
            PersonalAgent._tool_announcement("control_desk_lamp", {"mode": "reading"}),
            "好，我把台灯调成阅读模式。",
        )
        self.assertTrue(PersonalAgent._is_simple_open_only("打开微信", "open_app"))
        self.assertTrue(PersonalAgent._is_simple_open_only("请打开这个网站", "open_url"))
        self.assertTrue(PersonalAgent._is_simple_open_only("猫猫，关灯", "control_desk_lamp"))
        self.assertTrue(
            PersonalAgent._is_simple_open_only("把台灯亮度调到50%", "control_desk_lamp")
        )
        self.assertFalse(
            PersonalAgent._is_simple_open_only("打开微信，然后告诉我天气", "open_app")
        )

    def test_continuous_conversation_exit_phrases(self) -> None:
        for phrase in (
            "再见",
            "那今天先这样，拜拜啦",
            "okay, bye-bye 猫猫",
            "已经没事了，谢谢",
            "可以，可以，没你什么事了。",
            "没有什么事情了",
            "行了，没啥事儿啦",
            "暂时没有别的事",
            "好了，现在不需要你了",
            "请停止对话吧",
        ):
            self.assertTrue(AssistantWindow._is_conversation_exit(phrase))
        self.assertFalse(AssistantWindow._is_conversation_exit("继续帮我看看天气"))
        for filler in ("嗯。", "嗯嗯", "呃", "哦", "啊啊", "好", "hmm"):
            self.assertTrue(AssistantWindow._is_filler_only(filler))
        self.assertFalse(AssistantWindow._is_filler_only("嗯，帮我开灯"))
        self.assertFalse(AssistantWindow._is_filler_only("没有了"))

    def test_asr_language_tag_hallucinations_are_removed(self) -> None:
        noisy = " ".join(["<chinese>"] * 500)
        self.assertEqual(SpeechTranscriber.clean_transcript(noisy), "")
        self.assertEqual(
            SpeechTranscriber.clean_transcript("<chinese> 帮我开灯"),
            "帮我开灯",
        )

    def test_gui_discards_tag_only_transcription_before_submit(self) -> None:
        window = AssistantWindow.__new__(AssistantWindow)
        window.busy = True
        window.record_button = Mock()
        window.send_button = Mock()
        window.timing = Mock()
        window.status = Mock()
        window.wake_listener = SimpleNamespace(keyword="猫猫")
        window._continuous_session_active = True
        window._continuous_listening = True
        window._timing_summary = Mock(return_value="ASR 0.1s")
        window._submit = Mock()

        window._submit_transcription("<chinese> <chinese> <chinese>", 1.0, 0.1)

        window._submit.assert_not_called()
        self.assertFalse(window.busy)
        self.assertFalse(window._continuous_session_active)
        window.status.set.assert_called_once()

    def test_adaptive_voice_gate_rejects_transient_and_steady_noise(self) -> None:
        recorder = ButtonAudioRecorder(
            sample_rate=16000,
            channels=1,
            silence_threshold=500.0,
            speech_start_seconds=0.30,
            adaptive_noise_multiplier=2.2,
            adaptive_noise_offset=80.0,
        )
        recorder._process_audio_level(2200.0, 0.10, 0.10)
        recorder._process_audio_level(100.0, 0.10, 0.20)
        self.assertFalse(recorder._speech_started.is_set())
        for index in range(6):
            recorder._process_audio_level(600.0, 0.10, 0.30 + index * 0.10)
        self.assertFalse(recorder._speech_started.is_set())
        for index in range(3):
            recorder._process_audio_level(1800.0, 0.10, 1.00 + index * 0.10)
        self.assertTrue(recorder._speech_started.is_set())
        speech_ended_at = recorder._last_voice_at
        for index in range(10):
            recorder._process_audio_level(600.0, 0.10, 1.30 + index * 0.10)
        self.assertEqual(recorder._last_voice_at, speech_ended_at)
        recorder._process_audio_level(1800.0, 0.05, 2.30)
        recorder._process_audio_level(1800.0, 0.05, 2.35)
        self.assertEqual(recorder._last_voice_at, 2.35)

    def test_post_speech_steady_noise_stops_refreshing_voice_activity(self) -> None:
        recorder = ButtonAudioRecorder(
            sample_rate=16000,
            channels=1,
            silence_threshold=500.0,
            speech_start_seconds=0.30,
            adaptive_noise_multiplier=2.2,
            adaptive_noise_offset=80.0,
        )
        for index in range(3):
            recorder._process_audio_level(1800.0, 0.10, 0.10 + index * 0.10)
        self.assertTrue(recorder._speech_started.is_set())

        for index in range(20):
            recorder._process_audio_level(650.0, 0.10, 0.40 + index * 0.10)
        settled_at = recorder._last_voice_at
        for index in range(10):
            recorder._process_audio_level(650.0, 0.10, 2.40 + index * 0.10)

        self.assertLess(settled_at, 1.5)
        self.assertEqual(recorder._last_voice_at, settled_at)

    def test_preload_announcement_is_the_last_warmup_step(self) -> None:
        window = AssistantWindow.__new__(AssistantWindow)
        events: list[str] = []
        window.transcriber = Mock()
        window.transcriber.warmup.side_effect = lambda: events.append("asr")
        window.tts = Mock()
        window.tts.engine_label = "测试语音"
        window.tts.warmup.side_effect = lambda: events.append("announcement")
        window.asr_mode = Mock()
        window.asr_mode.get.return_value = "测试转写"
        window._prepare_voice_clips = Mock(side_effect=lambda: events.append("clips"))
        window._finish_tts_warmup_status = Mock(side_effect=lambda: events.append("finished"))
        window._preload_generation = 1
        window.preload_enabled = Mock()
        window.preload_enabled.get.return_value = True
        window.closing = False
        window.status = Mock()
        window.root = Mock()
        window.root.after.side_effect = lambda _delay, callback, *args: callback(*args)

        window._warmup_worker(1)

        self.assertEqual(events, ["asr", "clips", "announcement", "finished"])

    def test_voice_clip_rejects_silence_but_accepts_quiet_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            quiet_path = Path(temp_dir) / "quiet.wav"
            silent_path = Path(temp_dir) / "silent.wav"
            import wave

            for path, amplitude in ((quiet_path, 40), (silent_path, 0)):
                with wave.open(str(path), "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(16000)
                    output.writeframes(np.full(1600, amplitude, dtype=np.int16).tobytes())
            with patch("winsound.PlaySound"):
                play_wav_file(quiet_path)
                with self.assertRaisesRegex(RuntimeError, "没有有效声音"):
                    play_wav_file(silent_path)
            self.assertTrue(SpeechSynthesizer._cached_clip_is_audible(quiet_path))
            self.assertFalse(SpeechSynthesizer._cached_clip_is_audible(silent_path))

    def test_cloud_asr_safety_refusal_reconnects_but_never_uses_local_without_consent(self) -> None:
        transcriber = SpeechTranscriber.__new__(SpeechTranscriber)
        transcriber.backend = "mimo-api"
        transcriber.primary = Mock()
        transcriber.fallback = Mock()
        transcriber._reconnect_callback = Mock()
        transcriber.primary.transcribe.return_value = (
            "The request was rejected because it was considered high risk"
        )
        path = Path("recording.wav")

        with self.assertRaises(APIReconnectFailed):
            transcriber.transcribe(path)
        transcriber.primary.reconnect.assert_called_once_with()
        self.assertEqual(transcriber.primary.transcribe.call_count, 2)
        transcriber.fallback.transcribe.assert_not_called()

    def test_normal_english_asr_text_is_not_mistaken_for_provider_refusal(self) -> None:
        self.assertFalse(
            SpeechTranscriber._is_provider_rejection("Please open the browser for me")
        )

    def test_voice_input_source_is_explained_to_model(self) -> None:
        agent = PersonalAgent(
            {"memory": {"recent_messages": 2, "max_retrieved_memories": 2}},
            self.database,
            client=None,  # type: ignore[arg-type]
            tools=None,  # type: ignore[arg-type]
            session_id="test",
        )
        messages = agent._messages("你能听见吗", input_mode="voice")
        self.assertIn("本地麦克风", messages[0]["content"])
        self.assertIn("Qwen3-ASR", messages[0]["content"])

    def test_cost_calculation_splits_cached_tokens(self) -> None:
        cost = BudgetManager.estimate_cost("kimi-k2.6", 1_000_000, 250_000, 100_000)
        self.assertAlmostEqual(cost, 7.85)

    def test_legacy_usd_usage_is_migrated_without_mixing_currencies(self) -> None:
        self.database.close()
        path = Path(self.temp.name) / "legacy.db"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE api_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL,
                estimated_cost_usd REAL NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO api_usage(
                session_id, task_id, model, input_tokens,
                cached_input_tokens, output_tokens,
                estimated_cost_usd, created_at
            ) VALUES ('s', 't', 'kimi-k2.6', 10, 0, 1, 0.25, datetime('now'));
            """
        )
        connection.close()
        migrated = Database(path)
        try:
            self.assertAlmostEqual(migrated.usage_total("day", currency="USD"), 0.25)
            self.assertEqual(migrated.usage_total("day", currency="CNY"), 0)
        finally:
            migrated.close()
        self.database = Database(Path(self.temp.name) / "test.db")


if __name__ == "__main__":
    unittest.main()
