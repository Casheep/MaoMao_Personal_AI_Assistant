from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path


def _wait_for_process(process_id: int) -> None:
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x00100000, False, process_id)  # SYNCHRONIZE
        if handle:
            try:
                kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
            finally:
                kernel32.CloseHandle(handle)
        return

    while True:
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            return
        time.sleep(0.1)


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    try:
        previous_process_id = int(sys.argv[1])
    except ValueError:
        return 2

    _wait_for_process(previous_process_id)
    project_root = Path(__file__).resolve().parent.parent
    subprocess.Popen(
        [sys.executable, "-m", "assistant_app.rounded_gui"],
        cwd=str(project_root),
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
