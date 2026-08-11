from __future__ import annotations

import ctypes
import json
import random
import re
import subprocess
import sys
import threading
import time
import uuid
import wave
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PySide6.QtCore import QObject, Property, QTimer, QUrl, Signal, Slot

from ..audio import APIReconnectFailed, ButtonAudioRecorder, SpeechTranscriber, WakeWordListener
from ..budget import BudgetExceeded
from ..cli import build_agent
from ..components import COMPONENTS_BY_ID, install_component, missing_startup_components
from ..config import app_paths, load_config, save_local_settings
from ..database import Database
from ..secrets import key_configuration_status, save_api_keys
from ..skills import SKILL_CATALOG, is_skill_enabled
from ..tts import ENGINE_BACKENDS, ENGINE_LABELS, MIMO_VOICES, VOICE_PRESETS, SpeechSynthesizer


MODEL_MODES = {
    "auto": "自动（Kimi 优先）",
    "k3": "Kimi K3",
    "k2.6": "Kimi K2.6",
    "mimo-pro": "MiMo V2.5 Pro",
    "mimo-omni": "MiMo V2.5",
}
ASR_MODES = {"mimo-api": "API · MiMo-V2.5-ASR"}
HIGH_RISK_PATTERN = re.compile(
    r"删除|付款|支付|购买|下单|发送消息|提交表单|验证码|密码|管理员|卸载"
)
EXIT_PATTERN = re.compile(r"^(拜拜|再见|不聊了|先这样|结束对话|退出连续对话)[吧呀啊啦。！!]*$")


