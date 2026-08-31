from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class KeywordCheckResult:
    available: bool
    accepted: bool
    elapsed_seconds: float


class SherpaKeywordChecker:
    """High-precision local checker for candidates from the always-on detector."""

    def __init__(
        self,
        config: dict[str, Any] | None,
        keywords: list[str],
        project_root: Path = PROJECT_ROOT,
    ) -> None:
        values = dict(config or {})
        self.enabled = bool(values.get("enabled", True))
        self.mode = str(values.get("mode", "shadow")).strip().lower()
        if self.mode not in {"off", "shadow", "enforce"}:
            self.mode = "shadow"
        model_path = Path(
            str(
                values.get(
                    "model_path",
                    "engines/sherpa-onnx/sherpa-onnx-kws-zipformer-"
                    "wenetspeech-3.3M-2024-01-01",
                )
            )
        )
        self.model_path = (
            model_path if model_path.is_absolute() else project_root / model_path
        )
        self.num_threads = max(1, min(4, int(values.get("num_threads", 1))))
        self.keywords_score = float(values.get("keywords_score", 1.0))
        self.keywords_threshold = min(
            0.95, max(0.05, float(values.get("keywords_threshold", 0.30)))
        )
        self.max_audio_seconds = min(
            5.0, max(1.0, float(values.get("max_audio_seconds", 3.0)))
        )
        self.trailing_silence_seconds = min(
            1.2,
            max(0.2, float(values.get("trailing_silence_seconds", 0.72))),
        )
        self._keywords = list(keywords)
        self._keyword_spec = ""
        self._spotter: Any = None
        self._load_attempted = False
        self._lock = threading.Lock()

    @staticmethod
    def _normalise(value: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff]", "", str(value)).lower()

    @property
    def active(self) -> bool:
        return self.enabled and self.mode != "off"

    @property
    def available(self) -> bool:
        return self._spotter is not None and bool(self._keyword_spec)

    def update_keywords(self, keywords: list[str]) -> None:
        with self._lock:
            self._keywords = list(keywords)
            self._keyword_spec = ""
            if self._spotter is not None:
                self._keyword_spec = self._encode_keywords()

    def _model_files(self) -> dict[str, Path]:
        return {
            "tokens": self.model_path / "tokens.txt",
            "encoder": self.model_path
            / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "decoder": self.model_path
            / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "joiner": self.model_path
            / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "keywords_file": self.model_path / "keywords.txt",
        }

    def _encode_keywords(self) -> str:
        from sherpa_onnx.utils import text2token

        aliases = [value.strip() for value in self._keywords if value.strip()]
        rows: list[str] = []
        for alias in aliases:
            encoded = text2token(
                [alias],
                tokens=str(self._model_files()["tokens"]),
                tokens_type="ppinyin",
            )
            tokens = encoded[0] if encoded else []
            if tokens:
                rows.append(f"{' '.join(str(token) for token in tokens)} @{alias}")
        return "\n".join(rows)

    def prepare(self) -> bool:
        if not self.active:
            return False
        with self._lock:
            if self._spotter is not None:
                return bool(self._keyword_spec)
            if self._load_attempted:
                return False
            self._load_attempted = True
            try:
                import sherpa_onnx

                files = self._model_files()
                if not all(path.is_file() for path in files.values()):
                    return False
                self._spotter = sherpa_onnx.KeywordSpotter(
                    tokens=str(files["tokens"]),
                    encoder=str(files["encoder"]),
                    decoder=str(files["decoder"]),
                    joiner=str(files["joiner"]),
                    keywords_file=str(files["keywords_file"]),
                    num_threads=self.num_threads,
                    keywords_score=self.keywords_score,
                    keywords_threshold=self.keywords_threshold,
                    provider="cpu",
                )
                self._keyword_spec = self._encode_keywords()
                if not self._keyword_spec:
                    self._spotter = None
                    return False
                return True
            except (ImportError, OSError, RuntimeError, ValueError, AssertionError):
                self._spotter = None
                self._keyword_spec = ""
                return False

    def check_pcm16(self, audio_bytes: bytes, sample_rate: int) -> KeywordCheckResult:
        started = perf_counter()
        if not self.prepare():
            return KeywordCheckResult(False, False, perf_counter() - started)
        try:
            import numpy as np

            samples = np.frombuffer(audio_bytes, dtype=np.int16)
            maximum = max(1, int(sample_rate * self.max_audio_seconds))
            samples = samples[-maximum:].astype(np.float32) / 32768.0
            trailing = np.zeros(
                max(1, int(sample_rate * self.trailing_silence_seconds)),
                dtype=np.float32,
            )
            stream = self._spotter.create_stream(self._keyword_spec)
            block = max(1, int(sample_rate * 0.08))
            accepted = False
            for start in range(0, len(samples), block):
                stream.accept_waveform(sample_rate, samples[start : start + block])
                accepted = self._decode_ready(stream) or accepted
            for start in range(0, len(trailing), block):
                stream.accept_waveform(sample_rate, trailing[start : start + block])
                accepted = self._decode_ready(stream) or accepted
            stream.input_finished()
            accepted = self._decode_ready(stream) or accepted
            return KeywordCheckResult(True, accepted, perf_counter() - started)
        except (OSError, RuntimeError, ValueError):
            return KeywordCheckResult(True, False, perf_counter() - started)

    def _decode_ready(self, stream: Any) -> bool:
        expected = {self._normalise(value) for value in self._keywords}
        while self._spotter.is_ready(stream):
            self._spotter.decode_stream(stream)
            result = self._normalise(self._spotter.get_result(stream))
            if result and result in expected:
                return True
        return False
