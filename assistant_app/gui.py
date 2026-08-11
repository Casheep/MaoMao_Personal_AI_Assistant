from __future__ import annotations

import json
import ctypes
import os
import queue
import random
import re
import subprocess
import sys
import threading
import tkinter as tk
import time
import uuid
import wave
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from urllib.parse import urlparse

from .agent import OperationCancelled
from .audio import (
    APIReconnectFailed,
    ButtonAudioRecorder,
    SpeechTranscriber,
    WakeWordListener,
    play_wav_file,
)
from .budget import BudgetExceeded
from .cli import build_agent
from .config import app_paths, load_config, save_local_setting, save_local_settings
from .database import Database
from .skills import SKILL_CATALOG, is_skill_enabled
from .tts import ENGINE_BACKENDS, ENGINE_LABELS, SpeechSynthesizer


MODEL_MODE_LABELS = {
    "自动（Kimi 优先）": "auto",
    "Kimi K3": "k3",
    "Kimi K2.6": "k2.6",
    "MiMo V2.5 Pro": "mimo-pro",
    "MiMo V2.5": "mimo-omni",
}
ASR_MODE_LABELS = {
    "API · MiMo-V2.5-ASR": "mimo-api",
}


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def configure_windows_app_identity() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "MaoMao.LocalVoiceAssistant"
        )
    except (AttributeError, OSError):
        pass


def apply_windows_round_corners(root: tk.Tk) -> None:
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)
        preference = ctypes.c_int(2)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 33, ctypes.byref(preference), ctypes.sizeof(preference)
        )
    except (AttributeError, OSError):
        pass


