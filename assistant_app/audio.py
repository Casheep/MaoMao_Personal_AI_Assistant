from __future__ import annotations

import tempfile
import threading
import wave
import os
import sys
import gc
import base64
import json
import queue
import re
import time
from collections import deque
from functools import lru_cache
from heapq import nsmallest
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    import numpy as np
else:
    class _LazyNumpy:
        """Load NumPy only when an audio operation actually needs it."""

        def __getattr__(self, name: str) -> Any:
            import numpy as numpy_module

            globals()["np"] = numpy_module
            return getattr(numpy_module, name)

    np = _LazyNumpy()

from .secrets import load_mimo_key, redact_secret


_CUDA_DLL_HANDLES: list[object] = []
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class APIReconnectFailed(RuntimeError):
    """API failed again after its stale connection was rebuilt."""


def _sounddevice():
    """Import PortAudio bindings only when capture or playback starts."""
    import sounddevice

    return sounddevice


@lru_cache(maxsize=4)
def _mfcc_geometry(sample_rate: int, frame_length: int) -> tuple[np.ndarray, np.ndarray]:
    """Cache the fixed window and Mel filter bank used by wake-word scoring."""
    window = np.hanning(frame_length).astype(np.float32)
    mel = lambda hz: 2595.0 * np.log10(1.0 + hz / 700.0)
    hz = lambda value: 700.0 * (10 ** (value / 2595.0) - 1.0)
    points = hz(np.linspace(mel(40.0), mel(sample_rate / 2), 28))
    bins = np.floor((512 + 1) * points / sample_rate).astype(int)
    filters = np.zeros((26, 257), dtype=np.float32)
    for index in range(26):
        left, centre, right = bins[index : index + 3]
        rising = np.arange(left, max(left + 1, centre))
        filters[index, rising] = (rising - left) / max(1, centre - left)
        falling = np.arange(centre, max(centre + 1, right))
        filters[index, falling] = (right - falling) / max(1, right - centre)
    return window, filters


def _configure_cuda_dlls() -> None:
    if sys.platform != "win32" or _CUDA_DLL_HANDLES:
        return
    site_packages = Path(sys.prefix) / "Lib" / "site-packages"
    cuda_directories: list[str] = []
    for relative in ("nvidia/cublas/bin", "nvidia/cudnn/bin"):
        directory = site_packages / relative
        if directory.is_dir():
            value = str(directory)
            cuda_directories.append(value)
            _CUDA_DLL_HANDLES.append(os.add_dll_directory(value))
    if cuda_directories:
        os.environ["PATH"] = os.pathsep.join(cuda_directories + [os.environ.get("PATH", "")])


def play_wav_file(path: Path) -> None:
    """Play a small PCM voice clip synchronously."""
    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        sample_rate = source.getframerate()
        frames = source.readframes(source.getnframes())
    if sample_width != 2:
        raise RuntimeError("语音文件必须是 16-bit PCM WAV。")
    audio = np.frombuffer(frames, dtype=np.int16).reshape(-1, channels)
    if not audio.size or int(np.max(np.abs(audio.astype(np.int32)))) < 16:
        raise RuntimeError("语音文件没有有效声音。")
    if sys.platform == "win32":
        import winsound

        winsound.PlaySound(str(path), winsound.SND_FILENAME)
        return
    _sounddevice().play(audio, sample_rate, blocking=True)


class AudioRecorder:
    def __init__(self, sample_rate: int = 16000, channels: int = 1) -> None:
        self.sample_rate = sample_rate
        self.channels = channels

    def record_until_enter(self) -> Path:
        chunks: list[np.ndarray] = []
        stopped = threading.Event()

        def callback(indata: np.ndarray, frames: int, time_info: object, status: object) -> None:
            del frames, time_info
            if status:
                print(f"\n[录音设备提示] {status}")
            if not stopped.is_set():
                chunks.append(indata.copy())

        print("正在录音，按 Enter 停止……")
        with _sounddevice().InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            callback=callback,
        ):
            input()
            stopped.set()
        if not chunks:
            raise RuntimeError("没有录到音频。")
        audio = np.concatenate(chunks, axis=0)
        target = Path(tempfile.gettempdir()) / "personal-assistant-recording.wav"
        with wave.open(str(target), "wb") as output:
            output.setnchannels(self.channels)
            output.setsampwidth(2)
            output.setframerate(self.sample_rate)
            output.writeframes(audio.tobytes())
        return target


