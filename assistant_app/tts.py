from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
import wave
from array import array
from pathlib import Path
from typing import Any

from .performance import timed


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VOICE_PRESETS = {
    "default": {
        "description": "自然中文音色；可替换 voices/default.wav 自定义",
        "audio": "voices/default.wav",
        "prompt_text": "希望你以后能够做的比我还好呦。",
    },
}

ENGINE_LABELS = {
    "cosyvoice3": "本地 · CosyVoice 3（约 4.2 GB 显存）",
    "f5tts": "本地 · F5-TTS（约 0.7 GB 显存）",
    "mimo-api": "API · MiMo-V2.5-TTS（0 GB 显存）",
}

ENGINE_BACKENDS = {label: backend for backend, label in ENGINE_LABELS.items()}
MIMO_VOICES = ["冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"]


class SpeechSynthesizer:
    """Switchable local streaming TTS running in isolated GPU workers."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self.enabled = bool(config.get("enabled", True))
        self.backend = str(config.get("backend", "cosyvoice3"))
        if self.backend not in ENGINE_LABELS:
            raise ValueError(f"未知语音引擎：{self.backend}")
        self.speaker = str(
            config.get("mimo_voice", "冰糖")
            if self.backend == "mimo-api"
            else config.get("speaker", "default")
        )
        if self.speaker not in self.available_voices():
            self.speaker = "冰糖" if self.backend == "mimo-api" else "default"
        self.max_characters = int(config.get("max_characters", 500))
        self.rate = int(config.get("rate", 1))
        self.preferred_voice = str(config.get("preferred_voice", "")).lower()

        self._speak_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._reader_thread: threading.Thread | None = None
        self._worker: subprocess.Popen[str] | None = None
        self._reconnect_callback = None

    def set_reconnect_callback(self, callback) -> None:
        self._reconnect_callback = callback

    def _request_with_api_reconnect(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._send_worker_request(request)
        except Exception as first_error:
            if self.backend != "mimo-api":
                raise
            if self._reconnect_callback is not None:
                self._reconnect_callback("语音生成")
            self._restart_api_worker()
            try:
                return self._send_worker_request(request)
            except Exception as retry_error:
                raise RuntimeError(
                    f"MiMo-TTS 重新连接后仍不可用：{retry_error}；首次错误：{first_error}"
                ) from retry_error

    def _restart_api_worker(self) -> None:
        try:
            self.close()
        finally:
            if self._worker is not None and self._worker.poll() is not None:
                self._worker = None
            self._reader_thread = None

    def voice_descriptions(self) -> dict[str, str]:
        if self.backend == "mimo-api":
            return {name: "MiMo-V2.5-TTS 预设音色" for name in MIMO_VOICES}
        return {name: str(values["description"]) for name, values in VOICE_PRESETS.items()}

    def available_voices(self) -> list[str]:
        return list(MIMO_VOICES if self.backend == "mimo-api" else VOICE_PRESETS)

    @property
    def engine_label(self) -> str:
        return ENGINE_LABELS[self.backend]

    @property
    def voice_label(self) -> str:
        return self.speaker

    def available_engines(self) -> list[str]:
        if not bool(self.config.get("allow_local_engines", True)):
            return [ENGINE_LABELS["mimo-api"]]
        return list(ENGINE_BACKENDS)

    def set_speaker(self, speaker: str) -> None:
        match = next(
            (name for name in self.available_voices() if name.lower() == speaker.lower()),
            None,
        )
        if match is None:
            raise ValueError(f"未知参考音色：{speaker}")
        self.speaker = match
        self.config["mimo_voice" if self.backend == "mimo-api" else "speaker"] = match

    def set_engine(self, backend: str, model: str | None = None) -> None:
        del model
        if backend not in ENGINE_LABELS:
            raise ValueError(f"未知语音引擎：{backend}")
        if backend == self.backend:
            return
        self.close()
        self.backend = backend
        self.config["backend"] = backend
        if self.speaker not in self.available_voices():
            self.speaker = (
                str(self.config.get("mimo_voice", "冰糖"))
                if backend == "mimo-api"
                else str(self.config.get("speaker", "default"))
            )
            if self.speaker not in self.available_voices():
                self.speaker = "冰糖" if backend == "mimo-api" else "default"

    def _clean_text(self, text: str) -> str:
        value = re.sub(r"```.*?```", "代码内容已省略。", text, flags=re.DOTALL)
        value = re.sub(r"https?://\S+", "链接", value)
        value = re.sub(r"[*_#>`~]", "", value)
        slash_spoken_as = str(self.config.get("slash_spoken_as", "或者"))
        value = re.sub(r"\s*[／/]\s*", slash_spoken_as, value)
        value = re.sub(r"\s+", " ", value).strip()
        if len(value) > self.max_characters:
            value = value[: self.max_characters].rstrip() + "。后续内容请查看屏幕。"
        return value

    def _project_path(self, key: str, default: str) -> Path:
        path = Path(str(self.config.get(key, default)))
        return path if path.is_absolute() else PROJECT_ROOT / path

    def _request(self, op: str, text: str = "", output_path: Path | None = None) -> dict[str, Any]:
        preset = VOICE_PRESETS.get(self.speaker, VOICE_PRESETS["default"])
        reference_audio = self._project_path("reference_audio", str(preset["audio"]))
        prompt_text = self.config.get("prompt_text", preset["prompt_text"])
        return {
            "op": op,
            "backend": self.backend,
            "text": text,
            "reference_audio": str(reference_audio),
            "prompt_text": prompt_text,
            "prompt_language": self.config.get("prompt_language", "zh"),
            "text_language": self.config.get("text_language", "zh"),
            "leading_silence_ms": int(self.config.get("leading_silence_ms", 220)),
            "trailing_silence_ms": int(self.config.get("trailing_silence_ms", 180)),
            "model_dir": self.config.get(
                "cosyvoice_model_dir",
                "pretrained_models/Fun-CosyVoice3-0.5B",
            ),
            "speed": float(self.config.get("cosyvoice_speed", 1.0)),
            "fp16": bool(self.config.get("cosyvoice_fp16", True)),
            "f5_model": self.config.get("f5_model", "F5TTS_v1_Base"),
            "f5_nfe_steps": int(self.config.get("f5_nfe_steps", 24)),
            "f5_speed": float(self.config.get("f5_speed", 1.0)),
            "f5_cache_dir": str(
                self._project_path("f5_cache_dir", "engines/F5-TTS/models")
            ),
            "mimo_voice": self.speaker if self.backend == "mimo-api" else str(
                self.config.get("mimo_voice", "冰糖")
            ),
            "mimo_style": str(self.config.get("mimo_style", "")),
            "mimo_base_url": str(
                self.config.get("mimo_base_url", "https://api.xiaomimimo.com/v1")
            ),
            "mimo_timeout_seconds": float(self.config.get("mimo_timeout_seconds", 180)),
            "preload_text": str(
                self.config.get("preload_text", "猫猫已经预热完成啦！")
            ),
            "output_path": str(output_path.resolve()) if output_path is not None else None,
        }

    def warmup(self) -> dict[str, Any]:
        if not self.enabled:
            return {}
        return self._request_with_api_reconnect(self._request("warmup"))

    @timed("tts.speak")
    def speak(self, text: str, delivery_mode: str | None = None) -> dict[str, float | int | str]:
        del delivery_mode
        if not self.enabled or not text.strip():
            return {"chunks": 0, "first_audio_seconds": 0.0, "generation_seconds": 0.0}
        cleaned = self._clean_text(text)
        engine_label = self.engine_label
        with self._speak_lock:
            try:
                result = self._request_with_api_reconnect(self._request("speak", cleaned))
                return {
                    "chunks": int(result.get("chunks", 0)),
                    "first_audio_seconds": float(result.get("first_audio_seconds", 0.0)),
                    "generation_seconds": float(result.get("generation_seconds", 0.0)),
                    "peak_vram_gb": float(result.get("peak_vram_gb", 0.0)),
                    "resident_vram_gb": float(result.get("resident_vram_gb", 0.0)),
                    "engine": engine_label,
                }
            except Exception as gpt_error:
                if self.backend == "mimo-api":
                    raise
                if not self.config.get("fallback_to_sapi", True):
                    raise
                print(f"[TTS] {engine_label} 失败，暂时使用 SAPI：{gpt_error}", file=sys.stderr)
                started = time.perf_counter()
                try:
                    self._speak_sapi(cleaned)
                except Exception as sapi_error:
                    raise RuntimeError(
                        f"{engine_label}：{gpt_error}；Windows SAPI：{sapi_error}"
                    ) from sapi_error
                elapsed = time.perf_counter() - started
                return {
                    "chunks": 1,
                    "first_audio_seconds": elapsed,
                    "generation_seconds": elapsed,
                    "fallback_engine": "Windows SAPI",
                    "fallback_reason": str(gpt_error),
                    "engine": engine_label,
                }

    def synthesize_to_file(self, text: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._request_with_api_reconnect(
            self._request("synthesize", self._clean_text(text), output_path)
        )

    def cached_voice_clip_path(self, cache_root: Path, clip_name: str) -> Path:
        """Return a cache path unique to the active engine and voice."""
        safe_voice = re.sub(r"[^\w.-]+", "_", self.speaker, flags=re.UNICODE).strip("._")
        if not safe_voice:
            safe_voice = "default"
        safe_clip = re.sub(r"[^\w.-]+", "_", clip_name, flags=re.UNICODE).strip("._")
        if not safe_clip:
            safe_clip = "clip"
        return cache_root / self.backend / safe_voice / f"{safe_clip}.wav"

    @staticmethod
    def _cached_clip_is_audible(path: Path) -> bool:
        """Reject truncated or silent cache files before reusing them."""
        try:
            with wave.open(str(path), "rb") as source:
                if source.getsampwidth() != 2 or source.getnframes() <= 0:
                    return False
                samples = array("h", source.readframes(source.getnframes()))
            return bool(samples) and max(abs(sample) for sample in samples) >= 16
        except (OSError, EOFError, wave.Error, ValueError):
            return False

    def _cached_clip_has_padding(
        self,
        path: Path,
        leading_silence_ms: int | None = None,
        trailing_silence_ms: int | None = None,
    ) -> bool:
        """Return whether a cached clip has the complete explicit preroll and tail."""
        leading_ms = max(
            80,
            int(
                leading_silence_ms
                if leading_silence_ms is not None
                else self.config.get("leading_silence_ms", 220)
            ),
        )
        trailing_ms = max(
            60,
            int(
                trailing_silence_ms
                if trailing_silence_ms is not None
                else self.config.get("trailing_silence_ms", 180)
            ),
        )
        try:
            with wave.open(str(path), "rb") as source:
                rate = source.getframerate()
                leading_frames = max(1, int(rate * leading_ms / 1000))
                trailing_frames = max(1, int(rate * trailing_ms / 1000))
                if source.getnframes() <= leading_frames + trailing_frames:
                    return False
                leading = array("h", source.readframes(leading_frames))
                source.setpos(source.getnframes() - trailing_frames)
                trailing = array("h", source.readframes(trailing_frames))
            return (
                bool(leading)
                and bool(trailing)
                and max(abs(sample) for sample in leading) <= 2
                and max(abs(sample) for sample in trailing) <= 2
            )
        except (OSError, EOFError, wave.Error, ValueError):
            return False

    def synthesize_cached_voice_clip(
        self,
        text: str,
        cache_root: Path,
        clip_name: str = "goodbye",
        leading_silence_ms: int | None = None,
        trailing_silence_ms: int | None = None,
    ) -> Path:
        """Generate a reusable clip with the currently selected voice."""
        with self._speak_lock:
            output_path = self.cached_voice_clip_path(cache_root, clip_name)
            if (
                output_path.is_file()
                and self._cached_clip_is_audible(output_path)
                and self._cached_clip_has_padding(
                    output_path,
                    leading_silence_ms,
                    trailing_silence_ms,
                )
            ):
                return output_path
            # A previous interrupted/failed synthesis can leave a correctly
            # sized WAV containing only zeros.  Remove it so this call really
            # regenerates the selected engine and voice instead of reusing it.
            output_path.unlink(missing_ok=True)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = output_path.with_name(
                f".{output_path.stem}-{uuid.uuid4().hex}.wav"
            )
            try:
                request = self._request(
                    "synthesize",
                    self._clean_text(text),
                    temporary,
                )
                if leading_silence_ms is not None:
                    request["leading_silence_ms"] = max(80, int(leading_silence_ms))
                if trailing_silence_ms is not None:
                    request["trailing_silence_ms"] = max(60, int(trailing_silence_ms))
                self._request_with_api_reconnect(request)
                if not self._cached_clip_is_audible(temporary):
                    raise RuntimeError("生成的语音缓存没有有效声音。")
                os.replace(temporary, output_path)
            finally:
                temporary.unlink(missing_ok=True)
            return output_path

    def pause(self) -> None:
        if self._worker is not None and self._worker.poll() is None:
            self._send_worker_request({"op": "pause"})

    def resume(self) -> None:
        if self._worker is not None and self._worker.poll() is None:
            self._send_worker_request({"op": "resume"})

    def stop(self) -> None:
        if self._worker is not None and self._worker.poll() is None:
            self._send_worker_request({"op": "stop"})

    def _ensure_worker(self) -> subprocess.Popen[str]:
        with self._worker_lock:
            if self._worker is not None and self._worker.poll() is None:
                return self._worker
            worker_settings = {
                "cosyvoice3": (".venv-cosy", "assistant_app.workers.cosyvoice_worker"),
                "f5tts": (".venv-f5tts", "assistant_app.workers.tts_worker"),
                "mimo-api": (".venv", "assistant_app.workers.tts_worker"),
            }
            environment_name, module = worker_settings[self.backend]
            configured_python = str(self.config.get("worker_python", "")).strip()
            if configured_python:
                worker_python = Path(configured_python)
                python = (
                    worker_python
                    if worker_python.is_absolute()
                    else PROJECT_ROOT / worker_python
                )
            else:
                python = PROJECT_ROOT / environment_name / "Scripts" / "python.exe"
            engine_label = self.engine_label
            if not python.is_file():
                raise RuntimeError(f"{engine_label} 独立环境不存在，请重新安装语音引擎。")
            environment = dict(os.environ)
            environment.update(
                {
                    "PYTHONUTF8": "1",
                    "HF_HUB_DISABLE_PROGRESS_BARS": "1",
                    "TRANSFORMERS_VERBOSITY": "error",
                    "HF_HOME": str(PROJECT_ROOT / "engines" / ".cache" / "huggingface"),
                    "MODELSCOPE_CACHE": str(PROJECT_ROOT / "engines" / ".cache" / "modelscope"),
                }
            )
            self._worker = subprocess.Popen(
                [str(python), "-m", module],
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self._reader_thread = threading.Thread(target=self._read_worker_results, args=(self._worker,), daemon=True)
            self._reader_thread.start()
            return self._worker

    def _read_worker_results(self, worker: subprocess.Popen[str]) -> None:
        if worker.stdout is None:
            return
        for line in worker.stdout:
            if not line.startswith("TTS_RESULT "):
                continue
            result = json.loads(line.removeprefix("TTS_RESULT "))
            with self._pending_lock:
                response_queue = self._pending.pop(str(result.get("request_id", "")), None)
            if response_queue is not None:
                response_queue.put(result)
        try:
            exit_code = worker.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            exit_code = worker.poll()
        error = {"ok": False, "error": f"TTS 子进程异常退出，代码 {exit_code}。"}
        with self._pending_lock:
            pending, self._pending = self._pending, {}
        for response_queue in pending.values():
            response_queue.put(error)

    def _send_worker_request(self, request: dict[str, Any]) -> dict[str, Any]:
        worker = self._ensure_worker()
        if worker.stdin is None:
            raise RuntimeError(f"{self.engine_label} 子进程管道不可用。")
        request_id = uuid.uuid4().hex
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        payload = dict(request)
        payload["request_id"] = request_id
        with self._pending_lock:
            self._pending[request_id] = response_queue
        try:
            with self._write_lock:
                worker.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                worker.stdin.flush()
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise
        result = response_queue.get()
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "未知 TTS 错误"))
        return result

    def _speak_sapi(self, text: str) -> None:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            voice.Rate = self.rate
            tokens = list(voice.GetVoices())
            selected = None
            if self.preferred_voice:
                selected = next(
                    (token for token in tokens if self.preferred_voice in str(token.GetDescription()).lower()),
                    None,
                )
            if selected is None:
                selected = next(
                    (
                        token
                        for token in tokens
                        if "804" in str(token.GetAttribute("Language"))
                        or "chinese" in str(token.GetDescription()).lower()
                    ),
                    None,
                )
            if selected is not None:
                voice.Voice = selected
            voice.Speak(text)
        finally:
            pythoncom.CoUninitialize()

    def close(self) -> None:
        if self._worker is None or self._worker.poll() is not None:
            return
        try:
            self._send_worker_request({"op": "shutdown"})
            self._worker.wait(timeout=5)
        except (RuntimeError, subprocess.TimeoutExpired):
            self._worker.terminate()
            try:
                self._worker.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._worker.kill()
        self._worker = None

    @staticmethod
    def list_voices() -> list[str]:
        import win32com.client

        voice = win32com.client.Dispatch("SAPI.SpVoice")
        return [str(token.GetDescription()) for token in voice.GetVoices()]
