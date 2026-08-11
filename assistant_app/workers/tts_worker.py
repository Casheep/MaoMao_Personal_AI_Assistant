from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
import time
import traceback
import base64
from collections import deque
from pathlib import Path
from typing import Any, Iterable

from ..secrets import load_mimo_key, redact_secret


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AlternateTTSWorker:
    """F5-TTS and MiMo API worker with interruptible playback."""

    def __init__(self) -> None:
        self.backend = ""
        self.model: Any = None
        self._http_client: Any = None
        self._paused = threading.Event()
        self._cancelled = threading.Event()
        self._generation_lock = threading.Lock()
        self._audio_lock = threading.Lock()
        self._audio_queue: deque[Any] = deque()
        self._audio: Any = None
        self._position = 0
        self._stream: Any = None
        self._sample_rate = 0

    def _load(self, request: dict[str, Any]) -> None:
        backend = str(request["backend"])
        if self.model is not None:
            if backend != self.backend:
                raise RuntimeError("同一个语音进程不能加载两种引擎，请先切换引擎。")
            return
        if backend == "f5tts":
            engine_root = PROJECT_ROOT / "engines" / "F5-TTS"
            if not engine_root.is_dir():
                raise RuntimeError(f"F5-TTS 引擎不存在：{engine_root}")
            import static_ffmpeg

            ffmpeg_dir = engine_root / "win32"
            ffmpeg_dir.mkdir(parents=True, exist_ok=True)
            if not static_ffmpeg.add_paths(download_dir=str(ffmpeg_dir)):
                raise RuntimeError("无法准备 F5-TTS 所需的 FFmpeg。")
            from f5_tts.api import F5TTS

            cache_dir = Path(str(request["f5_cache_dir"]))
            cache_dir.mkdir(parents=True, exist_ok=True)
            self.model = F5TTS(
                model=str(request.get("f5_model", "F5TTS_v1_Base")),
                device="cuda",
                hf_cache_dir=str(cache_dir),
            )
        elif backend == "mimo-api":
            import httpx

            self.model = load_mimo_key(PROJECT_ROOT / "api_key.txt")
            self._http_client = httpx.Client(
                base_url=str(
                    request.get("mimo_base_url", "https://api.xiaomimimo.com/v1")
                ).rstrip("/"),
                headers={"Authorization": f"Bearer {self.model}"},
                timeout=float(request.get("mimo_timeout_seconds", 180)),
                limits=httpx.Limits(
                    max_connections=2,
                    max_keepalive_connections=1,
                    keepalive_expiry=300.0,
                ),
            )
        else:
            raise ValueError(f"未知语音引擎：{backend}")
        self.backend = backend

    def _stop_playback(self) -> None:
        self._paused.clear()
        self._cancelled.set()
        with self._audio_lock:
            self._audio_queue.clear()
            self._audio = None
            self._position = 0
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.abort()
            finally:
                stream.close()

    def _ensure_stream(self, sample_rate: int) -> None:
        import sounddevice as sd

        if self._stream is not None and self._stream.active and self._sample_rate == sample_rate:
            return
        if self._stream is not None:
            self._stream.close()
        self._sample_rate = sample_rate

        def callback(outdata: Any, frames: int, _time_info: Any, _status: Any) -> None:
            outdata.fill(0)
            if self._paused.is_set():
                return
            written = 0
            while written < frames:
                with self._audio_lock:
                    if self._audio is None and self._audio_queue:
                        self._audio = self._audio_queue.popleft()
                        self._position = 0
                    current = self._audio
                if current is None:
                    return
                remaining = len(current) - self._position
                count = min(frames - written, remaining)
                outdata[written : written + count] = current[self._position : self._position + count]
                self._position += count
                written += count
                if self._position >= len(current):
                    with self._audio_lock:
                        self._audio = None
                        self._position = 0

        self._stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            blocksize=1024,
            latency="low",
            callback=callback,
        )
        self._stream.start()

    def _enqueue_playback(self, audio: Any, sample_rate: int) -> None:
        import numpy as np

        values = np.asarray(audio).reshape(-1)
        if not values.size:
            return
        if values.dtype.kind in "iu":
            values = values.astype(np.float32) / 32768.0
        else:
            values = values.astype(np.float32)
        with self._audio_lock:
            self._audio_queue.append(values.reshape(-1, 1))
        self._ensure_stream(sample_rate)

    def _wait_for_playback(self) -> None:
        while not self._cancelled.is_set():
            if self._paused.is_set():
                time.sleep(0.03)
                continue
            with self._audio_lock:
                pending = self._audio is not None or bool(self._audio_queue)
            if not pending:
                return
            time.sleep(0.02)

    def _f5_generate(self, request: dict[str, Any]) -> tuple[list[Any], int]:
        wav, sample_rate, _spec = self.model.infer(
            ref_file=str(request["reference_audio"]),
            ref_text=str(request["prompt_text"]),
            gen_text=str(request["text"]),
            nfe_step=int(request.get("f5_nfe_steps", 24)),
            speed=float(request.get("f5_speed", 1.0)),
            show_info=lambda _message: None,
            progress=None,
        )
        return [wav], int(sample_rate)

    def _mimo_generate(self, request: dict[str, Any]) -> tuple[Iterable[Any], int]:
        import httpx
        import numpy as np

        payload = {
            "model": "mimo-v2.5-tts",
            "messages": [],
            "audio": {
                "format": "pcm16",
                "voice": str(request.get("mimo_voice", "冰糖")),
            },
            "stream": True,
        }
        style = str(request.get("mimo_style", "")).strip()
        if style:
            payload["messages"].append({"role": "user", "content": style})
        payload["messages"].append({"role": "assistant", "content": str(request["text"])})

        def chunks() -> Iterable[Any]:
            if self._http_client is None:
                raise RuntimeError("MiMo-TTS 长连接尚未初始化。")
            try:
                with self._http_client.stream(
                    "POST",
                    "/chat/completions",
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if self._cancelled.is_set():
                            return
                        if not line.startswith("data:"):
                            continue
                        data = line.removeprefix("data:").strip()
                        if not data or data == "[DONE]":
                            continue
                        body = json.loads(data)
                        choices = body.get("choices") or []
                        if not choices:
                            continue
                        audio = (choices[0].get("delta") or {}).get("audio")
                        if not isinstance(audio, dict) or not audio.get("data"):
                            continue
                        pcm = base64.b64decode(audio["data"])
                        yield np.frombuffer(pcm, dtype=np.int16)
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                detail = redact_secret(str(exc))
                if isinstance(exc, httpx.HTTPStatusError):
                    detail += f"; response={redact_secret(exc.response.text[:800])}"
                raise RuntimeError(f"MiMo-TTS 调用失败：{detail}") from exc

        return chunks(), 24000

    def _generate(self, request: dict[str, Any], play: bool) -> tuple[list[Any], int, dict[str, Any]]:
        import numpy as np
        self._load(request)
        self._cancelled.clear()
        self._paused.clear()
        torch = None
        if self.backend != "mimo-api":
            import torch as torch_module

            torch = torch_module
            torch.cuda.reset_peak_memory_stats()
        started = time.perf_counter()
        first_audio = 0.0
        collected: list[Any] = []
        if self.backend == "f5tts":
            stream, sample_rate = self._f5_generate(request)
        else:
            stream, sample_rate = self._mimo_generate(request)
        if play:
            # Start and keep the output device awake before the first phoneme.
            # Some Windows audio devices swallow the beginning of a stream
            # that starts immediately with speech.
            leading_frames = max(
                0,
                round(sample_rate * int(request.get("leading_silence_ms", 220)) / 1000),
            )
            if leading_frames:
                self._enqueue_playback(np.zeros(leading_frames, dtype=np.float32), sample_rate)
        for chunk in stream:
            if self._cancelled.is_set():
                break
            if hasattr(chunk, "detach"):
                chunk = chunk.detach().float().cpu().numpy()
            values = np.asarray(chunk).reshape(-1)
            if not values.size:
                continue
            if not first_audio:
                first_audio = time.perf_counter() - started
            collected.append(values)
            if play:
                self._enqueue_playback(values, sample_rate)
        if play:
            trailing_frames = max(
                0,
                round(sample_rate * int(request.get("trailing_silence_ms", 180)) / 1000),
            )
            if trailing_frames:
                self._enqueue_playback(np.zeros(trailing_frames, dtype=np.float32), sample_rate)
            self._wait_for_playback()
        elapsed = time.perf_counter() - started
        return collected, sample_rate, {
            "chunks": len(collected),
            "first_audio_seconds": first_audio or elapsed,
            "generation_seconds": elapsed,
            "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 1073741824, 3) if torch else 0.0,
            "resident_vram_gb": round(torch.cuda.memory_allocated() / 1073741824, 3) if torch else 0.0,
        }

    def handle(self, request: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        op = str(request.get("op", "speak"))
        if op == "pause":
            self._paused.set()
            return False, {}
        if op == "resume":
            self._paused.clear()
            return False, {}
        if op == "stop":
            self._stop_playback()
            return False, {}
        if op == "shutdown":
            self._stop_playback()
            if self._http_client is not None:
                self._http_client.close()
                self._http_client = None
            return True, {}
        if op == "warmup":
            request = dict(request)
            request["text"] = str(
                request.get("preload_text", "猫猫已经预热完成啦！")
            )
            with self._generation_lock:
                _chunks, _sample_rate, stats = self._generate(request, play=True)
            return False, stats
        if op in {"speak", "synthesize"}:
            with self._generation_lock:
                chunks, sample_rate, stats = self._generate(request, play=op == "speak")
            if op == "synthesize":
                import numpy as np
                import soundfile as sf

                output = Path(str(request["output_path"]))
                output.parent.mkdir(parents=True, exist_ok=True)
                joined = np.concatenate([np.asarray(chunk).reshape(-1) for chunk in chunks])
                leading_frames = max(
                    0,
                    round(sample_rate * int(request.get("leading_silence_ms", 220)) / 1000),
                )
                trailing_frames = max(
                    0,
                    round(sample_rate * int(request.get("trailing_silence_ms", 180)) / 1000),
                )
                joined = np.concatenate(
                    [
                        np.zeros(leading_frames, dtype=joined.dtype),
                        joined,
                        np.zeros(trailing_frames, dtype=joined.dtype),
                    ]
                )
                sf.write(output, joined, sample_rate, subtype="PCM_16")
            return False, stats
        raise ValueError(f"未知操作：{op}")


def main() -> int:
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    worker = AlternateTTSWorker()
    output_lock = threading.Lock()
    generation_requests: queue.Queue[dict[str, Any] | None] = queue.Queue()
    shutting_down = threading.Event()

    def process(request: dict[str, Any]) -> None:
        try:
            should_exit, details = worker.handle(request)
            result = {"ok": True, **details}
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            should_exit = False
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        result["request_id"] = request.get("request_id", "")
        with output_lock:
            print("TTS_RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
        if should_exit:
            shutting_down.set()

    def read_requests() -> None:
        for line in sys.stdin:
            request = json.loads(line)
            op = str(request.get("op", "speak"))
            if op in {"warmup", "speak", "synthesize"}:
                generation_requests.put(request)
            else:
                process(request)
            if op == "shutdown":
                generation_requests.put(None)
                return

    line = sys.stdin.readline()
    if not line:
        return 0
    first_request = json.loads(line)
    process(first_request)
    if str(first_request.get("op")) == "shutdown":
        return 0
    threading.Thread(target=read_requests, daemon=True).start()
    while not shutting_down.is_set():
        request = generation_requests.get()
        if request is None:
            break
        process(request)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