class ButtonAudioRecorder:
    """Non-blocking recorder for a GUI start/stop button."""

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        silence_threshold: float = 420.0,
        speech_start_seconds: float = 0.30,
        adaptive_noise_multiplier: float = 2.2,
        adaptive_noise_offset: float = 80.0,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.silence_threshold = silence_threshold
        self.speech_start_seconds = max(0.12, speech_start_seconds)
        self.adaptive_noise_multiplier = max(1.2, adaptive_noise_multiplier)
        self.adaptive_noise_offset = max(0.0, adaptive_noise_offset)
        self._chunks: list[np.ndarray] = []
        self._stream: Any = None
        self._lock = threading.Lock()
        self._speech_started = threading.Event()
        self._recording_started_at = 0.0
        self._last_voice_at = 0.0
        self._candidate_voice_seconds = 0.0
        self._continuation_voice_seconds = 0.0
        self._noise_floor = max(1.0, silence_threshold / 4.0)
        self._dynamic_threshold = silence_threshold
        self._recent_levels: deque[tuple[float, float]] = deque()
        self._recent_level_seconds = 0.0

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            raise RuntimeError("已经在录音。")
        with self._lock:
            self._chunks = []
        self._speech_started.clear()
        self._recording_started_at = time.monotonic()
        self._last_voice_at = 0.0
        self._candidate_voice_seconds = 0.0
        self._continuation_voice_seconds = 0.0
        self._noise_floor = max(1.0, self.silence_threshold / 4.0)
        self._dynamic_threshold = self.silence_threshold
        self._recent_levels.clear()
        self._recent_level_seconds = 0.0

        def callback(indata: np.ndarray, frames: int, time_info: object, status: object) -> None:
            del time_info, status
            values = indata.astype(np.float32)
            rms = float(np.sqrt(np.mean(values * values) + 1.0))
            self._process_audio_level(
                rms,
                max(0.001, frames / float(self.sample_rate)),
                time.monotonic(),
            )
            with self._lock:
                self._chunks.append(indata.copy())

        stream = _sounddevice().InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            callback=callback,
        )
        stream.start()
        self._stream = stream

    def _process_audio_level(
        self,
        rms: float,
        frame_seconds: float,
        now: float,
    ) -> None:
        """Adaptive onset gate: ignore steady noise and short transient sounds."""
        if not self._speech_started.is_set():
            # Learn only low-to-moderate background levels.  Normal nearby
            # speech is usually well above 1.25x the base threshold and must
            # not be mistaken for the room noise floor.
            if rms < self.silence_threshold * 1.25:
                self._noise_floor = self._noise_floor * 0.82 + rms * 0.18
            self._dynamic_threshold = max(
                self.silence_threshold,
                self._noise_floor * self.adaptive_noise_multiplier
                + self.adaptive_noise_offset,
            )
            if rms >= self._dynamic_threshold:
                self._candidate_voice_seconds += frame_seconds
                if self._candidate_voice_seconds >= self.speech_start_seconds:
                    self._speech_started.set()
                    self._last_voice_at = now
            else:
                self._candidate_voice_seconds = 0.0
            return

        # Do not let low, steady room noise keep a finished utterance alive.
        # The previous 0.62 multiplier was intentionally permissive, but it
        # also treated fans and distant audio as continuous speech.  Require a
        # short sustained signal close to the learned speech-onset level.
        # Re-estimate the post-speech room floor from a short rolling window.
        # This handles a fan, air conditioner or computer audio that begins
        # after speech onset: a steady sound must not refresh _last_voice_at
        # forever. The lower quartile remains close to the room floor during
        # normal speech because syllables naturally contain quieter gaps.
        self._recent_levels.append((rms, frame_seconds))
        self._recent_level_seconds += frame_seconds
        while self._recent_levels and self._recent_level_seconds > 1.25:
            _old_level, old_seconds = self._recent_levels.popleft()
            self._recent_level_seconds -= old_seconds

        adaptive_continuation = 0.0
        if self._recent_level_seconds >= 0.65:
            recent_floor = float(
                np.percentile([level for level, _seconds in self._recent_levels], 25)
            )
            adaptive_continuation = (
                recent_floor * 1.38 + self.adaptive_noise_offset * 0.5
            )

        continuation_threshold = max(
            self.silence_threshold * 1.05,
            self._dynamic_threshold * 0.82,
            adaptive_continuation,
        )
        if rms >= continuation_threshold:
            self._continuation_voice_seconds += frame_seconds
            if self._continuation_voice_seconds >= 0.08:
                self._last_voice_at = now
        else:
            self._continuation_voice_seconds = 0.0

    def wait_for_utterance_end(
        self,
        silence_seconds: float = 1.15,
        no_speech_timeout: float = 6.0,
        max_seconds: float = 30.0,
    ) -> bool:
        """Wait for speech followed by silence; return whether speech was heard."""
        while self.is_recording:
            now = time.monotonic()
            if self._speech_started.is_set() and now - self._last_voice_at >= silence_seconds:
                return True
            elapsed = now - self._recording_started_at
            if not self._speech_started.is_set() and elapsed >= no_speech_timeout:
                return False
            if elapsed >= max_seconds:
                return self._speech_started.is_set()
            time.sleep(0.05)
        return False

    def stop(self) -> Path:
        stream, self._stream = self._stream, None
        if stream is None:
            raise RuntimeError("当前没有在录音。")
        try:
            stream.stop()
        finally:
            stream.close()
        with self._lock:
            chunks = list(self._chunks)
            self._chunks = []
        if not chunks:
            raise RuntimeError("没有录到音频。")
        audio = np.concatenate(chunks, axis=0)
        target = Path(tempfile.gettempdir()) / f"personal-assistant-{threading.get_ident()}.wav"
        with wave.open(str(target), "wb") as output:
            output.setnchannels(self.channels)
            output.setsampwidth(2)
            output.setframerate(self.sample_rate)
            output.writeframes(audio.tobytes())
        return target

    def cancel(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.abort()
            finally:
                stream.close()
        with self._lock:
            self._chunks = []


class WakeWordListener:
    """Low-resource CPU wake-word listener backed by a restricted Vosk grammar."""

    def __init__(
        self,
        config: dict,
        on_wake: Callable[[], None],
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.enabled = bool(config.get("enabled", True))
        self.keyword = str(config.get("keyword", "猫猫"))
        aliases = config.get("aliases", [self.keyword, "喵喵"])
        self.aliases = [str(value) for value in aliases]
        self._normalised_aliases = self._normalise_aliases(self.aliases)
        model_path = Path(str(config.get("model_path", "engines/vosk/vosk-model-small-cn-0.22")))
        project_root = Path(__file__).resolve().parent.parent
        self.model_path = model_path if model_path.is_absolute() else project_root / model_path
        self.sample_rate = int(config.get("sample_rate", 16000))
        self.block_size = int(config.get("block_size", 4000))
        self.cooldown_seconds = float(config.get("cooldown_seconds", 2.0))
        self.min_confidence = float(config.get("min_confidence", 0.86))
        self.partial_hits_required = max(1, int(config.get("partial_hits_required", 1)))
        self.partial_voice_multiplier = min(
            1.15, max(0.90, float(config.get("partial_voice_multiplier", 1.05)))
        )
        self.fragment_hits_required = max(2, int(config.get("fragment_hits_required", 2)))
        self.fragment_voice_multiplier = min(
            1.10, max(0.85, float(config.get("fragment_voice_multiplier", 1.0)))
        )
        self.require_voice_match = bool(config.get("require_voice_match", True))
        template_path = Path(str(config.get("template_path", "voices/wake-templates.npz")))
        self.template_path = template_path if template_path.is_absolute() else project_root / template_path
        self.verifier = WakeVoiceVerifier(self.template_path)
        self.on_wake = on_wake
        self.on_status = on_status
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._capture_enabled = threading.Event()
        if bool(config.get("capture_enabled", True)):
            self._capture_enabled.set()
        self._stream_idle = threading.Event()
        self._stream_idle.set()
        self._thread: threading.Thread | None = None
        self._last_wake_at = 0.0
        self._model = None

    def start(self) -> None:
        if not self.enabled or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wake-word-listener", daemon=True)
        self._thread.start()

    def pause(self, wait: bool = False) -> None:
        self._paused.set()
        if wait:
            self._stream_idle.wait(timeout=1.5)

    def resume(self) -> None:
        if not self._stop.is_set():
            self._paused.clear()

    @property
    def capture_enabled(self) -> bool:
        return self._capture_enabled.is_set()

    def set_capture_enabled(self, enabled: bool, wait: bool = False) -> None:
        """Enable or suspend keyword capture independently of temporary pauses."""
        if enabled:
            self._capture_enabled.set()
        else:
            self._capture_enabled.clear()
            if wait:
                self._stream_idle.wait(timeout=1.5)

    def close(self) -> None:
        self._stop.set()
        self._paused.clear()
        self._capture_enabled.set()
        self._stream_idle.wait(timeout=1.5)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
            if not thread.is_alive():
                self._thread = None

    @property
    def enrolled(self) -> bool:
        return self.verifier.enrolled

    def enroll(self, samples: list[np.ndarray]) -> float:
        return self.verifier.enroll(samples, self.sample_rate)

    def update_keywords(self, phrases: list[str], clear_enrollment: bool = True) -> None:
        """Replace the restricted Vosk grammar before restarting the listener."""
        values = [str(value).strip() for value in phrases if str(value).strip()]
        if not values:
            raise ValueError("至少需要一个唤醒词。")
        self.keyword = values[0]
        self.aliases = list(dict.fromkeys(values))
        self._normalised_aliases = self._normalise_aliases(self.aliases)
        if clear_enrollment:
            self.verifier.clear()

    @staticmethod
    def _normalise(value: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff]", "", value).lower()

    @classmethod
    def _normalise_aliases(cls, aliases: list[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(cls._normalise(alias) for alias in aliases))

    def _matches(self, value: str) -> bool:
        normalised = self._normalise(value)
        return normalised in self._normalised_aliases

    def _keyword_hint(self, value: str) -> bool:
        """Accept an exact keyword or one half of a repeated-character keyword."""
        normalised = self._normalise(value)
        if self._matches(normalised):
            return True
        return any(
            len(alias_value) == 2
            and alias_value[0] == alias_value[1]
            and normalised == alias_value[0]
            for alias_value in self._normalised_aliases
        )

    def _grammar_phrases(self) -> list[str]:
        """Include the tokenized form preferred by Vosk's small Chinese model."""
        phrases = list(self.aliases)
        for alias in self.aliases:
            normalised = self._normalise(alias)
            if len(normalised) == 2 and normalised[0] == normalised[1]:
                phrases.append(f"{normalised[0]} {normalised[1]}")
        return list(dict.fromkeys(phrases))

    def _result_matches(self, result: dict) -> bool:
        if not self._matches(str(result.get("text", ""))):
            return False
        words = result.get("result") or []
        confidences = [float(word.get("conf", 0.0)) for word in words if "conf" in word]
        return bool(confidences) and sum(confidences) / len(confidences) >= self.min_confidence

    def _voice_and_keyword_match(
        self, voice_score: float, text_matches: bool, text_hint: bool
    ) -> bool:
        if not self.require_voice_match:
            return text_matches
        # A voice-template similarity alone must never wake the assistant.  It
        # only verifies an utterance that Vosk has already recognised as the
        # configured keyword.
        if not text_hint:
            return False
        multiplier = 1.05 if text_matches else 0.95
        return voice_score <= min(0.75, self.verifier.threshold * multiplier)

    def _partial_keyword_match(
        self, value: str, hits: int, voice_score: float
    ) -> bool:
        """Allow one stable partial keyword while retaining speaker verification."""
        exact = self._matches(value)
        if not exact and not self._keyword_hint(value):
            return False
        required_hits = self.partial_hits_required if exact else self.fragment_hits_required
        if hits < required_hits:
            return False
        if not self.require_voice_match:
            return True
        multiplier = (
            self.partial_voice_multiplier if exact else self.fragment_voice_multiplier
        )
        limit = min(0.75, self.verifier.threshold * multiplier)
        return voice_score <= limit

    def _notify(self, status: str) -> None:
        if self.on_status is not None:
            self.on_status(status)

    @staticmethod
    def _confirmation_decision(text: str) -> bool | None:
        compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(text).lower())
        if any(value in compact for value in ("不用", "不要", "不可以", "否", "取消", "no")):
            return False
        if any(value in compact for value in ("是", "可以", "好的", "好", "用吧", "yes", "ok")):
            return True
        return None

    def listen_for_confirmation(
        self,
        timeout_seconds: float = 8.0,
        cancel_event: threading.Event | None = None,
    ) -> bool | None:
        """Listen for a tiny yes/no grammar using Vosk only; never touches GPU."""
        model = self._model
        if model is None or self._stop.is_set():
            return None
        from vosk import KaldiRecognizer

        phrases = [
            "是", "可以", "也可以", "好", "好的", "用吧",
            "不用", "不要", "不可以", "否", "取消", "yes", "no", "ok",
        ]
        recognizer = KaldiRecognizer(
            model,
            self.sample_rate,
            json.dumps([*phrases, "[unk]"], ensure_ascii=False),
        )
        audio_queue: queue.Queue[bytes] = queue.Queue()

        def callback(indata, frames, time_info, status) -> None:
            del frames, time_info, status
            audio_queue.put(bytes(indata))

        deadline = time.monotonic() + max(2.0, float(timeout_seconds))
        try:
            with _sounddevice().RawInputStream(
                samplerate=self.sample_rate,
                blocksize=self.block_size,
                channels=1,
                dtype="int16",
                callback=callback,
            ):
                while (
                    time.monotonic() < deadline
                    and not self._stop.is_set()
                    and not (cancel_event is not None and cancel_event.is_set())
                ):
                    try:
                        data = audio_queue.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if recognizer.AcceptWaveform(data):
                        value = str(json.loads(recognizer.Result()).get("text", ""))
                    else:
                        value = str(json.loads(recognizer.PartialResult()).get("partial", ""))
                    decision = self._confirmation_decision(value)
                    if decision is not None:
                        return decision
        except Exception:
            return None
        if cancel_event is not None and cancel_event.is_set():
            return None
        final_text = str(json.loads(recognizer.FinalResult()).get("text", ""))
        return self._confirmation_decision(final_text)

    def _run(self) -> None:
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel

            if not self.model_path.is_dir():
                raise RuntimeError(f"唤醒模型不存在：{self.model_path}")
            SetLogLevel(-1)
            self._notify("正在加载唤醒词…")
            model = Model(str(self.model_path))
            self._model = model
            grammar = json.dumps([*self._grammar_phrases(), "[unk]"], ensure_ascii=False)
            recognizer = KaldiRecognizer(model, self.sample_rate, grammar)
            recognizer.SetWords(True)
            self.verifier.load()
            if not self.capture_enabled:
                self._notify("唤醒监听已关闭")
            elif self.require_voice_match and not self.verifier.enrolled:
                self._notify("请先录制唤醒词")
            else:
                self._notify(f"正在监听“{'、'.join(self.aliases)}”")
        except Exception as exc:
            self._notify(f"唤醒词不可用：{exc}")
            return

        while not self._stop.is_set():
            if self._paused.is_set() or not self.capture_enabled:
                self._stop.wait(timeout=0.05)
                continue
            audio_queue: queue.Queue[bytes] = queue.Queue()
            utterance = bytearray()
            partial_hits = 0

            def callback(indata, frames, time_info, status) -> None:
                del frames, time_info, status
                if (
                    not self._paused.is_set()
                    and self.capture_enabled
                    and not self._stop.is_set()
                ):
                    audio_queue.put(bytes(indata))

            try:
                self._stream_idle.clear()
                with _sounddevice().RawInputStream(
                    samplerate=self.sample_rate,
                    blocksize=self.block_size,
                    channels=1,
                    dtype="int16",
                    callback=callback,
                ):
                    while (
                        not self._stop.is_set()
                        and not self._paused.is_set()
                        and self.capture_enabled
                    ):
                        try:
                            data = audio_queue.get(timeout=0.1)
                        except queue.Empty:
                            continue
                        utterance.extend(data)
                        maximum = self.sample_rate * 2 * 5
                        if len(utterance) > maximum:
                            del utterance[: len(utterance) - maximum]
                        if not recognizer.AcceptWaveform(data):
                            partial = json.loads(recognizer.PartialResult())
                            partial_text = str(partial.get("partial", ""))
                            if not self._keyword_hint(partial_text):
                                partial_hits = 0
                                continue
                            partial_hits += 1
                            now = time.monotonic()
                            voice_score = self.verifier.score(bytes(utterance), self.sample_rate)
                            if (
                                self._partial_keyword_match(
                                    partial_text,
                                    partial_hits,
                                    voice_score,
                                )
                                and self.capture_enabled
                                and now - self._last_wake_at >= self.cooldown_seconds
                            ):
                                self._last_wake_at = now
                                self._paused.set()
                                recognizer.Reset()
                                self.on_wake()
                                break
                            continue
                        result = json.loads(recognizer.Result())
                        now = time.monotonic()
                        text_hint = self._keyword_hint(str(result.get("text", "")))
                        text_matches = self._result_matches(result)
                        voice_score = self.verifier.score(bytes(utterance), self.sample_rate)
                        wake_matches = self._voice_and_keyword_match(
                            voice_score, text_matches, text_hint
                        )
                        partial_hits = 0
                        utterance.clear()
                        if (
                            wake_matches
                            and self.capture_enabled
                            and now - self._last_wake_at >= self.cooldown_seconds
                        ):
                            self._last_wake_at = now
                            self._paused.set()
                            recognizer.Reset()
                            self.on_wake()
                            break
            except Exception as exc:
                if not self._stop.is_set():
                    self._notify(f"唤醒监听异常，正在重试：{exc}")
                    time.sleep(1.0)
            finally:
                self._stream_idle.set()


class WakeVoiceVerifier:
    """Small MFCC/DTW voice template verifier; all data stays on this computer."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.templates: list[np.ndarray] = []
        self.threshold = 0.38
        self._loaded = False

    @property
    def enrolled(self) -> bool:
        if not self._loaded:
            return self.path.is_file()
        with self._lock:
            return len(self.templates) >= 3

    def clear(self) -> None:
        """Discard phrase-specific templates after wake words are changed."""
        with self._lock:
            self.templates = []
            self.threshold = 0.38
            self._loaded = True
        self.path.unlink(missing_ok=True)

    def load(self) -> None:
        """Load saved voice templates on first background use."""
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            if not self.path.is_file():
                return
            try:
                with np.load(self.path, allow_pickle=False) as values:
                    templates = [
                        values[key].astype(np.float32)
                        for key in sorted(values.files)
                        if key.startswith("template_")
                    ]
                    threshold = float(values["threshold"][0])
                if len(templates) >= 3:
                    self.templates = templates
                    self.threshold = min(0.68, max(0.35, threshold))
            except Exception:
                self.templates = []

    @staticmethod
    def _features(samples: np.ndarray, sample_rate: int) -> np.ndarray:
        from scipy.fft import dct

        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        if not audio.size:
            raise ValueError("录音为空。")
        frame_length = max(1, int(sample_rate * 0.025))
        hop = max(1, int(sample_rate * 0.010))
        energy_audio = (
            audio
            if len(audio) >= frame_length
            else np.pad(audio, (0, frame_length - len(audio)))
        )
        energy_windows = np.lib.stride_tricks.sliding_window_view(
            energy_audio, frame_length
        )[::hop]
        energies = np.sqrt(np.mean(energy_windows * energy_windows, axis=1) + 1.0)
        active = np.flatnonzero(energies >= max(220.0, float(energies.max()) * 0.10))
        if not active.size:
            raise ValueError("没有检测到清晰语音。")
        start = max(0, int(active[0] * hop - sample_rate * 0.08))
        end = min(len(audio), int(active[-1] * hop + frame_length + sample_rate * 0.10))
        audio = audio[start:end] / 32768.0
        if len(audio) < frame_length:
            audio = np.pad(audio, (0, frame_length - len(audio)))
        frames = np.lib.stride_tricks.sliding_window_view(audio, frame_length)[::hop].copy()
        window, filters = _mfcc_geometry(sample_rate, frame_length)
        frames *= window
        spectrum = np.abs(np.fft.rfft(frames, n=512)) ** 2
        log_mel = np.log(np.maximum(spectrum @ filters.T, 1e-10))
        mfcc = dct(log_mel, type=2, axis=1, norm="ortho")[:, :13]
        mfcc -= mfcc.mean(axis=0, keepdims=True)
        mfcc /= mfcc.std(axis=0, keepdims=True) + 1e-5
        delta = np.gradient(mfcc, axis=0)
        return np.concatenate([mfcc, delta], axis=1).astype(np.float32)

    @staticmethod
    def _distance(first: np.ndarray, second: np.ndarray) -> float:
        from scipy.spatial.distance import cdist

        costs = cdist(first, second, metric="cosine")
        rows, columns = costs.shape
        previous = np.full(columns + 1, np.inf, dtype=np.float32)
        current = np.full(columns + 1, np.inf, dtype=np.float32)
        previous_steps = np.zeros(columns + 1, dtype=np.int32)
        current_steps = np.zeros(columns + 1, dtype=np.int32)
        previous[0] = 0.0
        for row in range(rows):
            current.fill(np.inf)
            current_steps.fill(0)
            row_costs = costs[row]
            for column in range(1, columns + 1):
                up = previous[column]
                left = current[column - 1]
                diagonal = previous[column - 1]
                if diagonal <= up and diagonal <= left:
                    best_total = diagonal
                    best_steps = previous_steps[column - 1]
                elif up <= left:
                    best_total = up
                    best_steps = previous_steps[column]
                else:
                    best_total = left
                    best_steps = current_steps[column - 1]
                current[column] = best_total + row_costs[column - 1]
                current_steps[column] = best_steps + 1
            previous, current = current, previous
            previous_steps, current_steps = current_steps, previous_steps
        return float(previous[columns] / max(1, previous_steps[columns]))

    @classmethod
    def _calibrated_threshold(cls, templates: list[np.ndarray]) -> float:
        leave_one_out: list[float] = []
        for index, template in enumerate(templates):
            scores = sorted(
                cls._distance(template, other)
                for other_index, other in enumerate(templates)
                if other_index != index
            )
            leave_one_out.append(float(np.mean(scores[:2])))
        # With five recordings, use a robust percentile instead of the worst
        # sample so one cough/noisy take cannot make waking overly permissive.
        reference = (
            float(np.percentile(leave_one_out, 80))
            if len(leave_one_out) >= 5
            else max(leave_one_out)
        )
        return min(0.68, max(0.35, reference * 1.12))

    def enroll(self, samples: list[np.ndarray], sample_rate: int) -> float:
        if len(samples) < 3:
            raise ValueError("至少需要录制 3 次唤醒词。")
        templates = [self._features(sample, sample_rate) for sample in samples]
        threshold = self._calibrated_threshold(templates)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {f"template_{index}": value for index, value in enumerate(templates)}
        payload["threshold"] = np.array([threshold], dtype=np.float32)
        np.savez_compressed(self.path, **payload)
        with self._lock:
            self.templates = templates
            self.threshold = threshold
            self._loaded = True
        return threshold

    def score(self, audio_bytes: bytes, sample_rate: int) -> float:
        self.load()
        with self._lock:
            templates = list(self.templates)
        if len(templates) < 3:
            return float("inf")
        try:
            samples = np.frombuffer(audio_bytes, dtype=np.int16)
            # Only compare the most recent speech. A longer buffer often contains
            # the assistant's own playback and used to hide a correctly spoken wake word.
            samples = samples[-int(sample_rate * 2.5) :]
            candidate = self._features(samples, sample_rate)
        except ValueError:
            return float("inf")
        scores = nsmallest(2, (self._distance(candidate, template) for template in templates))
        return float(np.mean(scores))

    def verify(self, audio_bytes: bytes, sample_rate: int) -> bool:
        return self.score(audio_bytes, sample_rate) <= self.threshold


class WhisperTranscriber:
    def __init__(self, config: dict) -> None:
        self.config = config
        self._model = None
        self._model_lock = threading.Lock()

    def _load(self) -> None:
        if self._model is not None:
            return
        with self._model_lock:
            if self._model is not None:
                return
            _configure_cuda_dlls()
            from faster_whisper import WhisperModel

            print(f"首次加载语音模型 {self.config['whisper_model']}，可能需要下载模型文件……")
            self._model = WhisperModel(
                self.config["whisper_model"],
                device=self.config["device"],
                compute_type=self.config["compute_type"],
            )

    def warmup(self) -> None:
        self._load()

    def close(self) -> None:
        with self._model_lock:
            self._model = None
        gc.collect()

    def transcribe(self, path: Path) -> str:
        self._load()
        segments, _ = self._model.transcribe(
            str(path),
            language=self.config.get("language") or None,
            vad_filter=True,
            beam_size=5,
        )
        return "".join(segment.text for segment in segments).strip()


class QwenASRTranscriber:
    def __init__(self, config: dict) -> None:
        self.config = config
        self._model = None
        self._model_lock = threading.Lock()

    def _load(self) -> None:
        if self._model is not None:
            return
        with self._model_lock:
            if self._model is not None:
                return
            import torch
            from qwen_asr import Qwen3ASRModel

            dtype_name = str(self.config.get("qwen_dtype", "bfloat16"))
            dtype = getattr(torch, dtype_name)
            model_name = str(self.config["qwen_model"])
            print(f"首次加载语音模型 {model_name}，可能需要下载模型文件……")
            self._model = Qwen3ASRModel.from_pretrained(
                model_name,
                dtype=dtype,
                device_map=str(self.config.get("qwen_device", "cuda:0")),
                max_inference_batch_size=1,
                max_new_tokens=int(self.config.get("qwen_max_new_tokens", 256)),
            )

    def warmup(self) -> None:
        self._load()

    def close(self) -> None:
        with self._model_lock:
            self._model = None
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    def transcribe(self, path: Path) -> str:
        self._load()
        results = self._model.transcribe(
            audio=str(path),
            language=self.config.get("qwen_language") or None,
        )
        if not results:
            return ""
        return str(results[0].text).strip()


class MiMoASRTranscriber:
    """Cloud ASR option; the local Qwen transcriber remains independently selectable."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self._client = None

    def _load(self) -> None:
        if self._client is not None:
            return
        import httpx

        key = load_mimo_key(PROJECT_ROOT / "api_key.txt")
        self._client = httpx.Client(
            base_url=str(
                self.config.get("mimo_base_url", "https://api.xiaomimimo.com/v1")
            ).rstrip("/"),
            headers={"Authorization": f"Bearer {key}"},
            timeout=float(self.config.get("mimo_timeout_seconds", 90)),
        )

    def warmup(self) -> None:
        # Validate local credentials without spending money on an empty API request.
        self._load()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def reconnect(self) -> None:
        """Discard a possibly stale keep-alive connection and create a new one."""
        self.close()
        self._load()

    def transcribe(self, path: Path) -> str:
        import httpx

        self._load()
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        payload = {
            "model": str(self.config.get("mimo_model", "mimo-v2.5-asr")),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": f"data:audio/wav;base64,{encoded}"
                            },
                        }
                    ],
                }
            ],
            "asr_options": {"language": "auto"},
        }
        try:
            response = self._client.post("/chat/completions", json=payload)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            detail = redact_secret(str(exc))
            if isinstance(exc, httpx.HTTPStatusError):
                detail += f"; response={redact_secret(exc.response.text[:800])}"
            raise RuntimeError(f"MiMo-ASR 调用失败：{detail}") from exc
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("MiMo-ASR 返回内容为空。")
        return str((choices[0].get("message") or {}).get("content") or "").strip()


