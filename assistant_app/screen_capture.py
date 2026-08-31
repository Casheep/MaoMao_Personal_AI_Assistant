from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .performance import record_timing


@dataclass(frozen=True)
class CapturedScreen:
    image: Any
    mode: str
    bounds: tuple[int, int, int, int]


@dataclass(frozen=True)
class EncodedScreen:
    source_size: tuple[int, int]
    output_size: tuple[int, int]
    byte_count: int


@dataclass(frozen=True)
class PreparedScreen:
    path: Path
    content: str
    prompt: str


_PROFILE_SETTINGS = {
    "overview": (896, 68, 2),
    "balanced": (1152, 76, 1),
    "detail": (1536, 88, 0),
}


def foreground_window_bbox() -> tuple[int, int, int, int] | None:
    """Return the visible foreground-window bounds unless MaoMao owns the window."""
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.windll.user32
        handle = int(user32.GetForegroundWindow())
        if not handle or not user32.IsWindowVisible(handle) or user32.IsIconic(handle):
            return None
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
        if int(process_id.value) == os.getpid():
            return None
        rectangle = wintypes.RECT()
        if not user32.GetWindowRect(handle, ctypes.byref(rectangle)):
            return None
        bounds = (
            int(rectangle.left),
            int(rectangle.top),
            int(rectangle.right),
            int(rectangle.bottom),
        )
        if bounds[2] - bounds[0] < 200 or bounds[3] - bounds[1] < 120:
            return None
        return bounds
    except Exception:
        return None


def _virtual_screen_bounds(image: Any) -> tuple[int, int, int, int]:
    if os.name != "nt":
        return (0, 0, int(image.width), int(image.height))
    try:
        user32 = ctypes.windll.user32
        left = int(user32.GetSystemMetrics(76))
        top = int(user32.GetSystemMetrics(77))
        width = int(user32.GetSystemMetrics(78))
        height = int(user32.GetSystemMetrics(79))
        if width > 0 and height > 0:
            return (left, top, left + width, top + height)
    except Exception:
        pass
    return (0, 0, int(image.width), int(image.height))


def capture_screen_frame() -> CapturedScreen:
    """Capture the foreground window and retain coordinates for optional ROI zoom."""
    from PIL import ImageGrab

    bounds = foreground_window_bbox()
    if bounds is not None:
        try:
            image = ImageGrab.grab(bbox=bounds, all_screens=True)
            return CapturedScreen(image, "foreground", bounds)
        except Exception:
            pass
    image = ImageGrab.grab(all_screens=True)
    return CapturedScreen(image, "all_screens", _virtual_screen_bounds(image))


def capture_for_inspection() -> tuple[Any, str]:
    """Compatibility wrapper returning the image and capture mode."""
    frame = capture_screen_frame()
    return frame.image, frame.mode


def encode_screen_image(image: Any, path: Path, profile: str) -> EncodedScreen:
    """Encode one adaptive JPEG overview; higher resolution is reserved for detail."""
    from PIL import Image

    max_dimension, quality, subsampling = _PROFILE_SETTINGS.get(
        profile,
        _PROFILE_SETTINGS["balanced"],
    )
    source_size = (int(image.width), int(image.height))
    output = image.convert("RGB")
    output.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    output.save(
        path,
        format="JPEG",
        quality=quality,
        optimize=True,
        progressive=True,
        subsampling=subsampling,
    )
    return EncodedScreen(source_size, output.size, int(path.stat().st_size))


