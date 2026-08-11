from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENGINE_ROOT = PROJECT_ROOT / "engines" / "CosyVoice"


class CosyVoiceWorker:
    def __init__(self) -> None:
        self.model: Any = None
        self._paused = threading.Event()
        self._cancelled = threading.Event()
        self._generation_lock = threading.Lock()
        self._audio_lock = threading.Lock()
        self._audio_queue: deque[Any] = deque()
        self._audio: Any = None
        self._position = 0
        self._stream: Any = None

    def _load(self, request: dict[str, Any]) -> None:
        if self.model is not None:
            return
        if not ENGINE_ROOT.is_dir():
            raise RuntimeError(f"CosyVoice 引擎不存在：{ENGINE_ROOT}")
        sys.path.insert(0, str(ENGINE_ROOT))
        sys.path.insert(0, str(ENGINE_ROOT / "third_party" / "Matcha-TTS"))
        os.chdir(ENGINE_ROOT)
        from cosyvoice.cli.cosyvoice import AutoModel

        model_dir = Path(str(request.get("model_dir", "pretrained_models/Fun-CosyVoice3-0.5B")))
        if not model_dir.is_absolute():
            model_dir = ENGINE_ROOT / model_dir
        self.model = AutoModel(
            model_dir=str(model_dir),
            load_trt=False,
            fp16=bool(request.get("fp16", True)),
        )

    @staticmethod
    def _trim_trailing_silence(audio: Any, sample_rate: int, keep_ms: int) -> Any:
        import numpy as np

        values = np.asarray(audio, dtype=np.int16).reshape(-1)
        if not values.size:
            return values
        window = max(1, int(sample_rate * 0.01))
        count = len(values) // window
        if not count:
            return values
        framed = values[: count * window].astype(np.float32).reshape(count, window)
        rms = np.sqrt(np.mean(framed * framed, axis=1) + 1.0)
        silent = rms < 260.0
        trailing_windows = 0
        for value in silent[::-1]:
            if not value:
                break
            trailing_windows += 1
        keep_windows = max(1, int(keep_ms / 10))
        if trailing_windows <= keep_windows:
            return values
        return values[: -(trailing_windows - keep_windows) * window]

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

    def _enqueue_playback(self, audio: Any, sample_rate: int) -> None:
        import numpy as np
        import sounddevice as sd

        values = np.asarray(audio, dtype=np.float32).reshape(-1, 1) / 32768.0
        if not values.size:
            return
        with self._audio_lock:
            self._audio_queue.append(values)
        if self._stream is not None and self._stream.active:
            return
        if self._stream is not None:
            self._stream.close()

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

    def _wait_for_playback(self) -> None:
        """Do not report speech completion until the output queue is actually silent."""
        while not self._cancelled.is_set():
            if self._paused.is_set():
                time.sleep(0.03)
                continue
            with self._audio_lock:
                pending = self._audio is not None or bool(self._audio_queue)
            if not pending:
                return
            time.sleep(0.02)

    def _generate(self, request: dict[str, Any], play: bool) -> tuple[list[Any], int, dict[str, Any]]:
        import numpy as np
        import torch

        self._load(request)
        self._cancelled.clear()
        self._paused.clear()
        torch.cuda.reset_peak_memory_stats()
        started = time.perf_counter()
        first_audio = 0.0
        chunks: list[Any] = []
        sample_rate = int(self.model.sample_rate)
        prompt = str(request.get("prompt_text", "希望你以后能够做的比我还好呦。"))
        if "<|endofprompt|>" not in prompt:
            prompt = "You are a helpful assistant.<|endofprompt|>" + prompt
        stream = self.model.inference_zero_shot(
            str(request["text"]),
            prompt,
            str(request["reference_audio"]),
            stream=True,
            speed=float(request.get("speed", 1.0)),
        )
        if play:
            # Prime the Windows output device with silence so the first
            # phoneme is not swallowed when a fresh stream starts.
            leading_frames = max(
                0,
                round(sample_rate * int(request.get("leading_silence_ms", 220)) / 1000),
            )
            if leading_frames:
                self._enqueue_playback(np.zeros(leading_frames, dtype=np.int16), sample_rate)
        for output in stream:
            if self._cancelled.is_set():
                break
            tensor = output["tts_speech"].detach().float().cpu().numpy().reshape(-1)
            raw_audio = np.clip(tensor * 32767.0, -32768, 32767).astype(np.int16)
            audio = self._trim_trailing_silence(
                raw_audio,
                sample_rate,
                int(request.get("trailing_silence_ms", 180)),
            )
            if not np.any(audio):
                continue
            chunks.append(audio)
            if not first_audio:
                first_audio = time.perf_counter() - started
            if play:
                self._enqueue_playback(audio, sample_rate)
        if play:
            trailing_frames = max(
                0,
                round(sample_rate * int(request.get("trailing_silence_ms", 180)) / 1000),
            )
            if trailing_frames:
                self._enqueue_playback(np.zeros(trailing_frames, dtype=np.int16), sample_rate)
            self._wait_for_playback()
        elapsed = time.perf_counter() - started
        return chunks, sample_rate, {
            "chunks": len(chunks),
            "first_audio_seconds": first_audio or elapsed,
            "generation_seconds": elapsed,
            "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 1073741824, 3),
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
                joined = np.concatenate(chunks) if chunks else np.zeros(sample_rate, dtype=np.int16)
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
    worker = CosyVoiceWorker()
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