class SpeechTranscriber:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.backend = ""
        self.primary = None
        self.fallback = None
        self._reconnect_callback = None
        self.set_backend(str(config.get("backend", "qwen3-asr")))

    def set_reconnect_callback(self, callback) -> None:
        self._reconnect_callback = callback

    def set_backend(self, backend: str) -> None:
        if backend == self.backend:
            return
        if self.primary is not None:
            self.close()
        if backend == "qwen3-asr":
            self.primary = QwenASRTranscriber(self.config)
        elif backend == "faster-whisper":
            self.primary = WhisperTranscriber(self.config)
        elif backend == "mimo-api":
            self.primary = MiMoASRTranscriber(self.config)
        else:
            raise ValueError(f"不支持的语音识别后端：{backend}")
        if backend == "mimo-api":
            # API selection is a hard boundary: never allocate a local ASR
            # model or silently consume VRAM when the network is unavailable.
            self.fallback = None
        else:
            self.fallback = (
                WhisperTranscriber(self.config)
                if backend != "faster-whisper" and self.config.get("fallback_to_whisper", True)
                else None
            )
        self.backend = backend
        self.config["backend"] = backend

    def transcribe(self, path: Path) -> str:
        try:
            transcript = self.primary.transcribe(path)
            if self._is_provider_rejection(transcript):
                raise RuntimeError("云端 ASR 返回了安全拒绝文案，而不是语音转写。")
            return self.clean_transcript(transcript)
        except Exception as exc:
            if self.backend == "mimo-api":
                callback = getattr(self, "_reconnect_callback", None)
                if callback is not None:
                    callback("语音转写")
                try:
                    self.primary.reconnect()
                    transcript = self.primary.transcribe(path)
                    if self._is_provider_rejection(transcript):
                        raise RuntimeError("云端 ASR 返回了安全拒绝文案，而不是语音转写。")
                    return self.clean_transcript(transcript)
                except Exception as retry_error:
                    raise APIReconnectFailed(
                        f"MiMo-ASR 重新连接后仍不可用：{retry_error}"
                    ) from retry_error
            if self.fallback is None:
                raise
            fallback_name = "本地 Qwen3-ASR" if self.backend == "mimo-api" else "faster-whisper"
            print(f"{self.backend} 暂时不可用，改用 {fallback_name}：{exc}")
            transcript = self.fallback.transcribe(path)
            if self._is_provider_rejection(transcript):
                raise RuntimeError("备用 ASR 也没有返回有效语音转写。")
            return self.clean_transcript(transcript)

    @staticmethod
    def clean_transcript(text: str) -> str:
        """Remove ASR language/control tokens and reject tag-only hallucinations."""
        raw = str(text or "").strip()
        if not raw:
            return ""
        tags = re.findall(r"<[^<>\r\n]{1,48}>", raw)
        cleaned = re.sub(
            r"<\s*(?:chinese|english|mandarin|cantonese|zh|en|transcribe|translate)\s*>"
            r"|<\|[^<>\r\n]{1,48}\|>",
            " ",
            raw,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        natural = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", cleaned.lower())
        # Repeated language markers are a known Qwen-ASR failure mode on
        # silence/noise. They are metadata, never something the user said.
        if tags and (not natural or (len(tags) >= 3 and len(natural) < len(tags))):
            return ""
        return cleaned

    @staticmethod
    def _is_provider_rejection(text: str) -> bool:
        """Detect provider safety messages that must never become user speech."""
        compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", str(text).lower()).strip()
        markers = (
            "the request was rejected because it was considered high risk",
            "request was rejected due to high risk",
            "your request has been rejected for safety reasons",
            "该请求被判定为高风险",
            "请求因高风险被拒绝",
        )
        return any(marker in compact for marker in markers)

    def warmup(self) -> None:
        self.primary.warmup()

    def transcribe_once_with_local(self, path: Path) -> str:
        """Use Qwen once after an explicit prompt, then immediately unload it."""
        local = QwenASRTranscriber(self.config)
        try:
            return self.clean_transcript(local.transcribe(path))
        finally:
            local.close()

    def close(self) -> None:
        self.primary.close()
        if self.fallback is not None:
            self.fallback.close()


def input_devices() -> list[str]:
    devices = _sounddevice().query_devices()
    return [
        f"{index}: {device['name']}"
        for index, device in enumerate(devices)
        if int(device["max_input_channels"]) > 0
    ]
