from __future__ import annotations

import argparse
import ctypes
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any


def _normalise(value: str) -> str:
    return re.sub(r"[\s._-]+", "", value.strip().lower())


def _find_window() -> Any | None:
    import psutil
    from pywinauto import Desktop

    candidates: list[tuple[int, int, Any]] = []
    for window in Desktop(backend="uia").windows():
        if not window.is_visible():
            continue
        title = str(window.window_text() or "").strip()
        normal_title = _normalise(title)
        title_match = any(
            marker in normal_title
            for marker in ("march7thassistant", "三月七小助手")
        )
        try:
            executable = str(psutil.Process(window.element_info.process_id).exe()).lower()
        except (psutil.Error, OSError):
            executable = ""
        path_match = "march7thassistant_full" in executable.replace("/", "\\")
        if not title_match and not path_match:
            continue
        try:
            rectangle = window.rectangle()
            area = max(0, int(rectangle.width()) * int(rectangle.height()))
        except Exception:
            area = 0
        candidates.append((2 if title_match else 1, area, window))
    return max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def _invoke(control: Any) -> None:
    try:
        control.iface_invoke.Invoke()
    except Exception:
        control.click_input()


def _click_task_card(window: Any, label: Any) -> None:
    target = label
    try:
        window_rectangle = window.rectangle()
        window_area = max(1, window_rectangle.width() * window_rectangle.height())
        best_area = 0
        current = label
        for _ in range(5):
            current = current.parent()
            rectangle = current.rectangle()
            area = max(0, rectangle.width() * rectangle.height())
            if best_area < area <= window_area * 0.18:
                target = current
                best_area = area
    except Exception:
        target = label
    target.click_input()


def _write_result(path: Path, success: bool, message: str) -> None:
    path.write_text(
        json.dumps({"success": success, "message": message}, ensure_ascii=False),
        encoding="utf-8",
    )


def run(executable: Path, button_text: str, cancel_path: Path) -> tuple[bool, str]:
    if cancel_path.exists():
        return False, "星铁日常操作已取消。"
    subprocess.Popen(
        [str(executable)],
        cwd=str(executable.parent),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    window = None
    deadline = time.monotonic() + 35.0
    while window is None and time.monotonic() < deadline:
        if cancel_path.exists():
            return False, "星铁日常操作已取消。"
        time.sleep(0.25)
        window = _find_window()
    if window is None:
        return False, "三月七助手已经启动，但没有找到主窗口。"

    try:
        handle = int(window.handle)
        ctypes.windll.user32.ShowWindow(handle, 9)
        ctypes.windll.user32.SetForegroundWindow(handle)
        time.sleep(0.25)
        window.set_focus()
    except Exception:
        pass
    wanted = _normalise(button_text)
    candidate = None
    try:
        for control in window.descendants():
            text = _normalise(str(control.window_text() or ""))
            if text == wanted and control.is_visible() and control.is_enabled():
                candidate = control
                break
    except Exception:
        candidate = None

    if cancel_path.exists():
        return False, "星铁日常操作已取消。"
    if candidate is not None:
        _click_task_card(window, candidate)
    else:
        import pyautogui

        rectangle = window.rectangle()
        pyautogui.click(
            int(rectangle.left + rectangle.width() * 0.124),
            int(rectangle.top + rectangle.height() * 0.846),
        )
    return True, "已打开三月七助手并点击“完整运行”。"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True)
    parser.add_argument("--button-text", default="完整运行")
    parser.add_argument("--result", required=True)
    parser.add_argument("--cancel", required=True)
    args = parser.parse_args()
    result_path = Path(args.result)
    cancel_path = Path(args.cancel)
    try:
        success, message = run(Path(args.executable), args.button_text, cancel_path)
    except Exception as exc:
        success = False
        message = f"三月七助手自动化失败：{type(exc).__name__}: {exc}"
    try:
        if not cancel_path.exists():
            _write_result(result_path, success, message)
    finally:
        cancel_path.unlink(missing_ok=True)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