class AdaptiveScreenSession:
    """Own one task-scoped source frame and create overview or ROI artifacts."""

    def __init__(self, temporary_dir: Path) -> None:
        self.temporary_dir = temporary_dir
        self.temporary_dir.mkdir(parents=True, exist_ok=True)
        self._source: Any | None = None
        self._bounds: tuple[int, int, int, int] | None = None
        self._overview_size: tuple[int, int] | None = None
        self._task_id = ""

    @staticmethod
    def _profile(value: str) -> str:
        profile = value.strip().lower()
        return profile if profile in _PROFILE_SETTINGS else "balanced"

    def _replace_source(
        self,
        frame: CapturedScreen,
        overview_size: tuple[int, int],
        task_id: str,
    ) -> None:
        previous = self._source
        self._source = frame.image
        self._bounds = frame.bounds
        self._overview_size = overview_size
        self._task_id = task_id
        if previous is not None and previous is not frame.image:
            close = getattr(previous, "close", None)
            if callable(close):
                close()

    def capture(self, profile: str, task_id: str) -> PreparedScreen:
        profile = self._profile(profile)
        path = self.temporary_dir / datetime.now().strftime(
            "screen-%Y%m%d-%H%M%S-%f.jpg"
        )
        capture_started = time.perf_counter()
        capture_success = False
        try:
            frame = capture_screen_frame()
            capture_success = True
        finally:
            record_timing(
                "screen.capture",
                time.perf_counter() - capture_started,
                success=capture_success,
            )
        encode_started = time.perf_counter()
        encode_success = False
        try:
            encoded = encode_screen_image(frame.image, path, profile)
            encode_success = True
        except Exception:
            close = getattr(frame.image, "close", None)
            if callable(close):
                close()
            raise
        finally:
            record_timing(
                "screen.image_save",
                time.perf_counter() - encode_started,
                success=encode_success,
            )
        self._replace_source(frame, encoded.output_size, task_id)

        description = "当前前台窗口" if frame.mode == "foreground" else "当前屏幕"
        left, top, right, bottom = frame.bounds
        output_width, output_height = encoded.output_size
        source_width, source_height = encoded.source_size
        prompt = (
            f"这是刚刚获取的{description}，采用 {profile} 自适应视觉档位。"
            f"原始区域为全局坐标 [{left},{top},{right},{bottom}]，原始尺寸 "
            f"{source_width}x{source_height}，当前图像尺寸 {output_width}x{output_height}。"
            "优先结合随请求提供的 UIA_DATA 理解文字和控件。只有确实看不清关键局部时才调用 "
            "inspect_screen_region，区域坐标使用当前图像的像素坐标；不要无条件切分整张图。"
        )
        return PreparedScreen(
            path,
            f"已截取{description}（{profile}，{output_width}×{output_height}，{encoded.byte_count // 1024} KB）。",
            prompt,
        )

    def zoom(
        self,
        *,
        left: int,
        top: int,
        right: int,
        bottom: int,
        task_id: str,
    ) -> PreparedScreen:
        source = self._source
        bounds = self._bounds
        overview_size = self._overview_size
        if (
            source is None
            or bounds is None
            or overview_size is None
            or self._task_id != task_id
        ):
            raise LookupError("还没有可供放大的屏幕概览，请先调用 inspect_screen。")

        overview_width, overview_height = overview_size
        left = max(0, min(overview_width - 1, int(left)))
        top = max(0, min(overview_height - 1, int(top)))
        right = max(left + 1, min(overview_width, int(right)))
        bottom = max(top + 1, min(overview_height, int(bottom)))
        if right - left < 24 or bottom - top < 24:
            raise ValueError("放大区域太小，请选择至少 24×24 像素的概览区域。")

        scale_x = float(source.width) / float(overview_width)
        scale_y = float(source.height) / float(overview_height)
        source_box = (
            max(0, int(left * scale_x)),
            max(0, int(top * scale_y)),
            min(int(source.width), max(1, int(right * scale_x + 0.999))),
            min(int(source.height), max(1, int(bottom * scale_y + 0.999))),
        )
        region = source.crop(source_box)
        path = self.temporary_dir / datetime.now().strftime(
            "screen-region-%Y%m%d-%H%M%S-%f.jpg"
        )
        encode_started = time.perf_counter()
        encode_success = False
        try:
            encoded = encode_screen_image(region, path, "detail")
            encode_success = True
        finally:
            close = getattr(region, "close", None)
            if callable(close):
                close()
            record_timing(
                "screen.region_encode",
                time.perf_counter() - encode_started,
                success=encode_success,
            )

        global_left = bounds[0] + source_box[0]
        global_top = bounds[1] + source_box[1]
        global_right = bounds[0] + source_box[2]
        global_bottom = bounds[1] + source_box[3]
        prompt = (
            "这是上一张概览的按需高清局部，不是新的全屏。"
            f"它对应概览坐标 [{left},{top},{right},{bottom}]，对应全局屏幕区域 "
            f"[{global_left},{global_top},{global_right},{global_bottom}]，当前局部图像尺寸 "
            f"{encoded.output_size[0]}x{encoded.output_size[1]}。请只用它确认此前看不清的细节。"
        )
        return PreparedScreen(
            path,
            f"已放大目标区域（{encoded.output_size[0]}×{encoded.output_size[1]}，{encoded.byte_count // 1024} KB）。",
            prompt,
        )

    def clear(self) -> None:
        source = self._source
        self._source = None
        self._bounds = None
        self._overview_size = None
        self._task_id = ""
        if source is not None:
            close = getattr(source, "close", None)
            if callable(close):
                close()