class AssistantBridge(QObject):
    messagesChanged = Signal()
    skillsChanged = Signal()
    schedulesChanged = Signal()
    permissionsChanged = Signal()
    busyChanged = Signal()
    recordingChanged = Signal()
    speakingChanged = Signal()
    statusChanged = Signal()
    usageChanged = Signal()
    modelModeChanged = Signal()
    audioSettingsChanged = Signal()
    wakeSettingsChanged = Signal()
    keyStatusChanged = Signal()
    integrationStatusChanged = Signal()
    startupChanged = Signal()
    uiSettingsChanged = Signal()
    confirmationRequested = Signal(str, str, bool)
    asrFallbackRequested = Signal(str)
    notificationRequested = Signal(str, str)
    showWindowRequested = Signal()

    _answerReady = Signal(str, str, str)
    _taskFailed = Signal(str)
    _transcriptionReady = Signal(str, float)
    _transcriptionFailed = Signal(str)
    _speechFinished = Signal(str, str)
    _speechFailed = Signal(str)
    _wakeTriggered = Signal()
    _wakeStatusReady = Signal(str)
    _progressReady = Signal(str)
    _scheduleFinished = Signal(int, bool, str, bool)
    _enrollmentFinished = Signal(bool, str)
    _startupProgressReady = Signal(int, str)
    _preloadStateReady = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.config = load_config()
        self.config.setdefault("skills", {})
        self.config.setdefault("ui", {})
        self.session_id = uuid.uuid4().hex
        self._messages: list[dict[str, str]] = []
        self._busy = False
        self._recording = False
        self._speaking = False
        self._preload_loading = False
        self._closing = False
        self._status = "正在初始化…"
        self._usage = "今日 ￥0.00 · 本月 ￥0.00"
        self._last_input_mode = "text"
        self._continuous_session = False
        self._runtime: tuple[Any, Database, Any] | None = None
        self._task_cancel_event = threading.Event()
        self._scheduled_cancel_event = threading.Event()
        self._confirmation_event = threading.Event()
        self._confirmation_result = False
        self._confirmation_remember = False
        self._pending_confirmation: tuple[str, dict[str, Any], str, bool] | None = None
        self._asr_fallback_event = threading.Event()
        self._asr_fallback_result = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="maomao-agent")
        self._voice_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="maomao-voice")
        self._schedule_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="maomao-schedule")

        wake = self.config.get("wake_word", {})
        self._recorder = ButtonAudioRecorder(
            int(self.config["audio"].get("sample_rate", 16000)),
            int(self.config["audio"].get("channels", 1)),
            float(wake.get("silence_threshold", 420.0)),
            float(wake.get("speech_start_seconds", 0.30)),
            float(wake.get("adaptive_noise_multiplier", 2.2)),
            float(wake.get("adaptive_noise_offset", 80.0)),
        )
        self._transcriber = SpeechTranscriber(self.config["audio"])
        self._transcriber.set_reconnect_callback(
            lambda service: self._progressReady.emit(f"{service} 连接中断，正在重新连接…")
        )
        self._tts = SpeechSynthesizer(self.config["tts"])
        self._tts.set_reconnect_callback(
            lambda service: self._progressReady.emit(f"{service} 连接中断，正在重新连接…")
        )
        self._wake_listener = WakeWordListener(
            wake,
            on_wake=self._wakeTriggered.emit,
            on_status=lambda value: self._wakeStatusReady.emit(str(value)),
        )
        self._wake_enabled = bool(wake.get("capture_enabled", True)) and is_skill_enabled(
            self.config, "wake-word"
        )
        self._wake_listener.set_capture_enabled(self._wake_enabled)
        self._ui_database = Database(app_paths().database)
        self._ui_database.recover_interrupted_scheduled_tasks()
        self._startup_components = missing_startup_components()
        self._startup_progress = 0
        self._startup_message = "需要安装启动组件" if self._startup_components else ""

        self._answerReady.connect(self._finish_answer)
        self._taskFailed.connect(self._finish_error)
        self._transcriptionReady.connect(self._finish_transcription)
        self._transcriptionFailed.connect(self._finish_transcription_error)
        self._speechFinished.connect(self._finish_speech)
        self._speechFailed.connect(self._finish_speech_error)
        self._wakeTriggered.connect(self._handle_wake)
        self._wakeStatusReady.connect(self._set_wake_status)
        self._progressReady.connect(self._set_progress)
        self._scheduleFinished.connect(self._finish_schedule)
        self._enrollmentFinished.connect(self._finish_enrollment)
        self._startupProgressReady.connect(self._set_startup_progress)
        self._preloadStateReady.connect(self._set_preload_loading)

        self._schedule_timer = QTimer(self)
        self._schedule_timer.setInterval(1500)
        self._schedule_timer.timeout.connect(self._poll_schedules)
        self._schedule_timer.start()
        self.refreshUsage()
        QTimer.singleShot(200, self.startServices)

    @Property("QVariantList", notify=messagesChanged)
    def messages(self) -> list[dict[str, str]]:
        return list(self._messages)

    @Property("QVariantList", notify=skillsChanged)
    def skills(self) -> list[dict[str, Any]]:
        favorites = set(self.config.get("ui", {}).get("favorite_skills", []))
        return [
            {
                "id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "category": skill.category,
                "enabled": is_skill_enabled(self.config, skill.id),
                "favorite": skill.id in favorites,
                "hasSettings": skill.id
                in {"wake-word", "continuous-conversation", "desk-lamp", "starrail-dailies"},
            }
            for skill in SKILL_CATALOG
        ]

    @Property("QVariantList", notify=schedulesChanged)
    def schedules(self) -> list[dict[str, Any]]:
        status_names = {
            "pending": "等待执行",
            "running": "执行中",
            "succeeded": "上次成功",
            "failed": "上次失败",
            "cancelled": "已停用",
        }
        values = []
        for task in self._ui_database.list_scheduled_tasks(include_disabled=True):
            item = dict(task)
            item["displayTime"] = str(task["next_run_at"])[:16].replace("T", " ")
            item["repeatLabel"] = "每天" if task["repeat_rule"] == "daily" else "仅一次"
            item["modeLabel"] = "静默" if task["silent"] else "普通"
            item["statusLabel"] = status_names.get(str(task["last_status"]), str(task["last_status"]))
            item["enabled"] = bool(task["enabled"])
            values.append(item)
        return values

    @Property("QVariantList", notify=permissionsChanged)
    def permissions(self) -> list[dict[str, Any]]:
        return self._managed_permissions()

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    @Property(bool, notify=recordingChanged)
    def recording(self) -> bool:
        return self._recording

    @Property(bool, notify=speakingChanged)
    def speaking(self) -> bool:
        return self._speaking

    @Property(str, notify=statusChanged)
    def status(self) -> str:
        return self._status

    @Property(str, notify=usageChanged)
    def usage(self) -> str:
        return self._usage

    @Property(str, notify=modelModeChanged)
    def modelMode(self) -> str:
        return str(self.config.get("routing", {}).get("model_mode", "auto"))

    @Property("QVariantList", constant=True)
    def modelOptions(self) -> list[dict[str, str]]:
        return [{"value": key, "label": label} for key, label in MODEL_MODES.items()]

    @Property("QVariantList", constant=True)
    def asrOptions(self) -> list[dict[str, str]]:
        return [{"value": key, "label": label} for key, label in ASR_MODES.items()]

    @Property(str, notify=audioSettingsChanged)
    def asrMode(self) -> str:
        return str(self.config.get("audio", {}).get("backend", "mimo-api"))

    @Property("QVariantList", notify=audioSettingsChanged)
    def ttsEngineOptions(self) -> list[dict[str, str]]:
        allow_local = bool(self.config.get("tts", {}).get("allow_local_engines", True))
        return [
            {"value": backend, "label": label}
            for backend, label in ENGINE_LABELS.items()
            if allow_local or backend == "mimo-api"
        ]

    @Property(str, notify=audioSettingsChanged)
    def ttsEngine(self) -> str:
        return str(self.config.get("tts", {}).get("backend", "mimo-api"))

    @Property("QVariantList", notify=audioSettingsChanged)
    def voiceOptions(self) -> list[str]:
        return list(MIMO_VOICES if self.ttsEngine == "mimo-api" else VOICE_PRESETS)

    @Property(str, notify=audioSettingsChanged)
    def voice(self) -> str:
        key = "mimo_voice" if self.ttsEngine == "mimo-api" else "speaker"
        return str(self.config.get("tts", {}).get(key, "冰糖" if key == "mimo_voice" else "default"))

    @Property(bool, notify=audioSettingsChanged)
    def ttsEnabled(self) -> bool:
        return bool(self.config.get("tts", {}).get("enabled", True))

    @Property(bool, notify=audioSettingsChanged)
    def preloadEnabled(self) -> bool:
        return bool(self.config.get("tts", {}).get("preload_on_startup", True))

    @Property(bool, notify=audioSettingsChanged)
    def preloadLoading(self) -> bool:
        return self._preload_loading

    @Property(bool, notify=wakeSettingsChanged)
    def wakeEnabled(self) -> bool:
        return self._wake_enabled

    @Property(str, notify=wakeSettingsChanged)
    def wakeWords(self) -> str:
        return "、".join(self._wake_listener.aliases)

    @Property(bool, notify=wakeSettingsChanged)
    def wakeEnrolled(self) -> bool:
        return self._wake_listener.enrolled

    @Property(bool, notify=wakeSettingsChanged)
    def continuousEnabled(self) -> bool:
        return bool(self.config.get("conversation", {}).get("continuous", True)) and is_skill_enabled(
            self.config, "continuous-conversation"
        )

    @Property("QVariantMap", notify=keyStatusChanged)
    def keyStatus(self) -> dict[str, bool]:
        return key_configuration_status(app_paths().key_file)

    @Property("QVariantMap", notify=integrationStatusChanged)
    def integrationStatus(self) -> dict[str, Any]:
        automation = self.config.get("automation", {}).get("starrail_dailies", {})
        devices = self._xiaomi_devices()
        return {
            "xiaomiConfigured": bool(devices),
            "xiaomiDevices": devices,
            "starrailExecutableConfigured": bool(str(automation.get("executable", "")).strip()),
            "starrailExecutable": str(automation.get("executable", "")).strip(),
        }

    @staticmethod
    def _xiaomi_devices() -> list[dict[str, str]]:
        token_path = app_paths().root / "xiaomi_token.txt"
        if not token_path.is_file():
            return []
        values: dict[str, str] = {}
        for line in token_path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip().lower()] = value.strip()
        token = values.get("token", "")
        valid = bool(values.get("ip")) and len(token) == 32 and all(
            character in "0123456789abcdefABCDEF" for character in token
        )
        if not valid:
            return []
        return [{key: values.get(key, "") for key in ("did", "name", "model", "ip")}]

    @Property(bool, notify=startupChanged)
    def startupReady(self) -> bool:
        return not self._startup_components

    @Property(int, notify=startupChanged)
    def startupProgress(self) -> int:
        return self._startup_progress

    @Property(str, notify=startupChanged)
    def startupMessage(self) -> str:
        return self._startup_message

    @Property(str, constant=True)
    def defaultScheduleTime(self) -> str:
        next_hour = datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return next_hour.strftime("%Y-%m-%d %H:%M")

    @Property(bool, notify=uiSettingsChanged)
    def skillSidebarExpanded(self) -> bool:
        return bool(self.config.get("ui", {}).get("skill_sidebar_expanded", True))

    @Property(bool, notify=uiSettingsChanged)
    def favoriteSidebarExpanded(self) -> bool:
        return bool(self.config.get("ui", {}).get("favorite_sidebar_expanded", False))

    def _set_busy(self, value: bool, status: str) -> None:
        if self._busy != value:
            self._busy = value
            self.busyChanged.emit()
        self._set_status(status)

    def _set_status(self, value: str) -> None:
        if self._status != value:
            self._status = value
            self.statusChanged.emit()

    @Slot(str)
    def _set_progress(self, value: str) -> None:
        self._set_status(value)

    def _append_message(self, role: str, text: str, meta: str = "") -> None:
        self._messages.append({"role": role, "text": text, "meta": meta})
        self.messagesChanged.emit()

    def _ensure_runtime(self):
        if self._runtime is None:
            self._runtime = build_agent(
                self.config,
                self.session_id,
                confirmation_callback=self._confirm_tool,
                progress_callback=lambda value: self._progressReady.emit(str(value)),
                cancel_event=self._task_cancel_event,
                include_tts=False,
            )
        return self._runtime

    def _run_prompt(self, text: str, input_mode: str) -> None:
        try:
            agent, _database, _tts = self._ensure_runtime()
            answer = agent.run(text, input_mode=input_mode)
            reason = "、".join(answer.route.reasons)
            meta = f"{answer.route.model} · {answer.route.reasoning}"
            if reason:
                meta += f" · {reason}"
            self._answerReady.emit(answer.text, meta, input_mode)
        except BudgetExceeded as exc:
            self._taskFailed.emit(f"预算限制：{exc}")
        except Exception as exc:
            self._taskFailed.emit(f"任务失败：{exc}")

    def _submit(self, text: str, input_mode: str) -> None:
        text = text.strip()
        if not text or self._busy or self._closing:
            return
        self._task_cancel_event.clear()
        self._last_input_mode = input_mode
        self._append_message("user", text, "语音转写" if input_mode == "voice" else "")
        self._set_busy(True, "正在思考…")
        self._executor.submit(self._run_prompt, text, input_mode)

    @Slot(str)
    def submit(self, text: str) -> None:
        self._submit(text, "text")

    @Slot(str, str, str)
    def _finish_answer(self, text: str, meta: str, input_mode: str) -> None:
        self._append_message("assistant", text, meta)
        self.refreshUsage()
        if self.ttsEnabled:
            self._speaking = True
            self.speakingChanged.emit()
            self._set_busy(False, "正在播报…")
            self._voice_executor.submit(self._speak_answer, text, input_mode)
        else:
            self._set_busy(False, "就绪")
            if input_mode == "voice" and self.continuousEnabled:
                self._begin_continuous_recording()

    @Slot(str)
    def _finish_error(self, message: str) -> None:
        self._append_message("system", message)
        self._set_busy(False, "发生错误")
        self._resume_wake()

    def _speak_answer(self, text: str, input_mode: str) -> None:
        try:
            self._wake_listener.pause(wait=True)
            self._tts.speak(text, delivery_mode="continuous")
            self._speechFinished.emit(input_mode, "播报完成")
        except Exception as exc:
            self._speechFailed.emit(str(exc))

    @Slot(str, str)
    def _finish_speech(self, input_mode: str, message: str) -> None:
        self._speaking = False
        self.speakingChanged.emit()
        self._set_status(message)
        if input_mode == "voice" and self.continuousEnabled and self._continuous_session:
            self._begin_continuous_recording()
        else:
            self._resume_wake()

    @Slot(str)
    def _finish_speech_error(self, message: str) -> None:
        self._speaking = False
        self.speakingChanged.emit()
        self._set_status(f"语音播报失败：{message}")
        self._resume_wake()

    @Slot()
    def toggleRecording(self) -> None:
        if self._closing or self._busy:
            return
        if not self._recording:
            try:
                self._wake_listener.pause(wait=True)
                self._recorder.start()
                self._continuous_session = self.continuousEnabled
                self._recording = True
                self.recordingChanged.emit()
                self._set_status("正在录音…再次点击结束")
            except Exception as exc:
                self._set_status(f"无法开始录音：{exc}")
                self._resume_wake()
            return
        self._recording = False
        self.recordingChanged.emit()
        self._set_busy(True, "正在转写…")
        self._voice_executor.submit(self._stop_and_transcribe)

    def _stop_and_transcribe(self) -> None:
        started = time.monotonic()
        try:
            path = self._recorder.stop()
            try:
                text = self._transcriber.transcribe(path)
            except APIReconnectFailed as exc:
                self._asr_fallback_result = False
                self._asr_fallback_event.clear()
                self.asrFallbackRequested.emit(str(exc))
                self._asr_fallback_event.wait()
                if not self._asr_fallback_result:
                    raise RuntimeError("语音转写已取消") from exc
                text = self._transcriber.transcribe_once_with_local(path)
            finally:
                path.unlink(missing_ok=True)
            self._transcriptionReady.emit(text, time.monotonic() - started)
        except Exception as exc:
            self._transcriptionFailed.emit(str(exc))

    @Slot(bool)
    def resolveAsrFallback(self, approved: bool) -> None:
        self._asr_fallback_result = approved
        self._asr_fallback_event.set()

    @Slot(str, float)
    def _finish_transcription(self, text: str, seconds: float) -> None:
        text = SpeechTranscriber.clean_transcript(text)
        self._busy = False
        self.busyChanged.emit()
        if not text:
            self._set_status("没有识别到有效语音")
            self._continuous_session = False
            self._resume_wake()
            return
        if EXIT_PATTERN.match(re.sub(r"\s+", "", text)):
            self._append_message("user", text, "语音转写")
            self._continuous_session = False
            self._set_status("连续对话已结束")
            goodbye = str(self.config.get("conversation", {}).get("goodbye_text", "拜拜，下次再聊。"))
            self._voice_executor.submit(self._speak_answer, goodbye, "text")
            return
        self._set_status(f"转写完成 · {seconds:.1f} 秒")
        self._submit(text, "voice")

    @Slot(str)
    def _finish_transcription_error(self, message: str) -> None:
        self._recording = False
        self.recordingChanged.emit()
        self._set_busy(False, f"语音转写失败：{message}")
        self._continuous_session = False
        self._resume_wake()

    def _begin_continuous_recording(self) -> None:
        if self._busy or self._recording or self._closing:
            return
        self._recording = True
        self.recordingChanged.emit()
        self._set_status("连续对话 · 正在听你说话…")
        self._voice_executor.submit(self._continuous_recording_worker)

    def _continuous_recording_worker(self) -> None:
        try:
            self._wake_listener.pause(wait=True)
            self._recorder.start()
            wake = self.config.get("wake_word", {})
            heard = self._recorder.wait_for_utterance_end(
                silence_seconds=float(wake.get("silence_seconds", 1.0)),
                no_speech_timeout=float(self.config.get("conversation", {}).get("followup_timeout", 6.0)),
                max_seconds=float(wake.get("max_record_seconds", 30.0)),
            )
            if not heard:
                self._recorder.cancel()
                self._transcriptionFailed.emit("等待后续语音超时")
                return
            self._recording = False
            self.recordingChanged.emit()
            self._stop_and_transcribe()
        except Exception as exc:
            self._transcriptionFailed.emit(str(exc))

    @Slot(str)
    def ttsControl(self, action: str) -> None:
        if action == "pause":
            self._voice_executor.submit(self._tts.pause)
            self._set_status("播报已暂停")
        elif action == "resume":
            self._voice_executor.submit(self._tts.resume)
            self._set_status("继续播报")
        elif action == "stop":
            self._voice_executor.submit(self._tts.stop)
            self._speaking = False
            self.speakingChanged.emit()
            self._set_status("播报已停止")

    @Slot()
    def pauseAll(self) -> None:
        self._task_cancel_event.set()
        self._scheduled_cancel_event.set()
        if self._recorder.is_recording:
            self._recorder.cancel()
        self._recording = False
        self.recordingChanged.emit()
        self._voice_executor.submit(self._tts.stop)
        self._continuous_session = False
        self._set_status("已暂停全部操作")

    @Slot()
    def startServices(self) -> None:
        if self._startup_components:
            self._set_status("等待安装启动组件")
            return
        if self.preloadEnabled:
            self._set_preload_loading(True)
            self._set_status("正在预加载语音…")
            self._voice_executor.submit(self._warmup_services)
        else:
            self._start_wake_listener()
            self._set_status("就绪")

    def _warmup_services(self) -> None:
        try:
            self._tts.warmup()
            self._progressReady.emit("语音预加载完成")
        except Exception as exc:
            self._progressReady.emit(f"语音预加载失败：{exc}")
        finally:
            self._start_wake_listener()
            self._preloadStateReady.emit(False)

    @Slot(bool)
    def _set_preload_loading(self, loading: bool) -> None:
        if self._preload_loading != loading:
            self._preload_loading = loading
            self.audioSettingsChanged.emit()

    def _start_wake_listener(self) -> None:
        if self._wake_enabled and not self._closing:
            self._wake_listener.start()

    def _resume_wake(self) -> None:
        if self._wake_enabled and not self._closing:
            self._wake_listener.start()
            self._wake_listener.resume()

    @Slot()
    def _handle_wake(self) -> None:
        if not self._wake_enabled or self._closing:
            return
        self.showWindowRequested.emit()
        self.pauseAll()
        self._continuous_session = self.continuousEnabled
        self._set_status("已唤醒，正在听…")
        self._voice_executor.submit(self._wake_and_record)

    def _wake_and_record(self) -> None:
        try:
            responses = self.config.get("wake_word", {}).get("response_texts", ["我在呢。"])
            if self.ttsEnabled and responses:
                self._tts.speak(random.choice(list(responses)))
        except Exception:
            pass
        self._continuous_recording_worker()

    @Slot(str)
    def _set_wake_status(self, value: str) -> None:
        if self._wake_enabled and not self._busy and not self._recording:
            self._set_status(value)

    @Slot(bool)
    def setWakeEnabled(self, enabled: bool) -> None:
        self._wake_enabled = enabled
        self.config.setdefault("wake_word", {})["capture_enabled"] = enabled
        self.config.setdefault("skills", {})["wake-word"] = enabled
        save_local_settings({"wake_word": {"capture_enabled": enabled}, "skills": {"wake-word": enabled}})
        self._wake_listener.set_capture_enabled(enabled, wait=not enabled)
        if enabled:
            self._wake_listener.start()
        self.wakeSettingsChanged.emit()
        self.skillsChanged.emit()
        self._set_status("唤醒监听已开启" if enabled else "唤醒监听已关闭")

    @staticmethod
    def _parse_wake_words(value: str) -> list[str]:
        words: list[str] = []
        seen: set[str] = set()
        for raw in re.split(r"[,，;；\n]+", str(value)):
            word = re.sub(r"\s+", "", raw).strip()
            normalised = WakeWordListener._normalise(word)
            if not normalised:
                continue
            if len(normalised) > 12:
                raise ValueError(f"唤醒词“{word}”太长，请控制在 12 个字符以内。")
            if normalised not in seen:
                words.append(word)
                seen.add(normalised)
        if not words:
            raise ValueError("请至少输入一个唤醒词。")
        if len(words) > 5:
            raise ValueError("最多可以设置 5 个唤醒词。")
        return words

    @Slot(str)
    def saveWakeWords(self, value: str) -> None:
        if self._busy or self._recording:
            self._set_status("请等待当前任务或录音结束")
            return
        try:
            words = self._parse_wake_words(value)
        except ValueError as exc:
            self._set_status(str(exc))
            return
        self._wake_listener.close()
        self._wake_listener.update_keywords(words, clear_enrollment=True)
        self._wake_listener.set_capture_enabled(self._wake_enabled)
        wake = self.config.setdefault("wake_word", {})
        wake["keyword"] = words[0]
        wake["aliases"] = words
        save_local_settings({"wake_word": {"keyword": words[0], "aliases": words}})
        self._start_wake_listener()
        self.wakeSettingsChanged.emit()
        self._set_status("唤醒词已保存，请重新录制声纹样本")

    @Slot()
    def enrollWakeWords(self) -> None:
        if self._busy or self._recording:
            return
        self._set_busy(True, "准备录制唤醒词…")
        self._wake_listener.pause(wait=True)
        self._voice_executor.submit(self._enrollment_worker)

    def _enrollment_worker(self) -> None:
        import numpy as np

        samples: list[np.ndarray] = []
        try:
            words = list(self._wake_listener.aliases)
            per_word = max(3, min(8, int(self.config.get("wake_word", {}).get("enrollment_samples", 5))))
            recordings = [word for word in words for _ in range(per_word)]
            total = len(recordings)
            for index, word in enumerate(recordings, 1):
                self._progressReady.emit(f"请说“{word}” · 第 {index}/{total} 次")
                if sys.platform == "win32":
                    import winsound

                    winsound.Beep(880, 120)
                time.sleep(0.25)
                self._recorder.start()
                heard = self._recorder.wait_for_utterance_end(0.9, 4.5, 6.0)
                if not heard:
                    self._recorder.cancel()
                    raise RuntimeError(f"第 {index} 次没有听到清晰语音")
                path = self._recorder.stop()
                with wave.open(str(path), "rb") as source:
                    samples.append(np.frombuffer(source.readframes(source.getnframes()), dtype=np.int16).copy())
                path.unlink(missing_ok=True)
            threshold = self._wake_listener.enroll(samples)
            self._enrollmentFinished.emit(True, f"唤醒词录制完成 · 匹配阈值 {threshold:.2f}")
        except Exception as exc:
            self._recorder.cancel()
            self._enrollmentFinished.emit(False, f"唤醒词录制失败：{exc}")

    @Slot(bool, str)
    def _finish_enrollment(self, success: bool, message: str) -> None:
        self._set_busy(False, message)
        self.wakeSettingsChanged.emit()
        self._resume_wake()

    def _permission_key(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "open_url":
            domain = (urlparse(str(arguments.get("url") or "")).hostname or "").lower()
            return f"open_url:{domain}" if domain else ""
        if name in {"click_screen", "type_text"}:
            app = str(arguments.get("_target_app") or self._foreground_app()).lower()
            description = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(arguments.get("description") or "").lower())[:80]
            return f"{name}:{app}:{description}" if description else f"{name}:{app}"
        if name == "create_scheduled_task":
            command = re.sub(r"\s+", "", str(arguments.get("任务") or arguments.get("command") or ""))[:100]
            repeat = str(arguments.get("重复") or arguments.get("repeat") or "")
            return f"create_scheduled_task:{repeat}:{command}" if command else name
        if name == "add_app_to_allowlist":
            app = str(arguments.get("name") or arguments.get("应用") or "").strip().lower()
            return f"add_app_to_allowlist:{app}" if app else name
        return name

    @staticmethod
    def _permission_category(name: str) -> str:
        if name == "open_url":
            return "网站"
        if name in {"click_screen", "type_text", "add_app_to_allowlist"}:
            return "应用与窗口"
        if name == "create_scheduled_task":
            return "自动化"
        return "其他"

    def _permission_label(self, name: str, arguments: dict[str, Any], key: str) -> str:
        if name == "open_url":
            return urlparse(str(arguments.get("url") or "")).hostname or key
        if name in {"click_screen", "type_text"}:
            return f"{arguments.get('_target_app') or '当前应用'} · {arguments.get('description') or '屏幕操作'}"
        if name == "create_scheduled_task":
            return str(arguments.get("任务") or arguments.get("command") or "定时任务")
        if name == "add_app_to_allowlist":
            return str(arguments.get("name") or arguments.get("应用") or "添加应用")
        return key

    @staticmethod
    def _foreground_app() -> str:
        if sys.platform != "win32":
            return "unknown"
        try:
            import win32api
            import win32con
            import win32process

            hwnd = ctypes.windll.user32.GetForegroundWindow()
            _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
            process = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid)
            try:
                return Path(win32process.GetModuleFileNameEx(process, 0)).name.lower()
            finally:
                process.Close()
        except Exception:
            return "unknown"

    def _permission_records(self) -> list[dict[str, Any]]:
        permissions = self.config.setdefault("permissions", {})
        allowlist = [str(value) for value in permissions.get("allowlist", [])]
        records = [dict(value) for value in permissions.get("records", []) if isinstance(value, dict)]
        known = {str(value.get("key") or "") for value in records}
        for key in allowlist:
            if key not in known:
                tool = key.split(":", 1)[0]
                records.append(
                    {"key": key, "tool": tool, "category": self._permission_category(tool), "label": key, "created_at": "旧版授权"}
                )
        return records

    def _grant_permission(self, name: str, arguments: dict[str, Any], key: str) -> None:
        permissions = self.config.setdefault("permissions", {})
        allowlist = {str(value) for value in permissions.get("allowlist", [])}
        allowlist.add(key)
        records = [value for value in self._permission_records() if str(value.get("key")) != key]
        records.append(
            {
                "key": key,
                "tool": name,
                "category": self._permission_category(name),
                "label": self._permission_label(name, arguments, key),
                "created_at": time.strftime("%Y-%m-%d %H:%M"),
            }
        )
        permissions["allowlist"] = sorted(allowlist)
        permissions["records"] = records
        save_local_settings({"permissions": {"allowlist": permissions["allowlist"], "records": records}})
        self.permissionsChanged.emit()

    def _confirm_tool(self, name: str, arguments: dict[str, Any]) -> bool:
        if self._closing:
            return False
        key = self._permission_key(name, arguments)
        if key and key in set(map(str, self.config.get("permissions", {}).get("allowlist", []))):
            return True
        display = {k: v for k, v in arguments.items() if not str(k).startswith("_")}
        details = json.dumps(display, ensure_ascii=False, indent=2, default=str)
        if len(details) > 1200:
            details = details[:1200] + "\n…"
        high_risk = bool(HIGH_RISK_PATTERN.search(str(arguments.get("description") or "") + details))
        self._pending_confirmation = (name, arguments, key, high_risk)
        self._confirmation_result = False
        self._confirmation_remember = False
        self._confirmation_event.clear()
        self._wake_listener.pause()
        self.confirmationRequested.emit(self._permission_label(name, arguments, key), details, high_risk)
        self._voice_executor.submit(self._listen_for_spoken_confirmation)
        self._confirmation_event.wait()
        if self._confirmation_result and self._confirmation_remember and key and not high_risk:
            self._grant_permission(name, arguments, key)
        self._pending_confirmation = None
        self._resume_wake()
        return self._confirmation_result

    def _listen_for_spoken_confirmation(self) -> None:
        try:
            result = self._wake_listener.listen_for_confirmation(10.0, self._confirmation_event)
            if result is not None and not self._confirmation_event.is_set():
                self.resolveConfirmation(bool(result), bool(result))
        except Exception:
            return

    @Slot(bool, bool)
    def resolveConfirmation(self, approved: bool, remember: bool = False) -> None:
        self._confirmation_result = approved
        self._confirmation_remember = remember
        self._confirmation_event.set()

    def _managed_permissions(self) -> list[dict[str, Any]]:
        items = [dict(value) for value in self._permission_records()]
        for action in self._ui_database.list_visual_actions(limit=100):
            items.append(
                {
                    "key": f"visual:{action['id']}",
                    "category": "常用动作",
                    "label": f"{action['app_name']} · {action['description']}",
                    "created_at": str(action.get("created_at") or ""),
                }
            )
        allowlist = self.config.get("applications", {}).get("allowlist", {})
        if isinstance(allowlist, dict):
            for name in allowlist:
                items.append({"key": f"application:{name}", "category": "应用与窗口", "label": str(name), "created_at": "应用白名单"})
        return sorted(items, key=lambda item: (str(item.get("category")), str(item.get("label"))))

    @Slot(str)
    def deletePermission(self, key: str) -> None:
        if key.startswith("visual:"):
            self._ui_database.delete_visual_action(int(key.split(":", 1)[1]))
        elif key.startswith("application:"):
            name = key.split(":", 1)[1]
            allowlist = self.config.setdefault("applications", {}).get("allowlist", {})
            if isinstance(allowlist, dict):
                allowlist.pop(name, None)
                self.config["applications"]["allowlist"] = allowlist
                save_local_settings({"applications": {"allowlist": allowlist}})
        else:
            permissions = self.config.setdefault("permissions", {})
            permissions["allowlist"] = [value for value in permissions.get("allowlist", []) if str(value) != key]
            permissions["records"] = [value for value in self._permission_records() if str(value.get("key")) != key]
            save_local_settings({"permissions": {"allowlist": permissions["allowlist"], "records": permissions["records"]}})
        self.permissionsChanged.emit()

    @Slot()
    def clearConversation(self) -> None:
        if self._busy:
            return
        self._ui_database.clear_messages(self.session_id)
        self._messages.clear()
        self.messagesChanged.emit()

    @Slot(str)
    def setModelMode(self, value: str) -> None:
        if value not in MODEL_MODES or value == self.modelMode:
            return
        self.config.setdefault("routing", {})["model_mode"] = value
        save_local_settings({"routing": {"model_mode": value}})
        self.modelModeChanged.emit()

    @Slot(str)
    def setAsrMode(self, value: str) -> None:
        if value not in ASR_MODES:
            return
        self.config.setdefault("audio", {})["backend"] = value
        self._transcriber.set_backend(value)
        save_local_settings({"audio": {"backend": value}})
        self.audioSettingsChanged.emit()

    @Slot(str)
    def setTtsEngine(self, value: str) -> None:
        if value not in ENGINE_LABELS:
            return
        self.config.setdefault("tts", {})["backend"] = value
        save_local_settings({"tts": {"backend": value}})
        self._voice_executor.submit(self._tts.set_engine, value)
        self.audioSettingsChanged.emit()

    @Slot(str)
    def setVoice(self, value: str) -> None:
        key = "mimo_voice" if self.ttsEngine == "mimo-api" else "speaker"
        self.config.setdefault("tts", {})[key] = value
        save_local_settings({"tts": {key: value}})
        self._voice_executor.submit(self._tts.set_speaker, value)
        self.audioSettingsChanged.emit()

    @Slot(bool)
    def setTtsEnabled(self, enabled: bool) -> None:
        self.config.setdefault("tts", {})["enabled"] = enabled
        self._tts.enabled = enabled
        save_local_settings({"tts": {"enabled": enabled}})
        self.audioSettingsChanged.emit()

    @Slot(bool)
    def setPreloadEnabled(self, enabled: bool) -> None:
        self.config.setdefault("tts", {})["preload_on_startup"] = enabled
        save_local_settings({"tts": {"preload_on_startup": enabled}})
        self.audioSettingsChanged.emit()
        if enabled:
            self._set_preload_loading(True)
            self._voice_executor.submit(self._warmup_services)
        else:
            self._set_preload_loading(False)
            self._voice_executor.submit(self._tts.close)

    @Slot(bool)
    def setContinuousEnabled(self, enabled: bool) -> None:
        self.config.setdefault("conversation", {})["continuous"] = enabled
        self.config.setdefault("skills", {})["continuous-conversation"] = enabled
        save_local_settings({"conversation": {"continuous": enabled}, "skills": {"continuous-conversation": enabled}})
        if not enabled:
            self._continuous_session = False
        self.wakeSettingsChanged.emit()
        self.skillsChanged.emit()

    @Slot(str, bool)
    def setSkillEnabled(self, skill_id: str, enabled: bool) -> None:
        if not any(skill.id == skill_id for skill in SKILL_CATALOG):
            return
        if skill_id == "wake-word":
            self.setWakeEnabled(enabled)
            return
        if skill_id == "continuous-conversation":
            self.setContinuousEnabled(enabled)
            return
        self.config.setdefault("skills", {})[skill_id] = enabled
        save_local_settings({"skills": {skill_id: enabled}})
        self.skillsChanged.emit()

    @Slot(str, bool)
    def setSkillFavorite(self, skill_id: str, favorite: bool) -> None:
        if not any(skill.id == skill_id for skill in SKILL_CATALOG):
            return
        favorites = list(self.config.setdefault("ui", {}).get("favorite_skills", []))
        if favorite and skill_id not in favorites:
            favorites.append(skill_id)
        elif not favorite and skill_id in favorites:
            favorites.remove(skill_id)
        self.config["ui"]["favorite_skills"] = favorites
        save_local_settings({"ui": {"favorite_skills": favorites}})
        self.skillsChanged.emit()

    @Slot(bool)
    def setSkillSidebarExpanded(self, expanded: bool) -> None:
        self.config.setdefault("ui", {})["skill_sidebar_expanded"] = expanded
        save_local_settings({"ui": {"skill_sidebar_expanded": expanded}})
        self.uiSettingsChanged.emit()

    @Slot(bool)
    def setFavoriteSidebarExpanded(self, expanded: bool) -> None:
        self.config.setdefault("ui", {})["favorite_sidebar_expanded"] = expanded
        save_local_settings({"ui": {"favorite_sidebar_expanded": expanded}})
        self.uiSettingsChanged.emit()

    @Slot(str, str, bool, bool)
    def createSchedule(self, command: str, run_at: str, daily: bool, silent: bool) -> None:
        try:
            if not command.strip():
                raise ValueError("请填写任务内容")
            when = datetime.strptime(run_at.strip(), "%Y-%m-%d %H:%M")
            self._ui_database.create_scheduled_task(
                command.strip(),
                when,
                "daily" if daily else "once",
                silent=silent,
            )
            self.schedulesChanged.emit()
            self._set_status("定时任务已添加")
        except Exception as exc:
            self._set_status(f"无法添加定时任务：{exc}")

    @Slot(int)
    def cancelSchedule(self, task_id: int) -> None:
        self._scheduled_cancel_event.set()
        self._ui_database.cancel_scheduled_task(task_id)
        self.schedulesChanged.emit()

    @Slot(int)
    def deleteSchedule(self, task_id: int) -> None:
        self._ui_database.delete_scheduled_task(task_id)
        self.schedulesChanged.emit()

    @Slot()
    def _poll_schedules(self) -> None:
        if self._closing or self._busy or self._recording or not is_skill_enabled(self.config, "scheduled-tasks"):
            return
        due = self._ui_database.due_scheduled_tasks(limit=1)
        if not due:
            return
        task = self._ui_database.claim_scheduled_task(int(due[0]["id"]))
        if task is None:
            return
        self._scheduled_cancel_event = threading.Event()
        self.schedulesChanged.emit()
        self._schedule_executor.submit(self._run_schedule, task, self._scheduled_cancel_event)

    def _run_schedule(self, task: dict[str, Any], cancel_event: threading.Event) -> None:
        task_id = int(task["id"])
        agent = database = None
        try:
            agent, database, _tts = build_agent(
                self.config,
                f"scheduled-{task_id}",
                confirmation_callback=lambda _name, _arguments: False,
                cancel_event=cancel_event,
                include_tts=False,
            )
            answer = agent.run(str(task["command"]), input_mode="text")
            result = answer.text.strip() or "操作已完成"
            success = not bool(re.search(r"失败|无法|未找到|已取消|拒绝|错误|没有执行", result))
            self._scheduleFinished.emit(task_id, success, result, bool(task["silent"]))
        except Exception as exc:
            self._scheduleFinished.emit(task_id, False, str(exc), bool(task["silent"]))
        finally:
            if agent is not None:
                agent.close()
            if database is not None:
                database.close()

    @Slot(int, bool, str, bool)
    def _finish_schedule(self, task_id: int, success: bool, result: str, silent: bool) -> None:
        self._ui_database.complete_scheduled_task(task_id, success, result)
        self.schedulesChanged.emit()
        if not silent:
            self._append_message("system", f"定时任务 #{task_id}：{result}")
            self.notificationRequested.emit("定时任务完成", result)

    @Slot(str, str)
    def saveApiKeys(self, kimi: str, mimo: str) -> None:
        try:
            save_api_keys(app_paths().key_file, kimi_key=kimi.strip() or None, mimo_key=mimo.strip() or None)
            self.keyStatusChanged.emit()
            self._set_status("API 密钥已保存")
        except Exception as exc:
            self._set_status(f"API 密钥保存失败：{exc}")

    @Slot(str)
    def setStarrailExecutable(self, value: str) -> None:
        url = QUrl(value)
        path = url.toLocalFile() if url.isLocalFile() else value
        path = str(Path(path).resolve()) if path else ""
        automation = self.config.setdefault("automation", {}).setdefault("starrail_dailies", {})
        automation["executable"] = path
        save_local_settings({"automation": {"starrail_dailies": automation}})
        self.integrationStatusChanged.emit()
        self._set_status("星铁日常程序路径已保存")

    @Slot()
    def launchXiaomiSetup(self) -> None:
        self._set_status("正在打开小米设备登录工具…")
        self._schedule_executor.submit(self._xiaomi_setup_worker)

    @Slot()
    def refreshIntegrations(self) -> None:
        self.integrationStatusChanged.emit()

    @Slot()
    def openStarrailProject(self) -> None:
        webbrowser.open("https://github.com/moesnow/March7thAssistant", new=0)

    def _xiaomi_setup_worker(self) -> None:
        try:
            subprocess.run(
                [sys.executable, "-m", "assistant_app.adapters.xiaomi_token_setup"],
                cwd=str(app_paths().root),
                check=True,
            )
            self._progressReady.emit("小米设备设置已完成")
        except Exception as exc:
            self._progressReady.emit(f"小米设备设置失败：{exc}")
        finally:
            self.integrationStatusChanged.emit()

    @Slot()
    def installStartupComponents(self) -> None:
        if not self._startup_components:
            return
        self._startup_message = "正在下载启动组件…"
        self.startupChanged.emit()
        self._schedule_executor.submit(self._install_components_worker)

    def _install_components_worker(self) -> None:
        try:
            total = len(self._startup_components)
            for index, component_id in enumerate(list(self._startup_components)):
                component = COMPONENTS_BY_ID[component_id]
                self._startupProgressReady.emit(int(index * 100 / total), f"正在安装：{component.name}")
                install_component(
                    component_id,
                    progress=lambda value, offset=index: self._startupProgressReady.emit(
                        int((offset * 100 + value) / total), f"正在安装：{component.name}"
                    ),
                )
            self._startup_components.clear()
            self._startupProgressReady.emit(100, "启动组件安装完成")
        except Exception as exc:
            self._startupProgressReady.emit(self._startup_progress, f"组件安装失败：{exc}")

    @Slot(int, str)
    def _set_startup_progress(self, value: int, message: str) -> None:
        self._startup_progress = value
        self._startup_message = message
        self.startupChanged.emit()
        if value >= 100 and not self._startup_components:
            self.startServices()

    @Slot()
    def refreshUsage(self) -> None:
        currency = str(self.config.get("api", {}).get("currency", "CNY"))
        symbol = str(self.config.get("api", {}).get("currency_symbol", "￥"))
        usage = self._ui_database.usage_summary(currency)
        value = f"今日 {symbol}{usage['today']:.2f} · 本月 {symbol}{usage['month']:.2f}"
        if value != self._usage:
            self._usage = value
            self.usageChanged.emit()

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._schedule_timer.stop()
        self._task_cancel_event.set()
        self._scheduled_cancel_event.set()
        self._confirmation_event.set()
        self._asr_fallback_event.set()
        self._recorder.cancel()
        self._wake_listener.close()

        def close_runtime() -> None:
            if self._runtime is None:
                return
            agent, database, _tts = self._runtime
            agent.close()
            database.close()

        try:
            self._executor.submit(close_runtime).result(timeout=10)
            self._voice_executor.submit(self._tts.close).result(timeout=10)
        finally:
            self._transcriber.close()
            self._ui_database.close()
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._voice_executor.shutdown(wait=True, cancel_futures=True)
            self._schedule_executor.shutdown(wait=True, cancel_futures=True)