class AssistantWindow:
    def __init__(self, root: tk.Tk, warmup: bool = True) -> None:
        self.root = root
        project_root = Path(__file__).resolve().parent.parent
        icon_path = project_root / "miao.ico"
        icon_png = project_root / "miao-icon.png"
        if icon_path.is_file():
            self.root.iconbitmap(default=str(icon_path))
        if icon_png.is_file():
            self._window_icon = tk.PhotoImage(file=str(icon_png))
            self.root.iconphoto(True, self._window_icon)
        self.config = load_config()
        self.config.setdefault("skills", {})
        self.beta_channel = self.config.get("distribution", {}).get("channel") == "beta"
        self.asr_mode_choices = (
            ["API · MiMo-V2.5-ASR"]
            if self.beta_channel
            else list(ASR_MODE_LABELS)
        )
        self.session_id = uuid.uuid4().hex
        self.busy = False
        self.closing = False
        self._window_restore_pending = False
        self._window_restore_job = None
        self._tray_icon = None
        self._tray_icon_path = project_root / "miao.jpg"
        self._active_task_started = 0.0
        self._task_cancel_event = threading.Event()
        self._action_notice_done = threading.Event()
        self._action_notice_done.set()
        self._action_notice_task_started = 0.0
        self._thinking_filler_stop = threading.Event()
        self._thinking_filler_stop.set()
        self._thinking_audio_done = threading.Event()
        self._thinking_audio_done.set()
        self._last_progress_speech_at = time.monotonic()
        self._wake_sequence = 0
        self._continuous_listening = False
        self._continuous_session_active = False
        self._skip_next_continuous = False
        self._wake_status_value = "唤醒词准备中…"
        self._preload_generation = 0
        self._preload_loading = False
        self._scheduled_task_running = False
        self._active_scheduled_task_id = 0
        self._scheduled_task_cancel_event = threading.Event()
        wake_config = self.config.get("wake_word", {})
        self.wake_enrollment_samples = max(
            3, min(8, int(wake_config.get("enrollment_samples", 5)))
        )
        self.recorder = ButtonAudioRecorder(
            int(self.config["audio"]["sample_rate"]),
            int(self.config["audio"]["channels"]),
            float(wake_config.get("silence_threshold", 420.0)),
            float(wake_config.get("speech_start_seconds", 0.30)),
            float(wake_config.get("adaptive_noise_multiplier", 2.2)),
            float(wake_config.get("adaptive_noise_offset", 80.0)),
        )
        self.transcriber = SpeechTranscriber(self.config["audio"])
        self.tts = SpeechSynthesizer(self.config["tts"])
        self.preload_enabled = tk.BooleanVar(
            value=bool(self.config["tts"].get("preload_on_startup", True))
        )
        self.wake_capture_enabled = tk.BooleanVar(
            value=(
                bool(wake_config.get("capture_enabled", True))
                and is_skill_enabled(self.config, "wake-word")
            )
        )
        self.wake_listener = WakeWordListener(
            wake_config,
            on_wake=lambda: self.root.after(0, self._handle_wake_word),
            on_status=lambda value: self.root.after(0, self._set_wake_status, value),
        )
        self.wake_listener.set_capture_enabled(self.wake_capture_enabled.get())
        self.wake_response_texts = [
            str(value)
            for value in wake_config.get(
                "response_texts", ["我在呢。", "怎么啦？", "听着呢。"]
            )
            if str(value).strip()
        ]
        self.interrupt_response_texts = [
            str(value)
            for value in wake_config.get(
                "interrupt_response_texts", ["嗯？", "在呢。", "你说。"]
            )
            if str(value).strip()
        ]
        conversation_config = self.config.get("conversation", {})
        goodbye_cache_dir = Path(
            str(conversation_config.get("goodbye_cache_dir", "voices/generated-goodbyes"))
        )
        self.goodbye_cache_dir = (
            goodbye_cache_dir
            if goodbye_cache_dir.is_absolute()
            else project_root / goodbye_cache_dir
        )
        self.goodbye_text = str(
            conversation_config.get("goodbye_text", "好呀，拜拜，下次再聊。")
        ).strip() or "拜拜，下次再聊。"
        self.thinking_first_filler_texts = [
            str(value).strip()
            for value in conversation_config.get(
                "thinking_first_filler_texts",
                ["嗯，我想一下。", "我看看。"],
            )
            if str(value).strip()
        ]
        self.thinking_followup_texts = [
            str(value).strip()
            for value in conversation_config.get(
                "thinking_followup_texts",
                ["稍等一下哦。", "嗯，再等我一下。", "快好了。"],
            )
            if str(value).strip()
        ]
        # Kept as a combined compatibility view for cache preparation and old
        # local settings. The worker uses the two stage-specific pools below.
        self.thinking_filler_texts = [
            *self.thinking_first_filler_texts,
            *self.thinking_followup_texts,
        ]
        self.thinking_filler_interval = max(
            3.5,
            float(conversation_config.get("thinking_filler_interval_seconds", 6.0)),
        )
        self.thinking_filler_max_count = max(
            0,
            min(6, int(conversation_config.get("thinking_filler_max_count", 4))),
        )
        self.thinking_long_wait_after_count = max(
            1,
            int(conversation_config.get("thinking_long_wait_after_count", 3)),
        )
        configured_long_wait = conversation_config.get("thinking_long_wait_texts", {})
        self.thinking_long_wait_texts = {
            str(key): str(value).strip()
            for key, value in configured_long_wait.items()
            if str(value).strip()
        } if isinstance(configured_long_wait, dict) else {}
        self.thinking_completion_texts = [
            str(value).strip()
            for value in conversation_config.get(
                "thinking_completion_texts",
                ["我处理好了。", "好啦，已经处理好了。", "弄好啦。"],
            )
            if str(value).strip()
        ]
        self.api_reconnect_text = str(
            conversation_config.get(
                "api_reconnect_text",
                "API 连接刚刚断开了，我重新连接一下，请稍等。",
            )
        ).strip()
        self.api_unavailable_text = str(
            conversation_config.get(
                "api_unavailable_text",
                "API 还是没有连上，请查看屏幕上的提示。",
            )
        ).strip()
        self.transcriber.set_reconnect_callback(self._announce_api_reconnect)
        self.tts.set_reconnect_callback(self._announce_api_reconnect)
        self._thinking_progress_task_started = 0.0
        self._thinking_progress_count = 0
        self._thinking_action_context = ""

        self.status = tk.StringVar(value="就绪")
        self.usage = tk.StringVar(value="费用：读取中…")
        model_mode = str(self.config["routing"].get("model_mode", "auto"))
        self.model_mode = tk.StringVar(
            value=next(
                (label for label, mode in MODEL_MODE_LABELS.items() if mode == model_mode),
                "自动（Kimi 优先）",
            )
        )
        asr_backend = str(self.config["audio"].get("backend", "qwen3-asr"))
        self.asr_mode = tk.StringVar(
            value=next(
                (label for label, backend in ASR_MODE_LABELS.items() if backend == asr_backend),
                "本地 · Qwen3-ASR",
            )
        )
        self.speaker = tk.StringVar(value=self.tts.voice_label)
        self.tts_mode = tk.StringVar(value=self.tts.engine_label)
        self.delivery_mode = tk.StringVar(value="连贯朗读")
        self.continuous_enabled = tk.BooleanVar(
            value=(
                bool(self.config.get("conversation", {}).get("continuous", True))
                and is_skill_enabled(self.config, "continuous-conversation")
            )
        )
        self.timing = tk.StringVar(value="本次处理 · 等待任务")

        self._build_window()
        self._ui_database = Database(app_paths().database)
        self._ui_database.recover_interrupted_scheduled_tasks()
        self.root.after(50, apply_windows_round_corners, self.root)
        self._refresh_usage()
        self._append_chat(
            "system",
            (
                (
                    "MaoMao beta 已就绪。可在左侧“唤醒词”的设置中管理监听和录制，"
                    "也可以直接点击“开始录音”。"
                    if self.wake_capture_enabled.get()
                    else "MaoMao beta 已就绪。唤醒监听已关闭，可以直接开始录音或输入文字。"
                )
                if self.beta_channel
                else f"猫猫已就绪。说“{self.wake_listener.keyword}”可以随时唤醒并打断当前播报。"
            ),
        )
        self._agent_runtime_agent = None
        self._agent_runtime_database = None
        self._agent_runtime_session_id = ""
        self._agent_task_queue: queue.Queue = queue.Queue()
        self._agent_runtime_thread = threading.Thread(
            target=self._agent_runtime_loop,
            name="maomao-agent",
            daemon=True,
        )
        self._agent_runtime_thread.start()
        self._start_background_services(warmup)
        # Tk's timer keeps running while the window is hidden in the tray.
        self.root.after(1000, self._poll_scheduled_tasks)
        # The tray icon represents the running assistant, not the window's
        # visibility, so create it immediately and keep it until full exit.
        self.root.after(10, self._ensure_tray_icon)
        # The title-bar close button keeps the always-listening assistant
        # running in the tray. The minimize button remains a normal taskbar
        # minimize; the tray menu owns the explicit exit action.
        self.root.protocol("WM_DELETE_WINDOW", self._minimize_to_tray)
        self.root.bind("<Unmap>", self._prepare_window_restore, add="+")
        self.root.bind("<Map>", self._complete_window_restore, add="+")

    def _start_background_services(self, warmup: bool) -> None:
        if warmup and self.preload_enabled.get():
            # Do not claim to be listening while ASR/TTS are still loading.
            # The listener starts only after warmup finishes or is cancelled.
            self._set_preload_loading(True)
            self.root.after(100, self._start_tts_warmup)
            return
        if self.wake_capture_enabled.get() and is_skill_enabled(self.config, "wake-word"):
            self.root.after(100, self.wake_listener.start)
        if self.tts.backend == "mimo-api":
            self.root.after(150, self._start_api_notice_preparation)

    def _agent_runtime_loop(self) -> None:
        """Prepare and reuse the text-agent runtime on one background thread."""
        try:
            self._ensure_agent_runtime()
        except Exception as exc:
            print(f"[Agent] 后台初始化延后：{exc}", file=sys.stderr)
        while True:
            task = self._agent_task_queue.get()
            if task is None:
                self._close_agent_runtime()
                return
            self._agent_worker(*task)

    def _ensure_agent_runtime(self):
        if (
            self._agent_runtime_agent is not None
            and self._agent_runtime_database is not None
            and self._agent_runtime_session_id == self.session_id
        ):
            return self._agent_runtime_agent, self._agent_runtime_database
        self._close_agent_runtime()
        agent, database, _tts = build_agent(
            self.config,
            self.session_id,
            confirmation_callback=lambda _name, _arguments: False,
            progress_callback=None,
            cancel_event=threading.Event(),
            include_tts=False,
        )
        self._agent_runtime_agent = agent
        self._agent_runtime_database = database
        self._agent_runtime_session_id = self.session_id
        return agent, database

    def _close_agent_runtime(self) -> None:
        agent = self._agent_runtime_agent
        database = self._agent_runtime_database
        self._agent_runtime_agent = None
        self._agent_runtime_database = None
        self._agent_runtime_session_id = ""
        if agent is not None:
            agent.close()
        if database is not None:
            database.close()

    def _start_api_notice_preparation(self) -> None:
        """Cache two API notices without enabling or retaining full preload."""
        def prepare() -> None:
            try:
                self._prepare_api_notice_clips()
            except Exception as exc:
                print(f"[API] 连接提示缓存准备失败：{exc}", file=sys.stderr)
            finally:
                if not self.preload_enabled.get():
                    self.tts.close()

        threading.Thread(target=prepare, daemon=True).start()

    def _poll_scheduled_tasks(self) -> None:
        if self.closing:
            return
        try:
            if (
                self._scheduled_task_running
                or self.busy
                or self.recorder.is_recording
                or not is_skill_enabled(self.config, "scheduled-tasks")
            ):
                return
            due = self._ui_database.due_scheduled_tasks(limit=1)
            if not due:
                return
            task = self._ui_database.claim_scheduled_task(int(due[0]["id"]))
            if task is None:
                return
            self._scheduled_task_running = True
            self._active_scheduled_task_id = int(task["id"])
            self._scheduled_task_cancel_event = threading.Event()
            threading.Thread(
                target=self._scheduled_task_worker,
                args=(task, self._scheduled_task_cancel_event),
                daemon=True,
            ).start()
        except Exception as exc:
            print(f"[定时任务] 调度失败：{exc}", file=sys.stderr)
        finally:
            if not self.closing:
                self.root.after(1500, self._poll_scheduled_tasks)

    def _scheduled_task_worker(
        self,
        task: dict,
        cancel_event: threading.Event,
    ) -> None:
        task_id = int(task["id"])
        command = str(task["command"])
        silent = bool(task["silent"])
        agent = database = temporary_tts = None
        success = False
        result = ""
        try:
            # A background schedule cannot ask a human to approve a new risky
            # action. Low-risk local skills do not call this callback.
            agent, database, temporary_tts = build_agent(
                self.config,
                f"scheduled-{task_id}",
                confirmation_callback=lambda _name, _arguments: False,
                progress_callback=None,
                cancel_event=cancel_event,
                include_tts=False,
            )
            answer = agent.run(command, input_mode="text")
            result = answer.text.strip() or "操作已完成"
            success = not bool(
                re.search(r"失败|无法|未找到|已取消|拒绝|错误|没有执行", result)
            )
        except OperationCancelled:
            result = "已由用户暂停"
        except Exception as exc:
            result = f"{type(exc).__name__}: {exc}"
        finally:
            if temporary_tts is not None:
                temporary_tts.close()
            if agent is not None:
                agent.close()
            if database is not None:
                database.close()
            log_database = Database(app_paths().database)
            try:
                log_database.complete_scheduled_task(task_id, success, result)
            finally:
                log_database.close()
            if not self.closing:
                self.root.after(
                    0,
                    self._finish_scheduled_task,
                    task_id,
                    command,
                    silent,
                    success,
                    result,
                )

    def _finish_scheduled_task(
        self,
        task_id: int,
        command: str,
        silent: bool,
        success: bool,
        result: str,
    ) -> None:
        self._scheduled_task_running = False
        self._active_scheduled_task_id = 0
        if not silent:
            state = "已完成" if success else "执行失败"
            self._append_chat(
                "system",
                f"定时任务 #{task_id} {state}：{command}\n{result}",
            )
        refresh = getattr(self, "_refresh_schedule_manager", None)
        if callable(refresh):
            refresh()

    def _minimize_to_tray(self) -> None:
        if self.closing:
            return
        self._ensure_tray_icon()
        if self._tray_icon is None:
            self.root.iconify()
            return
        self._set_window_transparency(0.0)
        self._window_restore_pending = True
        self.root.withdraw()
        self.status.set("猫猫正在系统托盘后台运行")

    def _set_window_transparency(self, value: float) -> None:
        try:
            self.root.attributes("-alpha", value)
        except tk.TclError:
            pass

    def _prepare_window_restore(self, event=None) -> None:
        if self.closing or (event is not None and event.widget is not self.root):
            return
        try:
            minimized = self.root.state() in {"iconic", "withdrawn"}
        except tk.TclError:
            return
        if minimized:
            self._window_restore_pending = True
            self._set_window_transparency(0.0)

    def _complete_window_restore(self, event=None) -> None:
        if (
            self.closing
            or not self._window_restore_pending
            or (event is not None and event.widget is not self.root)
        ):
            return
        if self._window_restore_job is not None:
            try:
                self.root.after_cancel(self._window_restore_job)
            except tk.TclError:
                pass
        self.root.update_idletasks()
        self._window_restore_job = self.root.after(32, self._show_restored_window)

    def _show_restored_window(self) -> None:
        self._window_restore_job = None
        if self.closing:
            return
        self.root.update_idletasks()
        self._set_window_transparency(1.0)
        self._window_restore_pending = False

    def _ensure_tray_icon(self) -> None:
        if self.closing or self._tray_icon is not None:
            return
        try:
            import pystray
            from PIL import Image

            if not self._tray_icon_path.is_file():
                raise FileNotFoundError("miao.jpg")
            image = Image.open(self._tray_icon_path).convert("RGBA")
            menu = pystray.Menu(
                pystray.MenuItem(
                    "显示猫猫",
                    lambda _icon, _item: self.root.after(0, self._restore_from_tray),
                    default=True,
                ),
                pystray.MenuItem(
                    "重启猫猫",
                    lambda _icon, _item: self.root.after(0, self._restart_app),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "退出猫猫",
                    lambda _icon, _item: self.root.after(0, self._on_close),
                ),
            )
            self._tray_icon = pystray.Icon("maomao", image, "猫猫", menu)
            self._tray_icon.run_detached()
        except Exception as exc:
            self._tray_icon = None
            self.status.set(f"系统托盘初始化失败：{exc}")

    def _restore_from_tray(self) -> None:
        if self.closing:
            return
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass
        self.status.set("就绪")

    def _stop_tray_icon(self) -> None:
        tray_icon = self._tray_icon
        self._tray_icon = None
        if tray_icon is not None:
            try:
                tray_icon.stop()
            except Exception:
                pass

    def _restart_app(self) -> None:
        if self.closing:
            return
        if self.busy and not messagebox.askyesno(
            "重启猫猫", "当前仍有任务执行中，确定重启吗？", parent=self.root
        ):
            return

        project_root = Path(__file__).resolve().parent.parent
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "assistant_app.restart_helper",
                    str(os.getpid()),
                ],
                cwd=str(project_root),
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            messagebox.showerror(
                "无法重启", f"启动重启程序失败：{exc}", parent=self.root
            )
            return

        self._begin_shutdown("正在重启猫猫…")

    def _build_window(self) -> None:
        self.root.title("猫猫")
        self.root.geometry("1020x780")
        self.root.minsize(860, 640)

        background = "#F5F5F7"
        card = "#FFFFFF"
        text = "#1D1D1F"
        secondary = "#6E6E73"
        border = "#E5E5EA"
        blue = "#007AFF"
        self.root.configure(bg=background)

        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(
            "Primary.TButton",
            background=blue,
            foreground="#FFFFFF",
            borderwidth=0,
            focusthickness=0,
            padding=(18, 10),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#1687FF"), ("pressed", "#0062CC"), ("disabled", "#A8CFFF")],
        )
        style.configure(
            "Secondary.TButton",
            background="#F0F0F2",
            foreground=text,
            borderwidth=0,
            focusthickness=0,
            padding=(14, 9),
            font=("Microsoft YaHei UI", 9),
        )
        style.map("Secondary.TButton", background=[("active", "#E5E5EA"), ("pressed", "#D9D9DE")])
        style.configure(
            "Apple.TCombobox",
            fieldbackground="#F0F0F2",
            background="#F0F0F2",
            foreground=text,
            bordercolor="#F0F0F2",
            lightcolor="#F0F0F2",
            darkcolor="#F0F0F2",
            arrowsize=14,
            padding=(8, 6),
        )

        outer = tk.Frame(self.root, bg=background, padx=24, pady=20)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        header = tk.Frame(outer, bg=background)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(
            header,
            text="喵~",
            bg=background,
            fg=text,
            font=("Microsoft YaHei UI", 22, "bold"),
        ).pack(side="left")
        tk.Label(
            header,
            textvariable=self.usage,
            bg="#E9E9EB",
            fg=secondary,
            padx=12,
            pady=6,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="right")

        subtitle = "Qwen3-ASR · Kimi K3"
        tk.Label(
            outer,
            text=subtitle,
            bg=background,
            fg=secondary,
            font=("Microsoft YaHei UI", 9),
        ).grid(
            row=1, column=0, sticky="w", pady=(2, 14)
        )

        chat_card = tk.Frame(
            outer,
            bg=card,
            highlightthickness=1,
            highlightbackground=border,
            padx=4,
            pady=4,
        )
        chat_card.grid(row=2, column=0, sticky="nsew")
        chat_card.rowconfigure(1, weight=1)
        chat_card.columnconfigure(0, weight=1)
        chat_toolbar = tk.Frame(chat_card, bg=card)
        chat_toolbar.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 0))
        self.clear_chat_button = ttk.Button(
            chat_toolbar,
            text="清除记录",
            command=self._clear_conversation,
            style="Secondary.TButton",
        )
        self.clear_chat_button.pack(side="right")
        self.chat = scrolledtext.ScrolledText(
            chat_card,
            wrap="word",
            state="disabled",
            font=("Microsoft YaHei UI", 10),
            bg=card,
            fg=text,
            insertbackground=text,
            selectbackground="#D6E9FF",
            padx=18,
            pady=16,
            relief="flat",
            borderwidth=0,
        )
        self.chat.grid(row=1, column=0, sticky="nsew")
        self.chat.tag_configure("user", foreground=blue, spacing1=10, spacing3=10, font=("Microsoft YaHei UI", 10, "bold"))
        self.chat.tag_configure("assistant", foreground=text, spacing1=10, spacing3=10)
        self.chat.tag_configure("system", foreground=secondary, spacing1=8, spacing3=8)
        self.chat.tag_configure("meta", foreground="#8E8E93", font=("Microsoft YaHei UI", 8))

        controls_card = tk.Frame(
            outer,
            bg=card,
            highlightthickness=1,
            highlightbackground=border,
            padx=16,
            pady=13,
        )
        controls_card.grid(row=3, column=0, sticky="ew", pady=(14, 10))
        voice_bar = tk.Frame(controls_card, bg=card)
        voice_bar.pack(fill="x")
        self.record_button = ttk.Button(
            voice_bar,
            text="●  开始录音",
            command=self._toggle_recording,
            style="Primary.TButton",
        )
        self.record_button.pack(side="left")

        voice_group = tk.Frame(voice_bar, bg=card)
        voice_group.pack(side="left", padx=(18, 0))
        tk.Label(voice_group, text="音色", bg=card, fg=secondary, font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        self.voice_combo = ttk.Combobox(
            voice_group,
            textvariable=self.speaker,
            values=self.tts.available_voices(),
            state="readonly",
            width=11,
            style="Apple.TCombobox",
        )
        self.voice_combo.pack(anchor="w")
        self.voice_combo.bind("<<ComboboxSelected>>", self._change_voice)

        quality_group = tk.Frame(voice_bar, bg=card)
        quality_group.pack(side="left", padx=(12, 0))
        tk.Label(quality_group, text="语音生成", bg=card, fg=secondary, font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        self.mode_combo = ttk.Combobox(
            quality_group,
            textvariable=self.tts_mode,
            values=self.tts.available_engines(),
            state="readonly",
            width=30,
            style="Apple.TCombobox",
        )
        self.mode_combo.pack(anchor="w")
        self.mode_combo.bind("<<ComboboxSelected>>", self._change_tts_mode)

        delivery_group = tk.Frame(voice_bar, bg=card)
        delivery_group.pack(side="left", padx=(12, 0))
        tk.Label(delivery_group, text="播报方式", bg=card, fg=secondary, font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        self.delivery_combo = ttk.Combobox(
            delivery_group,
            textvariable=self.delivery_mode,
            values=["快速分句", "连贯朗读"],
            state="readonly",
            width=10,
            style="Apple.TCombobox",
        )
        self.delivery_combo.pack(anchor="w")

        playback_bar = tk.Frame(controls_card, bg=card)
        playback_bar.pack(fill="x", pady=(11, 0))
        tk.Label(
            playback_bar,
            text="播放控制",
            bg=card,
            fg=secondary,
            font=("Microsoft YaHei UI", 8),
        ).pack(side="left", padx=(0, 10))
        ttk.Button(
            playback_bar,
            text="暂停",
            command=lambda: self._tts_control("pause"),
            style="Secondary.TButton",
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            playback_bar,
            text="继续",
            command=lambda: self._tts_control("resume"),
            style="Secondary.TButton",
        ).pack(side="left", padx=6)
        ttk.Button(
            playback_bar,
            text="停止",
            command=lambda: self._tts_control("stop"),
            style="Secondary.TButton",
        ).pack(side="left", padx=(6, 0))
        self.interrupt_button = ttk.Button(
            playback_bar,
            text="暂停全部",
            command=self._pause_all_operations,
            style="Secondary.TButton",
        )
        self.interrupt_button.pack(side="right")

        tk.Frame(controls_card, bg=border, height=1).pack(fill="x", pady=(11, 9))
        status_bar = tk.Frame(controls_card, bg=card)
        status_bar.pack(fill="x")
        tk.Label(status_bar, text="●", bg=card, fg="#34C759", font=("Segoe UI", 9)).pack(side="left")
        tk.Label(status_bar, textvariable=self.status, bg=card, fg=secondary, font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(5, 0))
        tk.Label(status_bar, textvariable=self.timing, bg=card, fg="#8E8E93", font=("Microsoft YaHei UI", 9)).pack(side="right")

        compose = tk.Frame(
            outer,
            bg=card,
            highlightthickness=1,
            highlightbackground=border,
            padx=12,
            pady=12,
        )
        compose.grid(row=4, column=0, sticky="ew")
        compose.columnconfigure(0, weight=1)
        self.input_box = tk.Text(
            compose,
            height=4,
            wrap="word",
            font=("Microsoft YaHei UI", 10),
            bg=card,
            fg=text,
            insertbackground=text,
            selectbackground="#D6E9FF",
            relief="flat",
            borderwidth=0,
            padx=6,
            pady=6,
        )
        self.input_box.grid(row=0, column=0, sticky="nsew")
        self.input_box.bind("<Control-Return>", self._send_shortcut)

        actions = tk.Frame(compose, bg=card)
        actions.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        self.send_button = ttk.Button(
            actions,
            text="发送",
            command=self._send_text,
            style="Primary.TButton",
        )
        self.send_button.pack(fill="x", pady=(10, 0))
        tk.Label(
            actions,
            text="Ctrl + Enter",
            bg=card,
            fg="#8E8E93",
            font=("Segoe UI", 8),
        ).pack(pady=(7, 0))

    def _append_chat(self, role: str, text: str, meta: str = "") -> None:
        labels = {"user": "你", "assistant": "猫猫", "system": "系统"}
        self.chat.configure(state="normal")
        self.chat.insert("end", f"{labels[role]}：", role)
        self.chat.insert("end", f"{text}\n", role)
        if meta:
            self.chat.insert("end", f"    {meta}\n", "meta")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def _clear_conversation(self) -> None:
        if not messagebox.askyesno(
            "清除对话记录",
            "确定清空当前会话吗？\n\n长期记忆、应用白名单、费用和操作日志不会被删除。",
            parent=self.root,
        ):
            return
        if self.busy or self.recorder.is_recording:
            self._pause_all_operations()
        old_session = self.session_id
        self._ui_database.clear_messages(old_session)
        self.session_id = uuid.uuid4().hex
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        self.status.set("当前对话记录已清除")
        self.timing.set("本次处理 · 等待任务")

    def _set_busy(self, value: bool, status: str) -> None:
        self.busy = value
        self.status.set(status)
        state = "disabled" if value else "normal"
        self.send_button.configure(state=state)
        if not self.recorder.is_recording:
            self.record_button.configure(state=state)

    @staticmethod
    def _timing_summary(
        asr_seconds: float | None = None,
        llm_seconds: float | None = None,
        first_audio_seconds: float | None = None,
        total_seconds: float | None = None,
    ) -> str:
        parts: list[str] = []
        if asr_seconds is not None:
            parts.append(f"识别 {asr_seconds:.2f}s")
        if llm_seconds is not None:
            parts.append(f"回答 {llm_seconds:.2f}s")
        if first_audio_seconds is not None:
            parts.append(f"首段语音 {first_audio_seconds:.2f}s")
        if total_seconds is not None:
            parts.append(f"总处理 {total_seconds:.2f}s")
        return "本次处理 · " + (" · ".join(parts) if parts else "计时中…")

    def _send_shortcut(self, _: tk.Event) -> str:
        self._send_text()
        return "break"

    def _change_model_mode(self, value: str) -> None:
        mode = MODEL_MODE_LABELS.get(value, "auto")
        self.model_mode.set(value if value in MODEL_MODE_LABELS else "自动（Kimi 优先）")
        self.config["routing"]["model_mode"] = mode
        save_local_setting("routing", "model_mode", mode)
        descriptions = {
            "auto": "文本模型：自动分流，优先 Kimi，失败时回退 MiMo",
            "k2.6": "文本模型：固定 Kimi K2.6",
            "k3": "文本模型：固定 Kimi K3",
            "mimo-pro": "文本模型：固定 MiMo V2.5 Pro",
            "mimo-omni": "文本模型：固定 MiMo V2.5",
        }
        self.status.set(descriptions[mode])

    def _change_asr_mode(self, value: str) -> None:
        backend = ASR_MODE_LABELS.get(value)
        if backend is None or backend == self.transcriber.backend:
            return
        if self.busy or self.recorder.is_recording:
            current = next(
                label for label, name in ASR_MODE_LABELS.items() if name == self.transcriber.backend
            )
            self.asr_mode.set(current)
            self.status.set("请等待当前语音任务结束后再切换语音转写")
            return
        previous = self.transcriber.backend
        preloading = self.preload_enabled.get()
        if preloading:
            self._set_preload_loading(True)
            self.root.update_idletasks()
            self.wake_listener.pause(wait=True)
        self.status.set(f"正在切换到 {value}…")
        self.asr_selector.configure(state="disabled")

        def switch() -> None:
            try:
                self.transcriber.set_backend(backend)
                if self.preload_enabled.get():
                    self.transcriber.warmup()
                self.config["audio"]["backend"] = backend
                save_local_setting("audio", "backend", backend)
                if not self.closing:
                    self.root.after(0, finish, None)
            except Exception as exc:
                self.transcriber.set_backend(previous)
                if not self.closing:
                    self.root.after(0, finish, exc)

        def finish(error: Exception | None) -> None:
            if preloading:
                self._set_preload_loading(False)
                self.wake_listener.resume()
            self.asr_selector.configure(state="normal")
            current_label = next(
                label for label, name in ASR_MODE_LABELS.items() if name == self.transcriber.backend
            )
            self.asr_mode.set(current_label)
            self.status.set(
                f"已切换到 {current_label}" if error is None else f"切换语音转写失败：{error}"
            )

        threading.Thread(target=switch, daemon=True).start()

    def _refresh_continuous_button(self) -> None:
        if hasattr(self, "continuous_button"):
            enabled = self.continuous_enabled.get()
            self.continuous_button.configure(
                text="连续：开" if enabled else "连续：关",
                fg_color="#E8F2FF" if enabled else "#F0F0F3",
                text_color="#007AFF" if enabled else "#6E6E73",
            )

    def _toggle_continuous(self) -> None:
        enabled = not self.continuous_enabled.get()
        self.continuous_enabled.set(enabled)
        self.config.setdefault("conversation", {})["continuous"] = enabled
        self.config.setdefault("skills", {})["continuous-conversation"] = enabled
        save_local_settings(
            {
                "conversation": {"continuous": enabled},
                "skills": {"continuous-conversation": enabled},
            }
        )
        self._sync_skill_switch("continuous-conversation", enabled)
        self._refresh_continuous_button()
        if not enabled and self._continuous_listening and self.recorder.is_recording:
            self._wake_sequence += 1
            self.recorder.cancel()
            self._continuous_listening = False
            self.wake_listener.resume()
            self.record_button.configure(text="●  开始录音", state="normal")
            self.send_button.configure(state="normal")
        if not enabled:
            self._continuous_session_active = False
        self.status.set("连续对话已开启" if enabled else "连续对话已关闭")

    def _send_text(self) -> None:
        if self.busy:
            return
        text = self.input_box.get("1.0", "end").strip()
        if not text:
            return
        self.input_box.delete("1.0", "end")
        self._submit(text, "text")

    def _submit(
        self,
        text: str,
        input_mode: str,
        task_started: float | None = None,
        asr_seconds: float | None = None,
    ) -> None:
        self._task_cancel_event.set()
        cancel_event = threading.Event()
        self._task_cancel_event = cancel_event
        task_started = task_started or time.perf_counter()
        self._active_task_started = task_started
        self._append_chat("user", text, "语音转写" if input_mode == "voice" else "")
        self._set_busy(True, "正在思考…")
        self.timing.set(self._timing_summary(asr_seconds=asr_seconds))
        self._action_notice_task_started = 0.0
        self._last_progress_speech_at = time.monotonic()
        self._thinking_progress_task_started = task_started
        self._thinking_progress_count = 0
        self._thinking_action_context = ""
        self._thinking_filler_stop.set()
        thinking_stop = threading.Event()
        self._thinking_filler_stop = thinking_stop
        if input_mode == "voice" and self.thinking_filler_texts:
            threading.Thread(
                target=self._thinking_filler_worker,
                args=(task_started, thinking_stop),
                daemon=True,
            ).start()
        self._agent_task_queue.put(
            (
                text,
                input_mode,
                task_started,
                asr_seconds,
                cancel_event,
                thinking_stop,
            )
        )

    def _thinking_filler_worker(
        self,
        task_started: float,
        stop_event: threading.Event,
    ) -> None:
        """Play prepared, voice-matched fillers only when a voice task is slow."""
        first_texts = list(
            getattr(self, "thinking_first_filler_texts", self.thinking_filler_texts[:1])
        )
        followup_texts = list(
            getattr(self, "thinking_followup_texts", self.thinking_filler_texts[1:])
        )
        if not followup_texts:
            followup_texts = [
                value for value in self.thinking_filler_texts
                if value not in {"我看看。", "我先看看。", "我看一下。"}
            ]
        random.shuffle(first_texts)
        random.shuffle(followup_texts)
        maximum = self.thinking_filler_max_count
        for position in range(maximum):
            while True:
                if stop_event.is_set():
                    return
                action_notice_task = getattr(self, "_action_notice_task_started", 0.0)
                action_notice_done = getattr(self, "_action_notice_done", None)
                if (
                    action_notice_task == task_started
                    and action_notice_done is not None
                    and not action_notice_done.is_set()
                ):
                    if stop_event.wait(0.05):
                        return
                    continue
                remaining = self.thinking_filler_interval - (
                    time.monotonic() - self._last_progress_speech_at
                )
                if remaining <= 0:
                    break
                if stop_event.wait(min(0.10, remaining)):
                    return
            if (
                self.closing
                or self._active_task_started != task_started
                or stop_event.is_set()
            ):
                return
            if position == self.thinking_long_wait_after_count:
                context = self._thinking_context_kind(self._thinking_action_context)
                text = self.thinking_long_wait_texts.get(
                    context,
                    self.thinking_long_wait_texts.get(
                        "general",
                        "这个任务还需要一点时间。你可以先忙，处理好了我叫你。",
                    ),
                )
                clip_name = f"thinking-long-{context}"
            else:
                pool = first_texts if position == 0 else followup_texts
                if not pool:
                    return
                text = pool[position % len(pool)]
                try:
                    index = self.thinking_filler_texts.index(text)
                except ValueError:
                    index = position
                clip_name = f"thinking-filler-{index + 1:02d}"
            clip = self._thinking_voice_clip(
                text,
                clip_name,
                stop_event,
            )
            if clip is None or stop_event.is_set():
                return
            self._thinking_audio_done.clear()
            if not self.closing:
                self.root.after(0, self.status.set, "还在处理…")
            try:
                play_wav_file(clip)
            except Exception as exc:
                print(f"[TTS] 思考提示播放失败：{exc}", file=sys.stderr)
                return
            finally:
                self._thinking_audio_done.set()
                self._last_progress_speech_at = time.monotonic()
                if self._thinking_progress_task_started == task_started:
                    self._thinking_progress_count += 1

    @staticmethod
    def _thinking_context_kind(message: str) -> str:
        value = str(message).lower()
        if any(marker in value for marker in ("屏幕", "截图", "画面", "图像")):
            return "screen"
        if any(marker in value for marker in ("查", "搜索", "网页", "网站", "chatgpt", "资料")):
            return "web"
        if any(marker in value for marker in ("执行", "操作", "打开", "启动", "点击")):
            return "action"
        return "general"

    def _thinking_voice_clip(
        self,
        text: str,
        clip_name: str,
        stop_event: threading.Event | None = None,
    ) -> Path | None:
        """Reuse or generate a padded cue without cancelling the active task."""
        clip = self.tts.cached_voice_clip_path(self.goodbye_cache_dir, clip_name)
        if clip.is_file() and self.tts._cached_clip_is_audible(clip):
            return clip
        if stop_event is not None and stop_event.is_set():
            return None
        try:
            generated = self.tts.synthesize_cached_voice_clip(
                text,
                self.goodbye_cache_dir,
                clip_name,
                leading_silence_ms=320,
                trailing_silence_ms=420,
            )
        except Exception as exc:
            print(f"[TTS] 进度提示生成失败：{exc}", file=sys.stderr)
            return None
        return generated

    def _agent_worker(
        self,
        text: str,
        input_mode: str,
        task_started: float,
        asr_seconds: float | None,
        cancel_event: threading.Event,
        thinking_stop: threading.Event,
    ) -> None:
        try:
            agent, _database = self._ensure_agent_runtime()
            agent.progress_callback = lambda message: self._announce_action(
                message, task_started
            )
            agent.cancel_event = cancel_event
            agent.tools.confirmation_callback = self._confirm_tool
            agent.tools.cancel_event = cancel_event
            llm_started = time.perf_counter()
            answer = agent.run(text, input_mode=input_mode)
            thinking_stop.set()
            llm_seconds = time.perf_counter() - llm_started
            if not self.closing:
                self.root.after(
                    0,
                    self._show_answer,
                    answer,
                    input_mode,
                    task_started,
                    asr_seconds,
                    llm_seconds,
                )
        except OperationCancelled:
            thinking_stop.set()
            pass
        except BudgetExceeded as exc:
            thinking_stop.set()
            self._show_worker_error(f"预算限制：{exc}", task_started)
        except Exception as exc:
            thinking_stop.set()
            self._show_worker_error(f"任务失败：{type(exc).__name__}: {exc}", task_started)
        finally:
            thinking_stop.set()
            agent = self._agent_runtime_agent
            if agent is not None:
                agent.tools.close_research_browser()
                agent.tools.cleanup_temporary_screenshots()

    def _announce_action(self, message: str, task_started: float) -> None:
        """Show an action notice and start speaking it without delaying the tool."""
        if self.closing or self._active_task_started != task_started:
            return
        self._action_notice_task_started = task_started
        self._thinking_action_context = message
        self._action_notice_done.clear()
        self.root.after(0, self._show_action_notice, message, task_started)
        threading.Thread(
            target=self._speak_action_notice,
            args=(message, task_started),
            daemon=True,
        ).start()

    def _speak_action_notice(self, message: str, task_started: float) -> None:
        if self.closing or self._active_task_started != task_started:
            return
        try:
            self._thinking_audio_done.wait(timeout=5.0)
            self.tts.speak(message)
        except Exception as exc:
            if not self.closing and self._active_task_started == task_started:
                self.root.after(0, self.status.set, f"动作提示播报失败：{exc}")
        finally:
            self._last_progress_speech_at = time.monotonic()
            self._action_notice_done.set()

    def _show_action_notice(self, message: str, task_started: float) -> None:
        if self.closing or self._active_task_started != task_started:
            return
        self._append_chat("assistant", message, "正在执行")
        self.status.set("正在说明并同时执行操作…")

    def _show_worker_error(self, message: str, task_started: float | None = None) -> None:
        if not self.closing:
            self.root.after(0, self._finish_with_error, message, task_started)

    def _finish_with_error(self, message: str, task_started: float | None = None) -> None:
        if task_started is not None and self._active_task_started != task_started:
            return
        self._append_chat("system", message)
        self._set_busy(False, "就绪")

    def _show_answer(
        self,
        answer,
        input_mode: str,
        task_started: float,
        asr_seconds: float | None,
        llm_seconds: float,
    ) -> None:
        if self._active_task_started != task_started:
            return
        if answer.silent:
            self._set_busy(False, "操作已执行 · 提示播报结束后继续监听")
            self.timing.set(
                self._timing_summary(
                    asr_seconds=asr_seconds,
                    llm_seconds=llm_seconds,
                    total_seconds=time.perf_counter() - task_started,
                )
            )
            self._refresh_usage()

            def wait_for_notice() -> None:
                self._action_notice_done.wait(timeout=60.0)
                if not self.closing:
                    self.root.after(
                        0,
                        self._finish_silent_action,
                        input_mode,
                        task_started,
                        answer.continue_listening,
                    )

            threading.Thread(target=wait_for_notice, daemon=True).start()
            return
        reason = "、".join(answer.route.reasons)
        meta = (
            f"{answer.route.model}/{answer.route.reasoning} · {reason} · "
            f"本任务 {self.config['api']['currency_symbol']}{answer.task_cost:.2f} · "
            f"回答 {llm_seconds:.2f}s"
        )
        self._append_chat("assistant", answer.text, meta)
        self._set_busy(False, "正在生成首段语音…")
        self.timing.set(
            self._timing_summary(asr_seconds=asr_seconds, llm_seconds=llm_seconds)
            + " · 语音生成中…"
        )
        self._refresh_usage()
        delivery = "coherent"
        threading.Thread(
            target=self._speak_worker,
            args=(answer.text, delivery, input_mode, task_started, asr_seconds, llm_seconds),
            daemon=True,
        ).start()

    def _finish_silent_action(
        self,
        input_mode: str,
        task_started: float,
        continue_listening: bool = True,
    ) -> None:
        if self.closing or self._active_task_started != task_started:
            return
        self.status.set("操作已完成")
        if (
            input_mode == "voice"
            and self.continuous_enabled.get()
            and continue_listening
        ):
            self._start_continuous_listening()
        elif input_mode == "voice":
            self._continuous_session_active = False

    def _speak_worker(
        self,
        text: str,
        delivery: str,
        input_mode: str,
        task_started: float,
        asr_seconds: float | None,
        llm_seconds: float,
    ) -> None:
        try:
            self._thinking_audio_done.wait(timeout=5.0)
            self._action_notice_done.wait(timeout=60.0)
            if self.closing or self._active_task_started != task_started:
                return
            should_announce_completion = (
                self._action_notice_task_started == task_started
                or (
                    self._thinking_progress_task_started == task_started
                    and self._thinking_progress_count > 0
                )
            )
            if should_announce_completion and self.thinking_completion_texts:
                completion_index = random.randrange(len(self.thinking_completion_texts))
                completion_clip = self._thinking_voice_clip(
                    self.thinking_completion_texts[completion_index],
                    f"thinking-complete-{completion_index + 1:02d}",
                )
                if completion_clip is not None:
                    play_wav_file(completion_clip)
            metrics = self.tts.speak(text, delivery_mode=delivery)
            if not self.closing:
                self.root.after(
                    0,
                    self._finish_speech,
                    input_mode,
                    task_started,
                    asr_seconds,
                    llm_seconds,
                    metrics,
                )
        except Exception as exc:
            if not self.closing and self._active_task_started == task_started:
                self.root.after(0, self.status.set, f"播报失败：{exc}")

    def _finish_speech(
        self,
        input_mode: str,
        task_started: float,
        asr_seconds: float | None,
        llm_seconds: float,
        metrics: dict[str, float | int | str],
    ) -> None:
        if self._active_task_started != task_started:
            return
        total = time.perf_counter() - task_started
        if metrics.get("fallback_engine"):
            reason = str(metrics.get("fallback_reason", "未知原因"))
            engine = str(metrics.get("engine", "TTS"))
            self.status.set(f"{engine} 异常，已回退 Windows 语音：{reason[:80]}")
        else:
            self.status.set(f"就绪 · 已生成 {int(metrics['chunks'])} 段语音")
        self.timing.set(
            self._timing_summary(
                asr_seconds=asr_seconds,
                llm_seconds=llm_seconds,
                first_audio_seconds=float(metrics["first_audio_seconds"]),
                total_seconds=total,
            )
        )
        should_continue = (
            input_mode == "voice"
            and self.continuous_enabled.get()
            and not self._skip_next_continuous
        )
        self._skip_next_continuous = False
        if should_continue:
            self._start_continuous_listening()
        elif input_mode == "voice":
            self._continuous_session_active = False

    def _start_continuous_listening(self) -> None:
        if self.closing or self.busy or self.recorder.is_recording:
            return
        self._wake_sequence += 1
        sequence = self._wake_sequence
        self._continuous_session_active = True
        self.wake_listener.pause(wait=True)
        self._begin_wake_recording(sequence, continuous=True)

    def _set_wake_status(self, value: str) -> None:
        self._wake_status_value = value
        if (
            self.wake_capture_enabled.get()
            and not self.closing
            and not self.busy
            and not self.recorder.is_recording
        ):
            self.status.set(value)

    def _refresh_wake_capture_button(self) -> None:
        button = getattr(self, "wake_capture_button", None)
        if button is None:
            return
        button.configure(
            text="唤醒监听：开" if self.wake_capture_enabled.get() else "唤醒监听：关"
        )

    def _toggle_wake_capture(self) -> None:
        enabled = not self.wake_capture_enabled.get()
        self.wake_capture_enabled.set(enabled)
        self.wake_listener.set_capture_enabled(enabled)
        self.config.setdefault("wake_word", {})["capture_enabled"] = enabled
        self.config.setdefault("skills", {})["wake-word"] = enabled
        save_local_settings(
            {
                "wake_word": {"capture_enabled": enabled},
                "skills": {"wake-word": enabled},
            }
        )
        self._sync_skill_switch("wake-word", enabled)
        self._refresh_wake_capture_button()
        self.status.set(
            f"唤醒监听已开启 · 正在监听“{self._wake_words_display()}”"
            if enabled
            else "唤醒监听已关闭 · 手动录音仍可使用"
        )

    def _set_skill_enabled(self, skill_id: str, enabled: bool) -> None:
        self.config.setdefault("skills", {})[skill_id] = bool(enabled)
        changes = {"skills": {skill_id: bool(enabled)}}
        if skill_id == "wake-word":
            self.wake_capture_enabled.set(bool(enabled))
            self.wake_listener.set_capture_enabled(bool(enabled), wait=not enabled)
            self.config.setdefault("wake_word", {})["capture_enabled"] = bool(enabled)
            changes["wake_word"] = {"capture_enabled": bool(enabled)}
            if enabled:
                self.wake_listener.start()
            self._refresh_wake_capture_button()
        elif skill_id == "continuous-conversation":
            self.continuous_enabled.set(bool(enabled))
            self.config.setdefault("conversation", {})["continuous"] = bool(enabled)
            changes["conversation"] = {"continuous": bool(enabled)}
            if not enabled:
                self._continuous_session_active = False
                if self._continuous_listening and self.recorder.is_recording:
                    self.recorder.cancel()
                    self._continuous_listening = False
            self._refresh_continuous_button()
        save_local_settings(changes)
        definition = next((item for item in SKILL_CATALOG if item.id == skill_id), None)
        label = definition.name if definition is not None else skill_id
        self.status.set(f"技能已{'开启' if enabled else '关闭'}：{label}")
        self._refresh_skill_count()

    def _refresh_skill_count(self) -> None:
        label = getattr(self, "_skill_count_label", None)
        if label is None:
            return
        enabled = sum(
            1 for definition in SKILL_CATALOG
            if is_skill_enabled(self.config, definition.id)
        )
        label.configure(text=f"已开启 {enabled} / 共 {len(SKILL_CATALOG)} 个")

    def _sync_skill_switch(self, skill_id: str, enabled: bool) -> None:
        variables = getattr(self, "_skill_switch_vars", {})
        variable = variables.get(skill_id) if isinstance(variables, dict) else None
        if variable is not None and bool(variable.get()) != bool(enabled):
            variable.set(bool(enabled))

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

    def _wake_words_display(self) -> str:
        return "、".join(self.wake_listener.aliases)

    def _save_wake_words(self, value: str) -> None:
        if self.busy or self.recorder.is_recording:
            messagebox.showinfo("设置唤醒词", "请等待当前任务或录音结束。", parent=self.root)
            return
        try:
            words = self._parse_wake_words(value)
        except ValueError as exc:
            messagebox.showerror("设置唤醒词", str(exc), parent=self.root)
            return
        if words == self.wake_listener.aliases:
            self.status.set(f"唤醒词未改变 · 正在监听“{self._wake_words_display()}”")
            return

        capture_enabled = self.wake_capture_enabled.get()
        self.wake_listener.close()
        self.wake_listener.update_keywords(words, clear_enrollment=True)
        self.wake_listener.set_capture_enabled(capture_enabled)
        wake_config = self.config.setdefault("wake_word", {})
        wake_config["keyword"] = words[0]
        wake_config["aliases"] = words
        save_local_settings(
            {"wake_word": {"keyword": words[0], "aliases": words}}
        )
        if (
            capture_enabled
            and is_skill_enabled(self.config, "wake-word")
            and not self._preload_loading
        ):
            self.wake_listener.start()
        if hasattr(self, "enroll_button"):
            self.enroll_button.configure(text="录制唤醒词", state="normal")
        self._wake_status_value = "请重新录制唤醒词"
        self.status.set(
            f"已设置 {len(words)} 个唤醒词：{self._wake_words_display()} · 请重新录制"
        )
        messagebox.showinfo(
            "唤醒词已保存",
            "新的唤醒词已经保存。为了保持识别准确率，请点击“录制唤醒词”，"
            "按提示为每个词重新录音。",
            parent=self.root,
        )

    def _enroll_wake_word(self) -> None:
        if self.busy or self.recorder.is_recording:
            messagebox.showinfo("录制唤醒词", "请等待当前任务或录音结束。", parent=self.root)
            return
        words = list(self.wake_listener.aliases)
        total = self.wake_enrollment_samples * len(words)
        if not messagebox.askokcancel(
            "录制唤醒词",
            f"将为 {len(words)} 个唤醒词分别录制 {self.wake_enrollment_samples} 次，共 {total} 次。\n"
            f"唤醒词：{self._wake_words_display()}\n\n每次听到提示音后清楚地说出屏幕提示的词，"
            "可以稍微改变语速、音高或距离，然后保持安静一秒。\n\n所有样本只保存在本机。",
            parent=self.root,
        ):
            return
        self._wake_sequence += 1
        self.wake_listener.pause(wait=True)
        self.record_button.configure(state="disabled")
        self.send_button.configure(state="disabled")
        if hasattr(self, "enroll_button"):
            self.enroll_button.configure(state="disabled")
        threading.Thread(target=self._wake_enrollment_worker, daemon=True).start()

    def _wake_enrollment_worker(self) -> None:
        import numpy as np

        samples: list[np.ndarray] = []
        try:
            words = list(self.wake_listener.aliases)
            total = self.wake_enrollment_samples * len(words)
            recordings = [
                word
                for word in words
                for _ in range(self.wake_enrollment_samples)
            ]
            for index, word in enumerate(recordings, start=1):
                if self.closing:
                    return
                self.root.after(0, self.status.set, f"准备录制唤醒词 {index}/{total}…")
                if hasattr(self, "enroll_button"):
                    self.root.after(
                        0,
                        lambda value=f"录制 {index}/{total}": self.enroll_button.configure(text=value),
                    )
                if sys.platform == "win32":
                    import winsound

                    winsound.Beep(880, 120)
                time.sleep(0.25)
                self.recorder.start()
                self.root.after(0, self.status.set, f"请说“{word}” · 第 {index}/{total} 次")
                heard = self.recorder.wait_for_utterance_end(
                    silence_seconds=0.9,
                    no_speech_timeout=4.5,
                    max_seconds=6.0,
                )
                if not heard:
                    self.recorder.cancel()
                    raise RuntimeError(f"第 {index} 次没有听到清晰语音，请重试。")
                path = self.recorder.stop()
                with wave.open(str(path), "rb") as source:
                    samples.append(np.frombuffer(source.readframes(source.getnframes()), dtype=np.int16).copy())
                path.unlink(missing_ok=True)
                time.sleep(0.25)
            threshold = self.wake_listener.enroll(samples)
            if not self.closing:
                self.root.after(0, self._finish_wake_enrollment, threshold)
        except Exception as exc:
            self.recorder.cancel()
            if not self.closing:
                self.root.after(0, self._fail_wake_enrollment, str(exc))

    def _finish_wake_enrollment(self, threshold: float) -> None:
        self.wake_listener.start()
        self.wake_listener.resume()
        self.record_button.configure(text="●  开始录音", state="normal")
        self.send_button.configure(state="normal")
        if hasattr(self, "enroll_button"):
            self.enroll_button.configure(text="重新录制唤醒词", state="normal")
        self._wake_status_value = f"正在监听“{self._wake_words_display()}”"
        self.status.set(
            f"{len(self.wake_listener.aliases)} 个唤醒词录制完成 · 匹配阈值 {threshold:.2f}"
        )

    def _fail_wake_enrollment(self, message: str) -> None:
        self.wake_listener.resume()
        self.record_button.configure(text="●  开始录音", state="normal")
        self.send_button.configure(state="normal")
        if hasattr(self, "enroll_button"):
            label = "重新录制唤醒词" if self.wake_listener.enrolled else "录制唤醒词"
            self.enroll_button.configure(text=label, state="normal")
        self.status.set(f"唤醒词录制失败：{message}")

    def _handle_wake_word(self) -> None:
        if not self.wake_capture_enabled.get():
            return
        if self.closing:
            return
        self._thinking_filler_stop.set()
        self._task_cancel_event.set()
        self._scheduled_task_cancel_event.set()
        self._wake_sequence += 1
        sequence = self._wake_sequence
        short_response = self._continuous_session_active
        self._continuous_session_active = True
        self._continuous_listening = False
        self._skip_next_continuous = False
        self._active_task_started = time.perf_counter()
        if self.recorder.is_recording:
            self.recorder.cancel()
        self.busy = False
        self.send_button.configure(state="disabled")
        self.record_button.configure(text="●  已唤醒", state="disabled")
        self.status.set(f"听到唤醒词 · 正在打断当前任务…")
        threading.Thread(
            target=self._wake_response_worker,
            args=(sequence, short_response),
            daemon=True,
        ).start()

    def _pause_all_operations(self) -> None:
        """Immediately interrupt the active task and return to wake standby."""
        if self.closing:
            return
        self._thinking_filler_stop.set()
        self._task_cancel_event.set()
        self._scheduled_task_cancel_event.set()
        self._active_task_started = 0.0
        self._wake_sequence += 1
        self._continuous_listening = False
        self._continuous_session_active = False
        self._skip_next_continuous = True
        if self.recorder.is_recording:
            self.recorder.cancel()
        self.busy = False
        self.record_button.configure(text="●  开始录音", state="normal")
        self.send_button.configure(state="normal")
        self.wake_listener.resume()
        self.status.set(f"所有当前操作已暂停 · 再说“{self.wake_listener.keyword}”即可开始")
        self.timing.set("本次处理 · 已由用户暂停")

        def stop_audio() -> None:
            try:
                self.tts.stop()
            except Exception:
                pass

        threading.Thread(target=stop_audio, daemon=True).start()

    def _cached_wake_response_audio(self, short_response: bool) -> Path | None:
        candidates = (
            self.interrupt_response_texts if short_response else self.wake_response_texts
        )
        index = random.randrange(len(candidates))
        prefix = "wake-interrupt" if short_response else "wake-response"
        try:
            return self.tts.synthesize_cached_voice_clip(
                candidates[index],
                self.goodbye_cache_dir,
                f"{prefix}-{index + 1:02d}",
            )
        except Exception:
            return None

    def _wake_response_worker(self, sequence: int, short_response: bool = False) -> None:
        try:
            self.tts.stop()
            response_audio = self._cached_wake_response_audio(short_response)
            available = [response_audio] if response_audio is not None else []
            if available:
                try:
                    play_wav_file(random.choice(available))
                except Exception:
                    self.tts.speak(random.choice(["嗯？", "在呢。", "你说。"]) if short_response else random.choice(["我在呢。", "怎么啦？", "听着呢。"]))
            else:
                self.tts.speak(random.choice(["嗯？", "在呢。", "你说。"]) if short_response else random.choice(["我在呢。", "怎么啦？", "听着呢。"]))
            if not self.closing:
                self.root.after(0, self._begin_wake_recording, sequence, short_response)
        except Exception as exc:
            if not self.closing:
                self.root.after(0, self._wake_response_failed, sequence, str(exc))

    def _wake_response_failed(self, sequence: int, message: str) -> None:
        if sequence != self._wake_sequence:
            return
        self.wake_listener.resume()
        self._set_busy(False, f"唤醒回应失败：{message}")

    def _begin_wake_recording(self, sequence: int, continuous: bool = False) -> None:
        if self.closing or sequence != self._wake_sequence:
            return
        try:
            self._continuous_listening = continuous
            self.recorder.start()
            self.record_button.configure(text="■  正在听你说", state="normal")
            self.send_button.configure(state="disabled")
            self.status.set("连续对话 · 正在听你说…" if continuous else "正在听你说…")
        except Exception as exc:
            self.wake_listener.resume()
            self._set_busy(False, f"唤醒录音失败：{exc}")
            return

        def monitor() -> None:
            heard_speech = self.recorder.wait_for_utterance_end(
                silence_seconds=float(self.config.get("wake_word", {}).get("silence_seconds", 1.15)),
                no_speech_timeout=float(
                    self.config.get("conversation", {}).get("followup_timeout", 6.0)
                    if continuous
                    else self.config.get("wake_word", {}).get("no_speech_timeout", 6.0)
                ),
                max_seconds=float(self.config.get("wake_word", {}).get("max_record_seconds", 30.0)),
            )
            if not self.closing:
                self.root.after(0, self._finish_wake_recording, sequence, heard_speech, continuous)

        threading.Thread(target=monitor, daemon=True).start()

    def _finish_wake_recording(
        self, sequence: int, heard_speech: bool, continuous: bool = False
    ) -> None:
        if sequence != self._wake_sequence or not self.recorder.is_recording:
            return
        if not heard_speech:
            self.recorder.cancel()
            self._continuous_listening = False
            self._continuous_session_active = False
            self.wake_listener.resume()
            self.record_button.configure(text="●  开始录音", state="normal")
            self.send_button.configure(state="normal")
            self.status.set(
                f"连续对话已结束 · 再说“{self.wake_listener.keyword}”即可唤醒"
                if continuous
                else f"没有听到指令 · 再说“{self.wake_listener.keyword}”即可唤醒"
            )
            return
        self._complete_recording()

    def _toggle_recording(self) -> None:
        if self.busy and not self.recorder.is_recording:
            return
        if not self.recorder.is_recording:
            self._wake_sequence += 1
            self._continuous_listening = False
            self._skip_next_continuous = False
            self.wake_listener.pause(wait=True)
            try:
                self.recorder.start()
                self.record_button.configure(text="■ 停止并发送")
                self.send_button.configure(state="disabled")
                self.status.set("正在录音…")
            except Exception as exc:
                self.wake_listener.resume()
                messagebox.showerror("录音失败", str(exc), parent=self.root)
            return
        self._wake_sequence += 1
        self._complete_recording()

    def _complete_recording(self) -> None:
        self._continuous_listening = False
        try:
            path = self.recorder.stop()
        except Exception as exc:
            self.wake_listener.resume()
            messagebox.showerror("录音失败", str(exc), parent=self.root)
            self.record_button.configure(text="●  开始录音")
            return
        self.wake_listener.resume()
        task_started = time.perf_counter()
        self._active_task_started = task_started
        self.timing.set(self._timing_summary())
        self.record_button.configure(text="●  开始录音", state="disabled")
        self._set_busy(True, "正在本地识别语音…")
        threading.Thread(
            target=self._transcribe_worker,
            args=(path, task_started),
            daemon=True,
        ).start()

    def _transcribe_worker(self, path, task_started: float) -> None:
        try:
            asr_started = time.perf_counter()
            text = self.transcriber.transcribe(path)
            asr_seconds = time.perf_counter() - asr_started
            if not text:
                raise RuntimeError("没有识别到有效语音。")
            if not self.closing:
                self.root.after(
                    0,
                    self._submit_transcription,
                    text,
                    task_started,
                    asr_seconds,
                )
        except APIReconnectFailed as exc:
            if self._ask_for_one_time_local_asr(str(exc)):
                try:
                    if not self.closing:
                        self.root.after(
                            0,
                            self.status.set,
                            "已确认 · 正在仅本次加载本地 Qwen3-ASR…",
                        )
                    local_started = time.perf_counter()
                    text = self.transcriber.transcribe_once_with_local(path)
                    asr_seconds = time.perf_counter() - local_started
                    if not text:
                        raise RuntimeError("本地模型也没有识别到有效语音。")
                    if not self.closing:
                        self.root.after(
                            0,
                            self._submit_transcription,
                            text,
                            task_started,
                            asr_seconds,
                        )
                except Exception as local_error:
                    self._show_worker_error(
                        f"本地语音转写失败：{local_error}", task_started
                    )
            else:
                self._show_worker_error(f"录音或识别失败：{exc}", task_started)
        except Exception as exc:
            self._show_worker_error(f"录音或识别失败：{exc}", task_started)

    def _cached_api_notice(self, clip_name: str) -> Path | None:
        clip = self.tts.cached_voice_clip_path(self.goodbye_cache_dir, clip_name)
        if clip.is_file() and self.tts._cached_clip_is_audible(clip):
            return clip
        return None

    def _announce_api_reconnect(self, service: str) -> None:
        if self.closing:
            return
        self.root.after(0, self.status.set, f"{service} API 连接失效 · 正在重新连接…")
        clip = self._cached_api_notice("api-reconnecting")
        if clip is not None:
            try:
                play_wav_file(clip)
            except Exception as exc:
                print(f"[API] 重连提示播放失败：{exc}", file=sys.stderr)

    def _ask_for_one_time_local_asr(self, error: str) -> bool:
        """Never construct Qwen before this explicit user decision."""
        if self.closing:
            return False
        self.wake_listener.pause(wait=True)
        clip = self._cached_api_notice("api-unavailable")
        if clip is not None:
            try:
                play_wav_file(clip)
            except Exception as exc:
                print(f"[API] 连接失败提示播放失败：{exc}", file=sys.stderr)
        if self.beta_channel:
            self.root.after(
                0,
                lambda: messagebox.showerror(
                    "API 无法连接",
                    "MiMo 语音转写重新连接后仍然失败。MaoMao beta 没有附带本地 Qwen3-ASR。",
                    parent=self.root,
                ),
            )
            return False
        event = threading.Event()
        dialog_ready = threading.Event()
        decision: list[bool] = []
        finish_callbacks: list = []

        def show_dialog() -> None:
            dialog = tk.Toplevel(self.root)
            dialog.title("API 无法连接")
            dialog.transient(self.root)
            dialog.resizable(False, False)
            dialog.configure(bg="#F5F5F7")
            dialog.grab_set()
            tk.Label(
                dialog,
                text="API 重新连接失败",
                bg="#F5F5F7",
                fg="#1D1D1F",
                font=("Microsoft YaHei UI", 13, "bold"),
            ).pack(anchor="w", padx=24, pady=(20, 7))
            tk.Label(
                dialog,
                text=(
                    "是否仅本次加载本地 Qwen3-ASR 完成这段录音？\n"
                    "完成后会立即卸载，语音转写选择仍保持 API。\n\n"
                    "可以点击按钮，也可以直接说“是 / 可以”或“不用 / 取消”。"
                ),
                justify="left",
                wraplength=480,
                bg="#F5F5F7",
                fg="#3A3A3C",
                font=("Microsoft YaHei UI", 10),
            ).pack(anchor="w", padx=24)
            tk.Label(
                dialog,
                text=f"错误：{error[:240]}",
                justify="left",
                wraplength=480,
                bg="#FFFFFF",
                fg="#8E8E93",
                padx=12,
                pady=9,
                font=("Microsoft YaHei UI", 8),
            ).pack(fill="x", padx=24, pady=(12, 0))
            buttons = tk.Frame(dialog, bg="#F5F5F7")
            buttons.pack(fill="x", padx=24, pady=18)

            def finish(value: bool) -> None:
                if event.is_set():
                    return
                decision.append(value)
                try:
                    dialog.grab_release()
                    dialog.destroy()
                finally:
                    event.set()

            finish_callbacks.append(finish)
            ttk.Button(buttons, text="不用", command=lambda: finish(False)).pack(side="right")
            ttk.Button(buttons, text="是，仅本次使用", command=lambda: finish(True)).pack(
                side="right", padx=8
            )
            dialog.protocol("WM_DELETE_WINDOW", lambda: finish(False))
            dialog_ready.set()

        self.root.after(0, show_dialog)
        if not dialog_ready.wait(timeout=3.0):
            self.wake_listener.resume()
            return False
        if not event.is_set():
            spoken = self.wake_listener.listen_for_confirmation(
                timeout_seconds=8.0,
                cancel_event=event,
            )
            if spoken is not None and finish_callbacks:
                self.root.after(0, finish_callbacks[0], spoken)
        event.wait()
        self.wake_listener.resume()
        return bool(decision and decision[0])

    def _submit_transcription(
        self,
        text: str,
        task_started: float,
        asr_seconds: float,
    ) -> None:
        text = SpeechTranscriber.clean_transcript(text)
        if not text:
            self.busy = False
            self.record_button.configure(text="●  开始录音", state="normal")
            self.send_button.configure(state="normal")
            self.timing.set(self._timing_summary(asr_seconds=asr_seconds))
            self._continuous_session_active = False
            self._continuous_listening = False
            self.status.set(f"已忽略无效语音标记 · 再说“{self.wake_listener.keyword}”即可唤醒")
            return
        if self._is_filler_only(text):
            self.busy = False
            self.record_button.configure(text="●  开始录音", state="normal")
            self.send_button.configure(state="normal")
            self.timing.set(self._timing_summary(asr_seconds=asr_seconds))
            self._continuous_session_active = False
            self._continuous_listening = False
            self.status.set(f"已忽略环境短音 · 再说“{self.wake_listener.keyword}”即可唤醒")
            return
        # An explicit voice goodbye always wins, even if a UI/state transition
        # briefly cleared the continuous-session flag while ASR was running.
        if self._is_conversation_exit(text):
            self._append_chat("user", text, "语音转写")
            self._continuous_session_active = False
            self._continuous_listening = False
            self._skip_next_continuous = True
            # Keep the microphone listener out of the audio path until the
            # complete goodbye clip (including its trailing silence) has
            # drained.  Resuming it here used to make the final syllable sound
            # clipped on some Windows audio devices.
            self.wake_listener.pause(wait=True)
            self.record_button.configure(text="●  开始录音", state="normal")
            self.send_button.configure(state="normal")
            self.busy = False
            self.status.set(f"连续对话已结束 · 再说“{self.wake_listener.keyword}”即可唤醒")
            self.timing.set(self._timing_summary(asr_seconds=asr_seconds))
            threading.Thread(
                target=self._play_goodbye_and_resume,
                daemon=True,
            ).start()
            return
        self._submit(text, "voice", task_started, asr_seconds)

    def _play_goodbye_and_resume(self) -> None:
        try:
            try:
                goodbye_audio = self.tts.synthesize_cached_voice_clip(
                    self.goodbye_text,
                    self.goodbye_cache_dir,
                )
                play_wav_file(goodbye_audio)
            except Exception as exc:
                # Never fall back to another voice's cached clip. If the
                # selected engine cannot create its own clip, let its normal
                # playback path (including SAPI fallback) handle the phrase.
                self.tts.speak(self.goodbye_text)
                # The live fallback completed the user-visible action. Keep
                # the internal cache diagnostic out of the main status area.
                print(f"[TTS] 告别语音缓存重建失败，已实时生成：{exc}", file=sys.stderr)
            # Let the Windows output device flush its last hardware buffer.
            time.sleep(0.35)
        finally:
            if not self.closing:
                self.root.after(0, self.wake_listener.resume)

    def _prepare_voice_clips(self) -> None:
        """Prepare wake, interruption, thinking and goodbye clips for this voice."""
        self._prepare_api_notice_clips()
        self.tts.synthesize_cached_voice_clip(
            self.goodbye_text,
            self.goodbye_cache_dir,
        )
        for index, text in enumerate(self.wake_response_texts, start=1):
            self.tts.synthesize_cached_voice_clip(
                text,
                self.goodbye_cache_dir,
                f"wake-response-{index:02d}",
            )
        for index, text in enumerate(self.interrupt_response_texts, start=1):
            self.tts.synthesize_cached_voice_clip(
                text,
                self.goodbye_cache_dir,
                f"wake-interrupt-{index:02d}",
            )
        for index, text in enumerate(self.thinking_filler_texts, start=1):
            self.tts.synthesize_cached_voice_clip(
                text,
                self.goodbye_cache_dir,
                f"thinking-filler-{index:02d}",
                leading_silence_ms=320,
                trailing_silence_ms=420,
            )
        for context, text in self.thinking_long_wait_texts.items():
            self.tts.synthesize_cached_voice_clip(
                text,
                self.goodbye_cache_dir,
                f"thinking-long-{context}",
                leading_silence_ms=320,
                trailing_silence_ms=420,
            )
        for index, text in enumerate(self.thinking_completion_texts, start=1):
            self.tts.synthesize_cached_voice_clip(
                text,
                self.goodbye_cache_dir,
                f"thinking-complete-{index:02d}",
                leading_silence_ms=320,
                trailing_silence_ms=420,
            )

    def _prepare_api_notice_clips(self) -> None:
        """Prepare connection notices first so later API failures can speak."""
        for clip_name, text in (
            (
                "api-reconnecting",
                getattr(
                    self,
                    "api_reconnect_text",
                    "API 连接刚刚断开了，我重新连接一下，请稍等。",
                ),
            ),
            (
                "api-unavailable",
                getattr(
                    self,
                    "api_unavailable_text",
                    "API 还是没有连上，请查看屏幕上的提示。",
                ),
            ),
        ):
            self.tts.synthesize_cached_voice_clip(
                text,
                self.goodbye_cache_dir,
                clip_name,
                leading_silence_ms=280,
                trailing_silence_ms=420,
            )

    @staticmethod
    def _is_conversation_exit(text: str) -> bool:
        compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", text.lower())
        exit_markers = {
            "再见", "拜拜", "掰掰", "byebye", "bye", "没事",
            "没你什么事了", "没你事了", "没什么事了", "没别的事了",
            "不需要你了", "用不着你了",
            "不用了", "不聊了", "算了", "先这样", "就这样",
            "结束对话", "停止对话", "退出对话",
        }
        if any(marker in compact for marker in exit_markers):
            return True
        # Common ASR variants of “没什么事了”.  Keep this deliberately broad:
        # the voice UX treats any sentence containing an exit phrase as an
        # explicit request to leave continuous conversation.
        return bool(
            re.search(
                r"没(?:有)?(?:什么|啥|别的)?(?:事|事情|事儿)(?:了|啦)?",
                compact,
            )
        )

    @staticmethod
    def _is_filler_only(text: str) -> bool:
        compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", text.lower())
        if not compact or len(compact) > 6:
            return False
        fillers = {
            "嗯", "嗯嗯", "嗯哼", "啊", "啊啊", "呃", "额", "哦", "噢", "喔",
            "哎", "诶", "唉", "唔", "昂", "哼", "哈", "哈哈", "好", "好的",
            "um", "uh", "hmm", "hm", "mm", "em",
        }
        return compact in fillers

    def _change_voice(self, selection: tk.Event | str | None = None) -> None:
        try:
            if isinstance(selection, str):
                self.speaker.set(selection)
            self.tts.set_speaker(self.speaker.get())
            save_local_setting(
                "tts",
                "mimo_voice" if self.tts.backend == "mimo-api" else "speaker",
                self.tts.speaker,
            )
            self.status.set(f"音色已切换为 {self.tts.speaker}")
            if self.preload_enabled.get():
                self._set_preload_loading(True)
                self.root.update_idletasks()
                self.wake_listener.pause(wait=True)

                def prepare() -> None:
                    error = None
                    try:
                        self._prepare_voice_clips()
                        # The spoken announcement is deliberately last: it
                        # means the selected voice and all of its cached clips
                        # are actually ready, not merely that the worker has
                        # started loading.
                        self.tts.warmup()
                    except Exception as exc:
                        error = exc
                    if not self.closing:
                        self.root.after(0, finish, error)

                def finish(error: Exception | None) -> None:
                    self._set_preload_loading(False)
                    self.wake_listener.resume()
                    self.status.set(
                        f"音色 {self.tts.speaker} 已预加载"
                        if error is None
                        else f"音色预加载失败：{error}"
                    )

                threading.Thread(target=prepare, daemon=True).start()
        except ValueError as exc:
            messagebox.showerror("音色错误", str(exc), parent=self.root)

    def _start_tts_warmup(self) -> None:
        if self.closing:
            return
        self._preload_generation += 1
        generation = self._preload_generation
        self._set_preload_loading(True)
        self.status.set(f"正在预加载 {self.asr_mode.get()}…")
        self.root.update_idletasks()
        self.wake_listener.pause(wait=True)
        threading.Thread(target=self._warmup_worker, args=(generation,), daemon=True).start()

    def _toggle_tts_preload(self) -> None:
        if self.busy or self.recorder.is_recording:
            self.status.set("请等待当前语音任务结束后再切换预加载")
            return
        enabled = not self.preload_enabled.get()
        self.preload_enabled.set(enabled)
        self._preload_generation += 1
        self.config["tts"]["preload_on_startup"] = enabled
        save_local_setting("tts", "preload_on_startup", enabled)
        self._refresh_preload_button()
        if enabled:
            self._start_tts_warmup()
            return
        if self.wake_capture_enabled.get() and is_skill_enabled(self.config, "wake-word"):
            self.wake_listener.start()
            self.wake_listener.resume()
        self._set_preload_loading(False)
        self.status.set("正在关闭预加载模型…")

        def unload() -> None:
            try:
                self.tts.close()
                self.transcriber.close()
                if not self.closing:
                    self.root.after(
                        0,
                        self.status.set,
                        f"预加载已关闭 · {self.tts.engine_label} 已卸载",
                    )
            except Exception as exc:
                if not self.closing:
                    self.root.after(0, self.status.set, f"关闭预加载失败：{exc}")

        threading.Thread(target=unload, daemon=True).start()

    def _refresh_preload_button(self) -> None:
        button = getattr(self, "preload_button", None)
        if button is None:
            return
        if self._preload_loading:
            button.configure(
                text="加载中",
                fg_color="#FFF2D8",
                hover_color="#FFE6B5",
                text_color="#A35A00",
            )
            return
        enabled = self.preload_enabled.get()
        button.configure(
            text="开" if enabled else "关",
            fg_color="#E8F2FF" if enabled else "#F0F0F3",
            hover_color="#D8E9FF" if enabled else "#E2E2E7",
            text_color="#007AFF" if enabled else "#6E6E73",
        )

    def _set_preload_loading(self, loading: bool) -> None:
        self._preload_loading = loading
        self._refresh_preload_button()
        state = "disabled" if loading else "normal"
        if not self.busy and not self.recorder.is_recording:
            self.record_button.configure(state=state)
            self.send_button.configure(state=state)
        if hasattr(self, "enroll_button"):
            self.enroll_button.configure(state=state)

    def _warmup_worker(self, generation: int) -> None:
        try:
            self.transcriber.warmup()
            if generation != self._preload_generation or not self.preload_enabled.get():
                self.transcriber.close()
                return
            if not self.closing:
                self.root.after(
                    0,
                    self.status.set,
                    f"{self.asr_mode.get()} 已加载 · 正在预加载 {self.tts.engine_label}…",
                )
            self._prepare_voice_clips()
            if generation != self._preload_generation or not self.preload_enabled.get():
                self.tts.close()
                return
            # Keep this as the final blocking preload operation. The worker
            # only returns after the announcement has finished playing.
            self.tts.warmup()
            if not self.closing and generation == self._preload_generation and self.preload_enabled.get():
                self.root.after(0, self._finish_tts_warmup_status)
        except Exception as exc:
            if not self.closing:
                self.root.after(0, self._refresh_preload_button)
                self.root.after(0, self._set_preload_loading, False)
                if self.wake_capture_enabled.get() and is_skill_enabled(self.config, "wake-word"):
                    self.root.after(0, self.wake_listener.start)
                    self.root.after(0, self.wake_listener.resume)
                self.root.after(0, self.status.set, f"语音预热失败：{exc}")

    def _finish_tts_warmup_status(self) -> None:
        self._set_preload_loading(False)
        if self.wake_capture_enabled.get() and is_skill_enabled(self.config, "wake-word"):
            self.wake_listener.start()
            self.wake_listener.resume()
        wake_status = (
            self._wake_status_value
            if self.wake_capture_enabled.get()
            else "唤醒监听已关闭"
        )
        self.status.set(f"ASR 与语音播报均已预加载 · {wake_status}")

    def _change_tts_mode(self, selection: tk.Event | str | None = None) -> None:
        if self.busy or self.recorder.is_recording:
            self.tts_mode.set(self.tts.engine_label)
            self.status.set("请等待当前语音任务结束后再切换引擎")
            return
        label = selection if isinstance(selection, str) else self.tts_mode.get()
        backend = ENGINE_BACKENDS.get(label)
        if backend is None:
            self.tts_mode.set(self.tts.engine_label)
            return
        if backend == self.tts.backend:
            self.status.set(f"当前使用 {label}")
            return
        previous_label = self.tts.engine_label
        previous_backend = self.tts.backend
        preloading = self.preload_enabled.get()
        if preloading:
            self._set_preload_loading(True)
            self.root.update_idletasks()
            self.wake_listener.pause(wait=True)
        self.status.set(f"正在切换到 {label}…")
        self.mode_combo.configure(state="disabled")

        def switch() -> None:
            try:
                self.tts.set_engine(backend)
                self.config["tts"]["backend"] = backend
                save_local_setting("tts", "backend", backend)
                if self.preload_enabled.get():
                    self._prepare_voice_clips()
                    self.tts.warmup()
                if not self.closing:
                    self.root.after(0, finish, None)
            except Exception as exc:
                try:
                    self.tts.set_engine(previous_backend)
                except Exception:
                    pass
                if not self.closing:
                    self.root.after(0, finish, exc)

        def finish(error: Exception | None) -> None:
            if preloading:
                self._set_preload_loading(False)
                self.wake_listener.resume()
            self.mode_combo.configure(
                state="readonly" if isinstance(self.mode_combo, ttk.Combobox) else "normal"
            )
            if error is None:
                self.tts_mode.set(self.tts.engine_label)
                self.speaker.set(self.tts.voice_label)
                self.voice_combo.configure(values=self.tts.available_voices())
                self.status.set(f"已切换到 {self.tts.engine_label}")
            else:
                self.tts_mode.set(previous_label)
                self.status.set(f"切换语音引擎失败：{error}")

        threading.Thread(target=switch, daemon=True).start()

    def _tts_control(self, action: str) -> None:
        labels = {"pause": "已暂停播报", "resume": "已继续播报", "stop": "已停止播报"}
        if action == "stop":
            self._skip_next_continuous = True
            self._continuous_session_active = False

        def worker() -> None:
            try:
                getattr(self.tts, action)()
                if not self.closing:
                    self.root.after(0, self.status.set, labels[action])
            except Exception as exc:
                if not self.closing:
                    self.root.after(0, self.status.set, f"播报控制失败：{exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_usage(self) -> None:
        values = self._ui_database.usage_summary(self.config["api"]["currency"])
        symbol = self.config["api"]["currency_symbol"]
        self.usage.set(f"今日 {symbol}{values['today']:.2f} · 本月 {symbol}{values['month']:.2f}")

    @staticmethod
    def _foreground_app_name() -> str:
        if sys.platform != "win32":
            return "unknown"
        try:
            import win32api
            import win32con
            import win32process

            hwnd = ctypes.windll.user32.GetForegroundWindow()
            _thread, process_id = win32process.GetWindowThreadProcessId(hwnd)
            process = win32api.OpenProcess(
                win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
                False,
                process_id,
            )
            try:
                return Path(win32process.GetModuleFileNameEx(process, 0)).name.lower()
            finally:
                process.Close()
        except Exception:
            return "unknown"

    def _permission_key(self, name: str, arguments: dict) -> str:
        if name == "open_url":
            domain = (urlparse(str(arguments.get("url") or "")).hostname or "").lower()
            return f"open_url:{domain}" if domain else ""
        if name in {"click_screen", "type_text"}:
            app_name = str(
                arguments.get("_target_app") or self._foreground_app_name()
            ).strip().lower()
            description = re.sub(
                r"[^a-z0-9\u4e00-\u9fff]",
                "",
                str(arguments.get("description") or "").lower(),
            )[:80]
            return f"{name}:{app_name}:{description}" if description else f"{name}:{app_name}"
        if name == "create_scheduled_task":
            command = re.sub(
                r"\s+", "", str(arguments.get("任务") or arguments.get("command") or "")
            )[:100]
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

    @staticmethod
    def _permission_label(name: str, arguments: dict, permission_key: str) -> str:
        if name == "open_url":
            return (urlparse(str(arguments.get("url") or "")).hostname or permission_key)
        if name in {"click_screen", "type_text"}:
            action = str(arguments.get("description") or "屏幕操作").strip()
            app = str(arguments.get("_target_app") or "当前应用").strip()
            return f"{app} · {action}"
        if name == "create_scheduled_task":
            return str(arguments.get("任务") or arguments.get("command") or "定时任务")
        if name == "add_app_to_allowlist":
            return str(arguments.get("name") or arguments.get("应用") or "添加应用")
        return permission_key

    def _permission_records(self) -> list[dict]:
        permissions = self.config.setdefault("permissions", {})
        allowlist = [str(value) for value in permissions.get("allowlist", [])]
        configured = permissions.get("records", [])
        records = [dict(value) for value in configured if isinstance(value, dict)]
        known = {str(value.get("key") or "") for value in records}
        for key in allowlist:
            if key in known:
                continue
            tool = key.split(":", 1)[0]
            records.append(
                {
                    "key": key,
                    "tool": tool,
                    "category": self._permission_category(tool),
                    "label": key,
                    "created_at": "旧版授权",
                }
            )
        return sorted(records, key=lambda item: (str(item.get("category")), str(item.get("label"))))

    def _grant_permission(
        self,
        name: str,
        arguments: dict,
        permission_key: str,
    ) -> None:
        if not permission_key:
            return
        permissions = self.config.setdefault("permissions", {})
        allowlist = {str(value) for value in permissions.get("allowlist", [])}
        allowlist.add(permission_key)
        records = [
            value for value in self._permission_records()
            if str(value.get("key") or "") != permission_key
        ]
        records.append(
            {
                "key": permission_key,
                "tool": name,
                "category": self._permission_category(name),
                "label": self._permission_label(name, arguments, permission_key),
                "created_at": time.strftime("%Y-%m-%d %H:%M"),
            }
        )
        permissions["allowlist"] = sorted(allowlist)
        permissions["records"] = records
        save_local_setting("permissions", "allowlist", permissions["allowlist"])
        save_local_setting("permissions", "records", records)

    def _remove_permission(self, permission_key: str) -> None:
        permissions = self.config.setdefault("permissions", {})
        permissions["allowlist"] = [
            str(value) for value in permissions.get("allowlist", [])
            if str(value) != permission_key
        ]
        permissions["records"] = [
            value for value in self._permission_records()
            if str(value.get("key") or "") != permission_key
        ]
        save_local_setting("permissions", "allowlist", permissions["allowlist"])
        save_local_setting("permissions", "records", permissions["records"])

    def _confirm_tool(self, name: str, arguments: dict) -> bool:
        if self.closing:
            return False
        permission_key = self._permission_key(name, arguments)
        allowlist = set(
            str(value)
            for value in self.config.get("permissions", {}).get("allowlist", [])
        )
        if permission_key and permission_key in allowlist:
            return True
        description = str(arguments.get("description") or "")
        high_risk = bool(
            re.search(
                r"删除|付款|支付|购买|下单|发送消息|提交表单|验证码|密码|管理员|卸载",
                description + json.dumps(arguments, ensure_ascii=False),
            )
        )
        event = threading.Event()
        dialog_ready = threading.Event()
        decision: list[bool] = []
        finish_callbacks: list = []

        self.wake_listener.pause()

        def ask() -> None:
            display_arguments = {
                key: value for key, value in arguments.items()
                if not str(key).startswith("_")
            }
            preview = json.dumps(display_arguments, ensure_ascii=False, indent=2)
            if len(preview) > 1200:
                preview = preview[:1200] + "\n…"
            dialog = tk.Toplevel(self.root)
            dialog.title("确认电脑操作")
            dialog.transient(self.root)
            dialog.resizable(False, False)
            dialog.configure(bg="#F5F5F7")
            dialog.grab_set()
            tk.Label(
                dialog,
                text=f"猫猫想执行：{self._permission_label(name, arguments, permission_key)}",
                bg="#F5F5F7",
                fg="#1D1D1F",
                font=("Microsoft YaHei UI", 11, "bold"),
            ).pack(anchor="w", padx=22, pady=(18, 8))
            tk.Label(
                dialog,
                text=preview,
                justify="left",
                wraplength=520,
                bg="#FFFFFF",
                fg="#3A3A3C",
                padx=14,
                pady=12,
                font=("Microsoft YaHei UI", 9),
            ).pack(fill="x", padx=22)
            if permission_key:
                tk.Label(
                    dialog,
                    text=(
                        "这类操作需要每次确认，可以点击或直接说“是 / 不用”。"
                        if high_risk
                        else "允许后会记住这个具体动作；也可以直接说“是 / 不用”。"
                    ),
                    bg="#F5F5F7",
                    fg="#8E8E93",
                    font=("Microsoft YaHei UI", 8),
                ).pack(anchor="w", padx=22, pady=(7, 0))
            buttons = tk.Frame(dialog, bg="#F5F5F7")
            buttons.pack(fill="x", padx=22, pady=18)

            def finish(allowed: bool) -> None:
                if event.is_set():
                    return
                if allowed and permission_key and not high_risk:
                    self._grant_permission(name, arguments, permission_key)
                decision.append(allowed)
                try:
                    dialog.grab_release()
                    dialog.destroy()
                finally:
                    event.set()

            finish_callbacks.append(finish)

            def pause_and_finish() -> None:
                self._pause_all_operations()
                finish(False)

            ttk.Button(
                buttons,
                text="暂停全部",
                command=pause_and_finish,
            ).pack(side="left")
            ttk.Button(buttons, text="取消", command=lambda: finish(False)).pack(side="right")
            allow_label = "确认本次" if high_risk else "允许并记住"
            ttk.Button(buttons, text=allow_label, command=lambda: finish(True)).pack(
                side="right", padx=8
            )
            dialog.protocol("WM_DELETE_WINDOW", lambda: finish(False))
            dialog.update_idletasks()
            x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - dialog.winfo_width()) // 2)
            y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - dialog.winfo_height()) // 2)
            dialog.geometry(f"+{x}+{y}")
            dialog_ready.set()

        self.root.after(0, ask)
        if not dialog_ready.wait(timeout=3.0):
            self.wake_listener.resume()
            return False
        if not event.is_set():
            spoken = self.wake_listener.listen_for_confirmation(
                timeout_seconds=10.0,
                cancel_event=event,
            )
            if spoken is not None and finish_callbacks:
                self.root.after(0, finish_callbacks[0], spoken)
        event.wait()
        self.wake_listener.resume()
        return bool(decision and decision[0])

    def _on_close(self) -> None:
        if self.closing:
            return
        if self.busy and not messagebox.askyesno(
            "退出助手", "当前仍有任务执行中，确定退出吗？", parent=self.root
        ):
            return
        self._begin_shutdown("正在关闭本地模型…")

    def _begin_shutdown(self, status_text: str) -> None:
        if self.closing:
            return
        self.closing = True
        self._task_cancel_event.set()
        self._scheduled_task_cancel_event.set()
        self._agent_task_queue.put(None)
        self._stop_tray_icon()
        ui_database = getattr(self, "_ui_database", None)
        if ui_database is not None:
            ui_database.close()
            self._ui_database = None
        self.status.set(status_text)
        self.send_button.configure(state="disabled")
        self.record_button.configure(state="disabled")

        def cleanup() -> None:
            try:
                self.wake_listener.close()
                self.recorder.cancel()
                self.transcriber.close()
                self.tts.close()
            finally:
                self.root.after(0, self.root.destroy)

        threading.Thread(target=cleanup, daemon=True).start()


def main() -> int:
    enable_dpi_awareness()
    configure_windows_app_identity()
    root = tk.Tk()
    AssistantWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
