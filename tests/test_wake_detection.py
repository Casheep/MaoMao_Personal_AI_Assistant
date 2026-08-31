from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from assistant_app.audio import WakeWordListener
from assistant_app.wake_detection import KeywordCheckResult, SherpaKeywordChecker


class WakeDetectionPolicyTests(unittest.TestCase):
    def test_default_policy_requires_stable_exact_partial_keyword(self) -> None:
        listener = WakeWordListener(
            {"enabled": False, "keyword": "猫猫", "aliases": ["猫猫"]},
            on_wake=lambda: None,
        )
        self.assertEqual(listener.partial_hits_required, 2)
        self.assertFalse(listener.allow_fragment_trigger)
        self.assertFalse(listener._partial_keyword_match("猫猫", 1, 0.0))
        self.assertFalse(listener._partial_keyword_match("猫", 20, 0.0))

    def test_shadow_checker_records_result_without_blocking_candidate(self) -> None:
        listener = WakeWordListener(
            {"enabled": False, "checker": {"mode": "shadow"}},
            on_wake=lambda: None,
        )
        listener.checker = SimpleNamespace(
            active=True,
            mode="shadow",
            check_pcm16=lambda *_args: KeywordCheckResult(True, False, 0.012),
        )
        with patch("assistant_app.audio.record_timing") as record:
            self.assertTrue(listener._checker_allows(b"\0\0",))
        record.assert_called_once_with(
            "wake.checker.candidate",
            0.012,
            success=False,
        )

    def test_enforced_checker_can_veto_vosk_candidate(self) -> None:
        listener = WakeWordListener(
            {"enabled": False, "checker": {"mode": "enforce"}},
            on_wake=lambda: None,
        )
        listener.checker = SimpleNamespace(
            active=True,
            mode="enforce",
            check_pcm16=lambda *_args: KeywordCheckResult(True, False, 0.008),
        )
        with patch("assistant_app.audio.record_timing"):
            self.assertFalse(listener._checker_allows(b"\0\0"))

    def test_sherpa_checker_uses_in_memory_custom_keyword_stream(self) -> None:
        class Stream:
            def accept_waveform(self, _sample_rate, _samples) -> None:
                return None

            def input_finished(self) -> None:
                return None

        class Spotter:
            def __init__(self, **_kwargs) -> None:
                self.ready = True

            def create_stream(self, keywords: str):
                self_outer.assertEqual(keywords, "m āo m āo @猫猫")
                return Stream()

            def is_ready(self, _stream) -> bool:
                if self.ready:
                    self.ready = False
                    return True
                return False

            def decode_stream(self, _stream) -> None:
                return None

            def get_result(self, _stream) -> str:
                return "猫猫"

        self_outer = self
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "model"
            model.mkdir()
            for name in (
                "tokens.txt",
                "keywords.txt",
                "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
                "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
                "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            ):
                (model / name).write_bytes(b"test")
            checker = SherpaKeywordChecker(
                {"model_path": str(model), "mode": "enforce"},
                ["猫猫"],
                project_root=root,
            )
            with (
                patch("sherpa_onnx.KeywordSpotter", Spotter),
                patch(
                    "sherpa_onnx.utils.text2token",
                    return_value=[["m", "āo", "m", "āo"]],
                ),
            ):
                result = checker.check_pcm16(
                    np.zeros(1600, dtype=np.int16).tobytes(),
                    16000,
                )
        self.assertTrue(result.available)
        self.assertTrue(result.accepted)


if __name__ == "__main__":
    unittest.main()
