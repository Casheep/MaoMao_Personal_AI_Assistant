from __future__ import annotations

import tkinter as tk
import ctypes
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .gui import (
    ASR_MODE_LABELS,
    MODEL_MODE_LABELS,
    AssistantWindow,
    configure_windows_app_identity,
    apply_windows_round_corners,
    app_paths,
)
from .database import Database
from .config import save_local_setting
from .components import (
    install_component,
    missing_startup_components,
)
from .skills import SKILL_CATALOG, SKILL_CATEGORY_ORDER, SKILLS_BY_ID, is_skill_enabled
from .secrets import key_configuration_status, save_api_keys
from .tts import ENGINE_BACKENDS


_SINGLE_INSTANCE_HANDLE: int | None = None
_SKILL_POSITION = {
    definition.id: index for index, definition in enumerate(SKILL_CATALOG)
}


class StartupComponentView:
    """Download missing Lite edition components before the main UI starts."""

    def __init__(self, root: ctk.CTk, component_ids: list[str], on_ready) -> None:
        self.root = root
        self.component_ids = component_ids
        self.on_ready = on_ready
        self.frame: ctk.CTkFrame | None = None
        self.status = None
        self.progress = None
        self.retry_button = None
        self._build()
        self._start()

    def _build(self) -> None:
        self.root.title("猫猫")
        self.root.geometry("560x320+250+160")
        self.root.resizable(False, False)
        self.root.configure(fg_color="#F5F5F7")
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        frame = ctk.CTkFrame(
            self.root,
            fg_color="#FFFFFF",
            border_color="#E3E3E8",
            border_width=1,
            corner_radius=34,
        )
        frame.pack(fill="both", expand=True, padx=28, pady=28)
        self.frame = frame
        ctk.CTkLabel(
            frame,
            text="正在准备猫猫",
            text_color="#1D1D1F",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=24, weight="bold"),
        ).pack(pady=(42, 10))
        self.status = ctk.CTkLabel(
            frame,
            text="正在检查运行组件…",
            text_color="#6E6E73",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=13),
        )
        self.status.pack(pady=(0, 18))
        self.progress = ctk.CTkProgressBar(
            frame,
            width=390,
            height=12,
            corner_radius=6,
            progress_color="#007AFF",
        )
        self.progress.set(0)
        self.progress.pack()
        self.retry_button = ctk.CTkButton(
            frame,
            text="重新下载",
            command=self._start,
            width=130,
            height=38,
            corner_radius=19,
            fg_color="#007AFF",
            hover_color="#1687FF",
        )
        self.root.after(50, apply_windows_round_corners, self.root)

    def _start(self) -> None:
        if self.retry_button is not None:
            self.retry_button.pack_forget()
        self.status.configure(text="正在下载首次运行所需组件…", text_color="#6E6E73")
        self.progress.set(0)
        threading.Thread(target=self._worker, name="startup-components", daemon=True).start()

    def _worker(self) -> None:
        count = len(self.component_ids)
        try:
            for index, component_id in enumerate(self.component_ids):
                install_component(
                    component_id,
                    progress=lambda value, current=index: self.root.after(
                        0,
                        self._set_progress,
                        (current + value / 100) / count,
                    ),
                )
        except Exception as exc:
            self.root.after(0, self._show_error, str(exc))
            return
        self.root.after(0, self._finish)

    def _set_progress(self, value: float) -> None:
        self.progress.set(max(0.0, min(1.0, value)))
        self.status.configure(text=f"正在下载首次运行所需组件… {round(value * 100)}%")

    def _show_error(self, detail: str) -> None:
        self.status.configure(text="下载失败，请检查网络后重试。", text_color="#D70015")
        self.retry_button.pack(pady=(20, 0))
        messagebox.showerror("无法完成首次准备", detail, parent=self.root)

    def _finish(self) -> None:
        self.progress.set(1)
        self.status.configure(text="准备完成，正在打开猫猫…", text_color="#34C759")
        self.root.after(250, self._open_main_window)

    def _open_main_window(self) -> None:
        if self.frame is not None:
            self.frame.destroy()
        self.root.resizable(True, True)
        self.on_ready()


def _activate_existing_window() -> None:
    """Restore the existing Cat window when the launcher is opened twice."""
    if not hasattr(ctypes, "windll"):
        return
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]

    def visit_window(window: int, _parameter: int) -> bool:
        length = user32.GetWindowTextLengthW(window)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(window, title, length + 1)
        if title.value == "猫猫":
            user32.ShowWindow(window, 9)  # SW_RESTORE
            user32.SetForegroundWindow(window)
            return False
        return True

    user32.EnumWindows(callback_type(visit_window), 0)


def _acquire_single_instance() -> bool:
    global _SINGLE_INSTANCE_HANDLE
    if not hasattr(ctypes, "windll"):
        return True
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.GetLastError.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.SetLastError(0)
    handle = kernel32.CreateMutexW(None, False, "Local\\MaoMaoPersonalAssistant")
    if not handle:
        return True
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        _activate_existing_window()
        return False
    _SINGLE_INSTANCE_HANDLE = handle
    return True


class RoundedAssistantWindow(AssistantWindow):
    """Apple-inspired shell using real rounded CustomTkinter components."""

    def _build_window(self) -> None:
        self.root.title("猫猫")
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        window_width = min(1540, max(1180, screen_width - 80))
        window_height = min(800, max(680, screen_height - 80))
        window_x = max(20, (screen_width - window_width) // 2)
        window_y = max(18, (screen_height - window_height) // 2)
        self.root.geometry(
            f"{window_width}x{window_height}+{window_x}+{window_y}"
        )
        self.root.minsize(1180, 640)
        self.root.configure(fg_color="#F5F5F7")

        bg = "#F5F5F7"
        card = "#FFFFFF"
        text = "#1D1D1F"
        secondary = "#6E6E73"
        border = "#E3E3E8"
        blue = "#007AFF"
        font = "Microsoft YaHei UI"
        self._sidebar_animation_ids: dict[str, str] = {}
        self._sidebar_label_font = ctk.CTkFont(
            family=font, size=12, weight="bold"
        )
        self._sidebar_arrow_font = ctk.CTkFont(
            family=font, size=24, weight="bold"
        )

        shell = ctk.CTkFrame(self.root, fg_color=bg, corner_radius=0)
        shell.pack(fill="both", expand=True, padx=22, pady=20)
        shell.grid_columnconfigure(1, weight=1)
        shell.grid_rowconfigure(0, weight=1)

        left_slot = ctk.CTkFrame(shell, fg_color="transparent", corner_radius=0)
        left_slot.grid(row=0, column=0, sticky="nsw")
        left_slot.grid_rowconfigure(0, weight=1)
        self._skill_sidebar = ctk.CTkFrame(
            left_slot,
            width=218,
            fg_color="#FFFFFF",
            border_color=border,
            border_width=1,
            corner_radius=34,
        )
        self._skill_sidebar.grid(row=0, column=0, sticky="ns")
        self._skill_sidebar.grid_propagate(False)
        self._skill_sidebar_tab = ctk.CTkButton(
            left_slot,
            text="‹",
            command=self._toggle_skill_sidebar_visibility,
            width=24,
            height=118,
            corner_radius=12,
            fg_color="#E8F2FF",
            hover_color="#D8E9FF",
            text_color=blue,
            font=self._sidebar_arrow_font,
        )
        self._skill_sidebar_tab.grid(row=0, column=1, sticky="w", padx=(4, 0))
        self._build_skill_sidebar(self._skill_sidebar, font, text, secondary, border, blue)

        outer = ctk.CTkFrame(shell, fg_color=bg, corner_radius=0)
        outer.grid(row=0, column=1, sticky="nsew", padx=14)
        # Keep the card grid constrained to the visible window instead of
        # letting the large chat widget request space below the screen.
        outer.grid_propagate(False)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(2, weight=1)

        right_slot = ctk.CTkFrame(shell, fg_color="transparent", corner_radius=0)
        right_slot.grid(row=0, column=2, sticky="nse")
        right_slot.grid_rowconfigure(0, weight=1)
        self._favorite_sidebar_tab = ctk.CTkButton(
            right_slot,
            text="收\n藏\n夹",
            command=self._toggle_favorite_sidebar_visibility,
            width=28,
            height=118,
            corner_radius=14,
            fg_color="#FFF3D9",
            hover_color="#FFE9B5",
            text_color="#A66300",
            font=self._sidebar_label_font,
        )
        self._favorite_sidebar_tab.grid(row=0, column=0, sticky="e", padx=(0, 4))
        self._favorite_sidebar = ctk.CTkFrame(
            right_slot,
            width=188,
            fg_color="#FFFFFF",
            border_color=border,
            border_width=1,
            corner_radius=34,
        )
        self._favorite_sidebar.grid(row=0, column=1, sticky="ns")
        self._favorite_sidebar.grid_propagate(False)
        self._build_favorite_sidebar(
            self._favorite_sidebar, font, text, secondary, border, blue
        )
        self._restore_sidebar_visibility()

        header = ctk.CTkFrame(outer, fg_color="transparent", corner_radius=0)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text="喵~",
            text_color=text,
            font=ctk.CTkFont(family=font, size=27, weight="bold"),
        ).grid(row=0, column=0, sticky="w")
        self.about_button = ctk.CTkButton(
            header,
            text="关于",
            command=self._open_about_page,
            width=62,
            height=28,
            corner_radius=14,
            fg_color="#F0F0F3",
            hover_color="#E2E2E7",
            text_color=text,
            font=ctk.CTkFont(family=font, size=11),
        )
        self.about_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.api_keys_button = ctk.CTkButton(
            header,
            text="API 密钥",
            command=self._open_api_key_page,
            width=78,
            height=28,
            corner_radius=14,
            fg_color="#E8F2FF",
            hover_color="#D8E9FF",
            text_color=blue,
            font=ctk.CTkFont(family=font, size=11),
        )
        self.api_keys_button.grid(row=0, column=2, sticky="e", padx=(8, 0))
        ctk.CTkLabel(
            header,
            textvariable=self.usage,
            text_color=secondary,
            fg_color="#E9E9ED",
            corner_radius=13,
            height=28,
            padx=13,
            font=ctk.CTkFont(family=font, size=12),
        ).grid(row=0, column=3, sticky="e")

        subheader = ctk.CTkFrame(outer, fg_color="transparent", corner_radius=0)
        subheader.grid(row=1, column=0, sticky="ew", pady=(2, 14))
        subheader.grid_columnconfigure(4, weight=1)
        ctk.CTkLabel(
            subheader,
            text="文本模型",
            text_color=secondary,
            font=ctk.CTkFont(family=font, size=12),
        ).grid(row=0, column=0, sticky="w")
        self.model_selector = ctk.CTkOptionMenu(
            subheader,
            values=list(MODEL_MODE_LABELS),
            variable=self.model_mode,
            command=self._change_model_mode,
            width=174,
            height=28,
            corner_radius=14,
            fg_color="#E9E9ED",
            button_color="#DEDEE3",
            button_hover_color="#D3D3D9",
            text_color=text,
            font=ctk.CTkFont(family=font, size=11),
        )
        self.model_selector.grid(row=0, column=1, sticky="w", padx=(6, 18))
        ctk.CTkLabel(
            subheader,
            text="语音转写",
            text_color=secondary,
            font=ctk.CTkFont(family=font, size=12),
        ).grid(row=0, column=2, sticky="w")
        self.asr_selector = ctk.CTkOptionMenu(
            subheader,
            values=self.asr_mode_choices,
            variable=self.asr_mode,
            command=self._change_asr_mode,
            width=178,
            height=28,
            corner_radius=14,
            fg_color="#E9E9ED",
            button_color="#DEDEE3",
            button_hover_color="#D3D3D9",
            text_color=text,
            font=ctk.CTkFont(family=font, size=11),
        )
        self.asr_selector.grid(row=0, column=3, sticky="w", padx=(6, 18))

        chat_card = ctk.CTkFrame(
            outer,
            fg_color=card,
            border_color=border,
            border_width=1,
            corner_radius=42,
        )
        chat_card.grid(row=2, column=0, sticky="nsew")
        chat_card.grid_columnconfigure(0, weight=1)
        chat_card.grid_rowconfigure(1, weight=1)
        chat_toolbar = ctk.CTkFrame(chat_card, fg_color="transparent", corner_radius=0)
        chat_toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=(24, 19), pady=(12, 0))
        chat_toolbar.grid_columnconfigure(0, weight=1)
        self.clear_chat_button = ctk.CTkButton(
            chat_toolbar,
            text="清除记录",
            command=self._clear_conversation,
            width=76,
            height=28,
            corner_radius=14,
            fg_color="#F0F0F3",
            hover_color="#E2E2E7",
            text_color=secondary,
            font=ctk.CTkFont(family=font, size=11),
        )
        self.clear_chat_button.grid(row=0, column=1, sticky="e")
        self.chat = tk.Text(
            chat_card,
            height=10,
            wrap="word",
            state="disabled",
            font=(font, 13),
            bg=card,
            fg=text,
            insertbackground=text,
            selectbackground="#D6E9FF",
            relief="flat",
            borderwidth=0,
            padx=20,
            pady=18,
        )
        # Tk Text is rectangular. Keep it inside the 30px arc so it cannot
        # cover the CustomTkinter card's rounded border on Windows.
        self.chat.grid(row=1, column=0, sticky="nsew", padx=(24, 4), pady=(5, 20))
        scrollbar = ctk.CTkScrollbar(chat_card, command=self.chat.yview, width=12)
        scrollbar.grid(row=1, column=1, sticky="ns", padx=(2, 19), pady=(10, 27))
        self.chat.configure(yscrollcommand=scrollbar.set)
        self.chat.tag_configure("user", foreground=blue, spacing1=10, spacing3=10, font=(font, 13, "bold"))
        self.chat.tag_configure("assistant", foreground=text, spacing1=10, spacing3=10, font=(font, 13))
        self.chat.tag_configure("system", foreground=secondary, spacing1=8, spacing3=8, font=(font, 13))
        self.chat.tag_configure("meta", foreground="#8E8E93", font=(font, 11))

        controls = ctk.CTkFrame(
            outer,
            fg_color=card,
            border_color=border,
            border_width=1,
            corner_radius=42,
        )
        controls.grid(row=3, column=0, sticky="ew", pady=(14, 10))
        controls.grid_columnconfigure(3, weight=1)

        def control_group(column: int, label: str, left_pad: int = 0, right_pad: int = 12):
            group = ctk.CTkFrame(controls, fg_color="transparent", corner_radius=0)
            group.grid(
                row=0,
                column=column,
                sticky="sw",
                padx=(left_pad, right_pad),
                pady=(11, 8),
            )
            ctk.CTkLabel(
                group,
                text=label,
                height=18,
                text_color=secondary,
                font=ctk.CTkFont(family=font, size=12),
            ).pack(anchor="w")
            return group

        record_group = control_group(0, "输入", 16, 10)
        self.record_button = ctk.CTkButton(
            record_group,
            text="●  开始录音",
            command=self._toggle_recording,
            fg_color=blue,
            hover_color="#1687FF",
            corner_radius=21,
            height=42,
            width=120,
            font=ctk.CTkFont(family=font, size=13, weight="bold"),
        )
        self.record_button.pack(anchor="w", pady=(4, 0))

        self.speaker.set(self.tts.voice_label)
        voice_group = control_group(1, "音色")
        self.voice_combo = ctk.CTkOptionMenu(
            voice_group,
            variable=self.speaker,
            values=self.tts.available_voices(),
            command=self._change_voice,
            width=104,
            height=42,
            corner_radius=21,
            fg_color="#F0F0F3",
            button_color="#E4E4E9",
            button_hover_color="#DADAE0",
            text_color=text,
            dropdown_fg_color="#FFFFFF",
            dropdown_hover_color="#E8F2FF",
            dropdown_text_color=text,
            font=ctk.CTkFont(family=font, size=13),
        )
        self.voice_combo.pack(anchor="w", pady=(4, 0))
        engine_group = control_group(2, "语音生成")
        self.mode_combo = ctk.CTkOptionMenu(
            engine_group,
            variable=self.tts_mode,
            values=self.tts.available_engines(),
            command=self._change_tts_mode,
            width=258,
            height=42,
            corner_radius=21,
            fg_color="#F0F0F3",
            button_color="#E4E4E9",
            button_hover_color="#DADAE0",
            text_color=text,
            dropdown_fg_color="#FFFFFF",
            dropdown_hover_color="#E8F2FF",
            dropdown_text_color=text,
            font=ctk.CTkFont(family=font, size=13),
        )
        self.mode_combo.pack(anchor="w", pady=(4, 0))

        preload_group = control_group(3, "预加载")
        self.preload_button = ctk.CTkButton(
            preload_group,
            text="",
            command=self._toggle_tts_preload,
            width=72,
            height=42,
            corner_radius=21,
            border_width=0,
            font=ctk.CTkFont(family=font, size=12),
        )
        self.preload_button.pack(anchor="w", pady=(4, 0))
        self._refresh_preload_button()

        playback_bar = ctk.CTkFrame(controls, fg_color="transparent", corner_radius=0)
        playback_bar.grid(
            row=1,
            column=0,
            columnspan=4,
            sticky="ew",
            padx=17,
            pady=(2, 7),
        )
        playback_bar.grid_columnconfigure(5, weight=1)
        ctk.CTkLabel(
            playback_bar,
            text="播放",
            height=30,
            text_color=secondary,
            font=ctk.CTkFont(family=font, size=12),
        ).grid(row=0, column=0, sticky="w", padx=(0, 9))
        for index, (label, action) in enumerate((("暂停", "pause"), ("继续", "resume"), ("停止", "stop"))):
            ctk.CTkButton(
                playback_bar,
                text=label,
                command=lambda name=action: self._tts_control(name),
                width=62,
                height=34,
                corner_radius=17,
                fg_color="#F0F0F3",
                hover_color="#E2E2E7",
                text_color=text,
                font=ctk.CTkFont(family=font, size=12),
            ).grid(row=0, column=index + 1, padx=(0, 4))

        self.interrupt_button = ctk.CTkButton(
            playback_bar,
            text="暂停全部",
            command=self._pause_all_operations,
            fg_color="#FFE8E8",
            hover_color="#FFD7D7",
            text_color="#D70015",
            corner_radius=17,
            width=82,
            height=34,
            font=ctk.CTkFont(family=font, size=11, weight="bold"),
        )
        self.interrupt_button.grid(row=0, column=6, sticky="e")

        status_bar = ctk.CTkFrame(controls, fg_color="transparent", corner_radius=0)
        status_bar.grid(row=2, column=0, columnspan=4, sticky="ew", padx=17, pady=(0, 12))
        status_bar.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(status_bar, text="●", text_color="#34C759", font=("Segoe UI", 10)).grid(row=0, column=0)
        ctk.CTkLabel(status_bar, textvariable=self.status, text_color=secondary, font=(font, 12)).grid(row=0, column=1, sticky="w", padx=(5, 0))
        ctk.CTkLabel(status_bar, textvariable=self.timing, text_color="#8E8E93", font=(font, 12)).grid(row=0, column=3, sticky="e")

        compose = ctk.CTkFrame(
            outer,
            fg_color=card,
            border_color=border,
            border_width=1,
            corner_radius=42,
        )
        compose.grid(row=4, column=0, sticky="ew")
        compose.grid_columnconfigure(0, weight=1)
        self.input_box = tk.Text(
            compose,
            height=4,
            wrap="word",
            font=(font, 13),
            bg=card,
            fg=text,
            insertbackground=text,
            selectbackground="#D6E9FF",
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=9,
        )
        self.input_box.grid(row=0, column=0, sticky="nsew", padx=(27, 8), pady=19)
        self.input_box.bind("<Control-Return>", self._send_shortcut)

        actions = ctk.CTkFrame(compose, fg_color="transparent", corner_radius=0)
        actions.grid(row=0, column=1, sticky="ns", padx=(6, 22), pady=16)
        self.send_button = ctk.CTkButton(
            actions,
            text="发送",
            command=self._send_text,
            fg_color=blue,
            hover_color="#1687FF",
            corner_radius=22,
            width=112,
            height=44,
            font=ctk.CTkFont(family=font, size=13, weight="bold"),
        )
        self.send_button.pack(fill="x", expand=True, pady=(10, 10))

    def _build_skill_sidebar(
        self,
        parent,
        font: str,
        text: str,
        secondary: str,
        border: str,
        blue: str,
    ) -> None:
        self._skill_sidebar_mode = "learned"
        self._skill_sidebar_category = "全部"
        stored_favorites = self.config.get("ui", {}).get("favorite_skills", [])
        self._favorite_skill_ids = [
            skill_id
            for skill_id in stored_favorites
            if skill_id in SKILLS_BY_ID
        ] if isinstance(stored_favorites, list) else []
        self._skill_switch_vars = {
            definition.id: tk.BooleanVar(
                value=is_skill_enabled(self.config, definition.id)
            )
            for definition in SKILL_CATALOG
        }

        title_bar = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        title_bar.pack(fill="x", padx=18, pady=(18, 2))
        ctk.CTkLabel(
            title_bar,
            text="技能",
            text_color=text,
            font=ctk.CTkFont(family=font, size=20, weight="bold"),
        ).pack(side="left")
        self._skill_count_label = ctk.CTkLabel(
            title_bar,
            text="",
            text_color=secondary,
            font=ctk.CTkFont(family=font, size=10),
        )
        self._skill_count_label.pack(side="right", padx=(6, 0))

        tabs = ctk.CTkFrame(parent, fg_color="#F0F0F3", corner_radius=18)
        tabs.pack(fill="x", padx=12, pady=(8, 10))
        tabs.grid_columnconfigure(0, weight=1)
        tabs.grid_columnconfigure(1, weight=1)
        self._learned_skills_button = ctk.CTkButton(
            tabs,
            text="",
            command=lambda: self._select_skill_sidebar("learned"),
            height=34,
            corner_radius=17,
            border_width=0,
            font=ctk.CTkFont(family=font, size=11, weight="bold"),
        )
        self._learned_skills_button.grid(row=0, column=0, sticky="ew", padx=2, pady=2)
        self._skill_library_button = ctk.CTkButton(
            tabs,
            text="",
            command=lambda: self._select_skill_sidebar("library"),
            height=34,
            corner_radius=17,
            border_width=0,
            font=ctk.CTkFont(family=font, size=11, weight="bold"),
        )
        self._skill_library_button.grid(row=0, column=1, sticky="ew", padx=2, pady=2)

        category_tabs = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        category_tabs.pack(fill="x", padx=12, pady=(0, 8))
        category_tabs.grid_columnconfigure(0, weight=1)
        category_tabs.grid_columnconfigure(1, weight=1)
        self._skill_category_buttons = {}
        for index, category in enumerate(("全部", *SKILL_CATEGORY_ORDER)):
            button = ctk.CTkButton(
                category_tabs,
                text=category,
                command=lambda value=category: self._select_skill_category(value),
                height=27,
                corner_radius=14,
                border_width=0,
                fg_color="transparent",
                hover_color="#E8F2FF",
                text_color="#6E6E73",
                font=ctk.CTkFont(family=font, size=9),
            )
            button.grid(
                row=index // 2,
                column=index % 2,
                sticky="ew",
                padx=2,
                pady=2,
            )
            self._skill_category_buttons[category] = button

        self._skill_sidebar_list = ctk.CTkScrollableFrame(
            parent,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color="#D7D7DC",
            scrollbar_button_hover_color="#C7C7CC",
        )
        self._skill_sidebar_list.pack(fill="both", expand=True, padx=(10, 6), pady=(0, 8))
        self._skill_sidebar_list.grid_columnconfigure(0, weight=1)

        self._skill_cards = {}
        self._skill_favorite_buttons = {}
        for row, definition in enumerate(sorted(SKILL_CATALOG, key=self._skill_sort_key)):
            card = self._build_skill_card(
                self._skill_sidebar_list,
                row,
                definition,
                is_skill_enabled(self.config, definition.id),
            )
            self._skill_cards[definition.id] = card
            card.grid_remove()
        self._skill_empty_label = ctk.CTkLabel(
            self._skill_sidebar_list,
            text="",
            text_color="#8E8E93",
            wraplength=160,
            justify="center",
            font=ctk.CTkFont(family=font, size=11),
        )

        self._refresh_skill_count()
        self._select_skill_sidebar("learned")
        self._select_skill_category("全部")

    def _build_favorite_sidebar(
        self,
        parent,
        font: str,
        text: str,
        secondary: str,
        border: str,
        blue: str,
    ) -> None:
        title_bar = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        title_bar.pack(fill="x", padx=16, pady=(18, 10))
        ctk.CTkLabel(
            title_bar,
            text="收藏夹",
            text_color=text,
            font=ctk.CTkFont(family=font, size=20, weight="bold"),
        ).pack(side="left")
        self._favorite_count_label = ctk.CTkLabel(
            title_bar,
            text="",
            text_color=secondary,
            font=ctk.CTkFont(family=font, size=10),
        )
        self._favorite_count_label.pack(side="right")
        self._favorite_sidebar_list = ctk.CTkScrollableFrame(
            parent,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color="#D7D7DC",
            scrollbar_button_hover_color="#C7C7CC",
        )
        self._favorite_sidebar_list.pack(
            fill="both", expand=True, padx=(8, 5), pady=(0, 10)
        )
        self._favorite_sidebar_list.grid_columnconfigure(0, weight=1)
        self._favorite_cards = {}
        for row, definition in enumerate(sorted(SKILL_CATALOG, key=self._skill_sort_key)):
            card = self._build_favorite_card(
                self._favorite_sidebar_list, row, definition
            )
            self._favorite_cards[definition.id] = card
            card.grid_remove()
        self._favorite_empty_label = ctk.CTkLabel(
            self._favorite_sidebar_list,
            text="在技能栏点击 ☆\n即可收藏常用技能",
            text_color="#8E8E93",
            justify="center",
            font=ctk.CTkFont(family=font, size=10),
        )
        self._render_favorite_sidebar()

    def _restore_sidebar_visibility(self) -> None:
        ui_config = self.config.get("ui", {})
        self._skill_sidebar_expanded = bool(
            ui_config.get("skill_sidebar_expanded", True)
        )
        self._favorite_sidebar_expanded = bool(
            ui_config.get("favorite_sidebar_expanded", False)
        )
        self._apply_sidebar_visibility()

    def _apply_sidebar_visibility(self) -> None:
        self._cancel_sidebar_animation("skill")
        self._cancel_sidebar_animation("favorite")
        if self._skill_sidebar_expanded:
            self._skill_sidebar.configure(width=218)
            self._skill_sidebar.grid()
        else:
            self._skill_sidebar.grid_remove()
            self._skill_sidebar.configure(width=218)
        self._configure_sidebar_tab("skill", self._skill_sidebar_expanded)
        if self._favorite_sidebar_expanded:
            self._favorite_sidebar.configure(width=188)
            self._favorite_sidebar.grid()
        else:
            self._favorite_sidebar.grid_remove()
            self._favorite_sidebar.configure(width=188)
        self._configure_sidebar_tab("favorite", self._favorite_sidebar_expanded)

    def _configure_sidebar_tab(self, side: str, expanded: bool) -> None:
        if side == "skill":
            button = self._skill_sidebar_tab
            label = "❮" if expanded else "技\n能\n栏"
        else:
            button = self._favorite_sidebar_tab
            label = "❯" if expanded else "收\n藏\n夹"
        button.configure(
            text=label,
            width=32 if expanded else 28,
            font=self._sidebar_arrow_font if expanded else self._sidebar_label_font,
        )

    def _cancel_sidebar_animation(self, side: str) -> None:
        callback_id = self._sidebar_animation_ids.pop(side, None)
        if callback_id is not None:
            try:
                self.root.after_cancel(callback_id)
            except tk.TclError:
                pass

    def _animate_sidebar_visibility(self, side: str, expanded: bool) -> None:
        for active_side in tuple(self._sidebar_animation_ids):
            self._cancel_sidebar_animation(active_side)
        self._set_window_transparency(1.0)

        fade_steps = 5
        interval_ms = 18
        minimum_alpha = 0.92

        def apply_layout() -> None:
            if side == "skill":
                sidebar = self._skill_sidebar
                target_width = 218
            else:
                sidebar = self._favorite_sidebar
                target_width = 188
            sidebar.configure(width=target_width)
            if expanded:
                sidebar.grid()
            else:
                sidebar.grid_remove()
            self._configure_sidebar_tab(side, expanded)
            self.root.update_idletasks()

        def fade_in(index: int) -> None:
            if self._window_restore_pending:
                self._sidebar_animation_ids.pop(side, None)
                return
            progress = min(1.0, index / fade_steps)
            alpha = minimum_alpha + ((1.0 - minimum_alpha) * progress)
            self._set_window_transparency(alpha)
            if index < fade_steps:
                self._sidebar_animation_ids[side] = self.root.after(
                    interval_ms, fade_in, index + 1
                )
                return
            self._sidebar_animation_ids.pop(side, None)
            self._set_window_transparency(1.0)

        def fade_out(index: int) -> None:
            if self._window_restore_pending:
                self._sidebar_animation_ids.pop(side, None)
                return
            progress = min(1.0, index / fade_steps)
            self._set_window_transparency(1.0 - ((1.0 - minimum_alpha) * progress))
            if index < fade_steps:
                self._sidebar_animation_ids[side] = self.root.after(
                    interval_ms, fade_out, index + 1
                )
                return
            apply_layout()
            fade_in(1)

        fade_out(1)

    def _toggle_skill_sidebar_visibility(self) -> None:
        self._skill_sidebar_expanded = not self._skill_sidebar_expanded
        self.config.setdefault("ui", {})["skill_sidebar_expanded"] = (
            self._skill_sidebar_expanded
        )
        self._animate_sidebar_visibility("skill", self._skill_sidebar_expanded)
        self._save_ui_setting_async(
            "skill_sidebar_expanded", self._skill_sidebar_expanded
        )

    def _toggle_favorite_sidebar_visibility(self) -> None:
        self._favorite_sidebar_expanded = not self._favorite_sidebar_expanded
        self.config.setdefault("ui", {})["favorite_sidebar_expanded"] = (
            self._favorite_sidebar_expanded
        )
        self._animate_sidebar_visibility(
            "favorite", self._favorite_sidebar_expanded
        )
        self._save_ui_setting_async(
            "favorite_sidebar_expanded", self._favorite_sidebar_expanded
        )

    def _select_skill_sidebar(self, mode: str) -> None:
        self._skill_sidebar_mode = "library" if mode == "library" else "learned"
        learned_active = self._skill_sidebar_mode == "learned"
        self._learned_skills_button.configure(
            fg_color="#007AFF" if learned_active else "transparent",
            hover_color="#1687FF" if learned_active else "#E2E2E7",
            text_color="#FFFFFF" if learned_active else "#3A3A3C",
        )
        self._skill_library_button.configure(
            fg_color="transparent" if learned_active else "#007AFF",
            hover_color="#E2E2E7" if learned_active else "#1687FF",
            text_color="#3A3A3C" if learned_active else "#FFFFFF",
        )
        self._render_skill_sidebar()
        self._schedule_skill_scroll_reset()

    def _select_skill_category(self, category: str) -> None:
        self._skill_sidebar_category = (
            category if category in {"全部", *SKILL_CATEGORY_ORDER} else "全部"
        )
        for value, button in self._skill_category_buttons.items():
            active = value == self._skill_sidebar_category
            button.configure(
                fg_color="#E8F2FF" if active else "transparent",
                hover_color="#D8E9FF" if active else "#F0F0F3",
                text_color="#007AFF" if active else "#6E6E73",
            )
        self._render_skill_sidebar()
        self._schedule_skill_scroll_reset()

    def _schedule_skill_scroll_reset(self) -> None:
        pending = getattr(self, "_skill_scroll_reset_job", None)
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except tk.TclError:
                pass
        self._skill_scroll_reset_job = self.root.after_idle(
            self._reset_skill_scroll_position
        )

    def _reset_skill_scroll_position(self) -> None:
        self._skill_scroll_reset_job = None
        container = getattr(self, "_skill_sidebar_list", None)
        canvas = getattr(container, "_parent_canvas", None)
        if canvas is not None and canvas.winfo_exists():
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.yview_moveto(0.0)

    def _skill_settings_handler(self, skill_id: str):
        return {
            "wake-word": self._open_wake_word_settings,
            "continuous-conversation": self._open_continuous_conversation_settings,
            "desk-lamp": self._open_xiaomi_home_settings,
            "scheduled-tasks": self._open_schedule_manager,
            "local-apps": self._open_permission_manager,
            "starrail-dailies": self._open_starrail_settings,
        }.get(skill_id)

    def _skill_sort_key(self, definition) -> tuple[int, int]:
        return (
            0 if self._skill_settings_handler(definition.id) else 1,
            _SKILL_POSITION[definition.id],
        )

    def _render_skill_sidebar(self) -> None:
        container = getattr(self, "_skill_sidebar_list", None)
        if container is None or not container.winfo_exists():
            return

        show_enabled = self._skill_sidebar_mode == "learned"
        visible = [
            definition
            for definition in SKILL_CATALOG
            if is_skill_enabled(self.config, definition.id) == show_enabled
            and (
                self._skill_sidebar_category == "全部"
                or definition.category == self._skill_sidebar_category
            )
        ]
        for card in self._skill_cards.values():
            card.grid_remove()
        self._skill_empty_label.grid_remove()
        if not visible:
            self._skill_empty_label.configure(
                text=(
                    "这个分类还没有已学习技能"
                    if show_enabled
                    else "这个分类没有可学习技能"
                )
            )
            self._skill_empty_label.grid(row=0, column=0, pady=24)
            return

        for row, definition in enumerate(sorted(visible, key=self._skill_sort_key)):
            skill_id = definition.id
            self._skill_switch_vars[skill_id].set(
                is_skill_enabled(self.config, skill_id)
            )
            self._skill_favorite_buttons[skill_id].configure(
                text="★" if skill_id in self._favorite_skill_ids else "☆"
            )
            self._skill_cards[skill_id].grid(
                row=row, column=0, sticky="ew", pady=4
            )

    def _build_skill_card(
        self, container, row: int, definition, enabled: bool
    ):
        card = ctk.CTkFrame(
            container,
            fg_color="#F7F7F9",
            border_color="#E3E3E8",
            border_width=1,
            corner_radius=18,
        )
        card.grid(row=row, column=0, sticky="ew", pady=4)
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text=definition.name,
            text_color="#1D1D1F",
            font=ctk.CTkFont(
                family="Microsoft YaHei UI", size=12, weight="bold"
            ),
        ).grid(row=0, column=0, sticky="w", padx=(12, 4), pady=(10, 1))
        favorite_button = ctk.CTkButton(
            card,
            text="★" if definition.id in self._favorite_skill_ids else "☆",
            command=lambda skill_id=definition.id: self._toggle_skill_favorite(
                skill_id
            ),
            width=28,
            height=26,
            corner_radius=13,
            fg_color="transparent",
            hover_color="#FFF3D9",
            text_color="#F5A623",
            font=ctk.CTkFont(family="Segoe UI Symbol", size=14),
        )
        favorite_button.grid(
            row=0, column=1, sticky="e", padx=(2, 0), pady=(8, 0)
        )
        self._skill_favorite_buttons[definition.id] = favorite_button
        variable = self._skill_switch_vars[definition.id]
        variable.set(enabled)
        ctk.CTkSwitch(
            card,
            text="",
            variable=variable,
            command=lambda skill_id=definition.id, value=variable: self._toggle_skill_from_sidebar(
                skill_id, value.get()
            ),
            width=42,
            progress_color="#007AFF",
            button_color="#FFFFFF",
            button_hover_color="#F5F5F7",
        ).grid(row=0, column=2, sticky="e", padx=(2, 10), pady=(8, 0))
        ctk.CTkLabel(
            card,
            text=definition.description,
            text_color="#6E6E73",
            wraplength=155,
            justify="left",
            font=ctk.CTkFont(family="Microsoft YaHei UI", size=9),
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=12, pady=(1, 6))
        settings = self._skill_settings_handler(definition.id)
        if settings is not None:
            ctk.CTkButton(
                card,
                text="设置",
                command=settings,
                width=54,
                height=26,
                corner_radius=13,
                fg_color="#E8F2FF",
                hover_color="#D8E9FF",
                text_color="#007AFF",
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=9),
            ).grid(row=2, column=0, columnspan=3, sticky="w", padx=12, pady=(0, 10))
        return card

    def _render_favorite_sidebar(self) -> None:
        container = getattr(self, "_favorite_sidebar_list", None)
        if container is None or not container.winfo_exists():
            return
        favorites = [
            SKILLS_BY_ID[skill_id]
            for skill_id in self._favorite_skill_ids
            if skill_id in SKILLS_BY_ID
        ]
        favorites.sort(key=self._skill_sort_key)
        count_label = getattr(self, "_favorite_count_label", None)
        if count_label is not None:
            count_label.configure(text=str(len(favorites)))
        for card in self._favorite_cards.values():
            card.grid_remove()
        self._favorite_empty_label.grid_remove()
        if not favorites:
            self._favorite_empty_label.grid(row=0, column=0, pady=28)
            return
        for row, definition in enumerate(favorites):
            self._favorite_cards[definition.id].grid(
                row=row, column=0, sticky="ew", pady=4
            )

    def _build_favorite_card(self, container, row: int, definition):
        card = ctk.CTkFrame(
            container,
            fg_color="#F7F7F9",
            border_color="#E3E3E8",
            border_width=1,
            corner_radius=17,
        )
        card.grid(row=row, column=0, sticky="ew", pady=4)
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text=definition.name,
            text_color="#1D1D1F",
            font=ctk.CTkFont(
                family="Microsoft YaHei UI", size=11, weight="bold"
            ),
        ).grid(row=0, column=0, sticky="w", padx=(11, 2), pady=(9, 6))
        variable = self._skill_switch_vars[definition.id]
        ctk.CTkSwitch(
            card,
            text="",
            variable=variable,
            command=lambda skill_id=definition.id, value=variable: self._toggle_skill_from_sidebar(
                skill_id, value.get()
            ),
            width=38,
            progress_color="#007AFF",
            button_color="#FFFFFF",
            button_hover_color="#F5F5F7",
        ).grid(row=0, column=1, sticky="e", padx=(2, 8), pady=(6, 2))
        settings = self._skill_settings_handler(definition.id)
        action_bar = ctk.CTkFrame(card, fg_color="transparent", corner_radius=0)
        action_bar.grid(
            row=1, column=0, columnspan=2, sticky="ew", padx=9, pady=(0, 9)
        )
        action_bar.grid_columnconfigure(0, weight=1)
        if settings is not None:
            ctk.CTkButton(
                action_bar,
                text="设置",
                command=settings,
                height=25,
                corner_radius=13,
                fg_color="#E8F2FF",
                hover_color="#D8E9FF",
                text_color="#007AFF",
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=9),
            ).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ctk.CTkButton(
            action_bar,
            text="★",
            command=lambda skill_id=definition.id: self._toggle_skill_favorite(
                skill_id
            ),
            width=28,
            height=25,
            corner_radius=13,
            fg_color="#FFF3D9",
            hover_color="#FFE9B5",
            text_color="#F5A623",
            font=ctk.CTkFont(family="Segoe UI Symbol", size=13),
        ).grid(row=0, column=1, sticky="e")
        return card

    def _toggle_skill_favorite(self, skill_id: str) -> None:
        if skill_id in self._favorite_skill_ids:
            self._favorite_skill_ids.remove(skill_id)
        elif skill_id in SKILLS_BY_ID:
            self._favorite_skill_ids.append(skill_id)
        self.config.setdefault("ui", {})["favorite_skills"] = list(
            self._favorite_skill_ids
        )
        self._schedule_skill_panel_refresh()
        self._save_ui_setting_async(
            "favorite_skills", list(self._favorite_skill_ids)
        )

    @staticmethod
    def _save_ui_setting_async(key: str, value) -> None:
        threading.Thread(
            target=save_local_setting,
            args=("ui", key, value),
            daemon=True,
        ).start()

    def _schedule_skill_panel_refresh(self) -> None:
        pending = getattr(self, "_skill_panel_refresh_job", None)
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except tk.TclError:
                pass
        self._skill_panel_refresh_job = self.root.after_idle(
            self._run_skill_panel_refresh
        )

    def _run_skill_panel_refresh(self) -> None:
        self._skill_panel_refresh_job = None
        self._refresh_skill_panels()

    def _refresh_skill_panels(self) -> None:
        self._render_skill_sidebar()
        self._render_favorite_sidebar()

    def _toggle_skill_from_sidebar(self, skill_id: str, enabled: bool) -> None:
        self._set_skill_enabled(skill_id, enabled)
        self._schedule_skill_panel_refresh()

    def _refresh_skill_count(self) -> None:
        super()._refresh_skill_count()
        learned_button = getattr(self, "_learned_skills_button", None)
        library_button = getattr(self, "_skill_library_button", None)
        if learned_button is None or library_button is None:
            return
        learned = sum(
            1
            for definition in SKILL_CATALOG
            if is_skill_enabled(self.config, definition.id)
        )
        library = len(SKILL_CATALOG) - learned
        learned_button.configure(text=f"已学习 {learned}")
        library_button.configure(text=f"技能库 {library}")
        favorite_label = getattr(self, "_favorite_count_label", None)
        if favorite_label is not None:
            favorite_label.configure(text=str(len(getattr(self, "_favorite_skill_ids", []))))

    def _refresh_wake_capture_button(self) -> None:
        self._sync_wake_settings_controls()

    def _open_continuous_conversation_settings(self) -> None:
        existing = getattr(self, "_continuous_settings_window", None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            return
        window = ctk.CTkToplevel(self.root)
        self._continuous_settings_window = window
        window.title("连续对话")
        window.geometry("570x300+250+150")
        window.minsize(530, 280)
        window.configure(fg_color="#F5F5F7")
        window.transient(self.root)
        self.root.after(50, apply_windows_round_corners, window)
        font = "Microsoft YaHei UI"
        ctk.CTkLabel(
            window,
            text="连续对话",
            text_color="#1D1D1F",
            font=ctk.CTkFont(family=font, size=25, weight="bold"),
        ).pack(anchor="w", padx=28, pady=(24, 4))
        ctk.CTkLabel(
            window,
            text="开启后，猫猫会在回答结束后继续听你说话，直到你说再见或没事。",
            text_color="#6E6E73",
            wraplength=500,
            justify="left",
            font=ctk.CTkFont(family=font, size=12),
        ).pack(anchor="w", padx=28, pady=(0, 14))
        card = ctk.CTkFrame(
            window,
            fg_color="#FFFFFF",
            border_color="#E3E3E8",
            border_width=1,
            corner_radius=28,
        )
        card.pack(fill="both", expand=True, padx=22, pady=(0, 22))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text="回答后继续聆听",
            text_color="#1D1D1F",
            font=ctk.CTkFont(family=font, size=14, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=22, pady=22)
        ctk.CTkSwitch(
            card,
            text="",
            variable=self.continuous_enabled,
            command=lambda: self._toggle_skill_from_sidebar(
                "continuous-conversation", bool(self.continuous_enabled.get())
            ),
            width=48,
            progress_color="#007AFF",
        ).grid(row=0, column=1, sticky="e", padx=22, pady=22)

    def _open_wake_word_settings(self) -> None:
        existing = getattr(self, "_wake_settings_window", None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            return

        window = ctk.CTkToplevel(self.root)
        self._wake_settings_window = window
        window.title("唤醒词")
        window.geometry("600x460+230+100")
        window.minsize(560, 430)
        window.configure(fg_color="#F5F5F7")
        window.transient(self.root)
        self.root.after(50, apply_windows_round_corners, window)

        def close_window() -> None:
            window.destroy()
            self._wake_settings_window = None
            self._wake_settings_state_label = None
            self._wake_settings_entry = None
            self._wake_settings_save_button = None
            self._wake_settings_enroll_button = None
            if hasattr(self, "enroll_button"):
                del self.enroll_button

        window.protocol("WM_DELETE_WINDOW", close_window)
        font = "Microsoft YaHei UI"
        ctk.CTkLabel(
            window,
            text="唤醒词",
            text_color="#1D1D1F",
            font=ctk.CTkFont(family=font, size=25, weight="bold"),
        ).pack(anchor="w", padx=28, pady=(24, 4))
        ctk.CTkLabel(
            window,
            text="监听、唤醒词内容和声纹录制统一在这里管理。",
            text_color="#6E6E73",
            font=ctk.CTkFont(family=font, size=12),
        ).pack(anchor="w", padx=28, pady=(0, 14))

        card = ctk.CTkFrame(
            window,
            fg_color="#FFFFFF",
            border_color="#E3E3E8",
            border_width=1,
            corner_radius=28,
        )
        card.pack(fill="both", expand=True, padx=22, pady=(0, 22))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text="唤醒监听",
            text_color="#1D1D1F",
            font=ctk.CTkFont(family=font, size=14, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(18, 0))
        ctk.CTkSwitch(
            card,
            text="",
            variable=self.wake_capture_enabled,
            command=lambda: self._toggle_skill_from_sidebar(
                "wake-word", bool(self.wake_capture_enabled.get())
            ),
            width=48,
            progress_color="#007AFF",
        ).grid(row=0, column=1, sticky="e", padx=20, pady=(16, 0))
        self._wake_settings_state_label = ctk.CTkLabel(
            card,
            text="",
            text_color="#6E6E73",
            font=ctk.CTkFont(family=font, size=11),
        )
        self._wake_settings_state_label.grid(
            row=1, column=0, columnspan=2, sticky="w", padx=20, pady=(2, 14)
        )
        ctk.CTkLabel(
            card,
            text="唤醒词（多个词用逗号分隔）",
            text_color="#6E6E73",
            font=ctk.CTkFont(family=font, size=11),
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=20)
        self._wake_settings_entry = ctk.CTkEntry(
            card,
            height=40,
            corner_radius=20,
            border_width=0,
            fg_color="#F0F0F3",
            text_color="#1D1D1F",
            font=ctk.CTkFont(family=font, size=12),
        )
        self._wake_settings_entry.grid(
            row=3, column=0, sticky="ew", padx=(20, 10), pady=(7, 16)
        )
        self._wake_settings_entry.insert(0, "，".join(self.wake_listener.aliases))
        self._wake_settings_save_button = ctk.CTkButton(
            card,
            text="保存",
            command=lambda: self._save_wake_words(self._wake_settings_entry.get()),
            width=76,
            height=40,
            corner_radius=20,
            fg_color="#007AFF",
            hover_color="#1687FF",
            font=ctk.CTkFont(family=font, size=11, weight="bold"),
        )
        self._wake_settings_save_button.grid(
            row=3, column=1, sticky="e", padx=(0, 20), pady=(7, 16)
        )
        self._wake_settings_enroll_button = ctk.CTkButton(
            card,
            text="重新录制唤醒词" if self.wake_listener.enrolled else "录制唤醒词",
            command=self._enroll_wake_word,
            height=40,
            corner_radius=20,
            fg_color="#E8F2FF",
            hover_color="#D8E9FF",
            text_color="#007AFF",
            font=ctk.CTkFont(family=font, size=11, weight="bold"),
        )
        self._wake_settings_enroll_button.grid(
            row=4, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 20)
        )
        self.enroll_button = self._wake_settings_enroll_button
        self._sync_wake_settings_controls()

    def _sync_wake_settings_controls(self) -> None:
        label = getattr(self, "_wake_settings_state_label", None)
        entry = getattr(self, "_wake_settings_entry", None)
        save_button = getattr(self, "_wake_settings_save_button", None)
        enroll_button = getattr(self, "_wake_settings_enroll_button", None)
        if label is None or not label.winfo_exists():
            return
        enabled = bool(self.wake_capture_enabled.get())
        state = "normal" if enabled else "disabled"
        entry.configure(state=state)
        save_button.configure(state=state)
        enroll_button.configure(state=state)
        if not enabled:
            value = "已关闭，不会监听环境声音。手动录音仍可使用。"
        elif self.wake_listener.enrolled:
            value = f"已开启 · 正在监听“{self._wake_words_display()}”"
        else:
            value = "已开启 · 请先录制唤醒词"
        label.configure(text=value, text_color="#34C759" if enabled else "#8E8E93")

    def _open_xiaomi_home_settings(self) -> None:
        existing = getattr(self, "_xiaomi_settings_window", None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            self._refresh_xiaomi_settings_status()
            return

        window = ctk.CTkToplevel(self.root)
        self._xiaomi_settings_window = window
        window.title("小米智能家居")
        window.geometry("640x520+230+90")
        window.minsize(590, 470)
        window.configure(fg_color="#F5F5F7")
        window.transient(self.root)
        self.root.after(50, apply_windows_round_corners, window)
        font = "Microsoft YaHei UI"
        ctk.CTkLabel(
            window,
            text="小米智能家居",
            text_color="#1D1D1F",
            font=ctk.CTkFont(family=font, size=25, weight="bold"),
        ).pack(anchor="w", padx=28, pady=(24, 4))
        ctk.CTkLabel(
            window,
            text="连接米家设备后，猫猫可以在局域网内控制家具并读取设备状态。",
            text_color="#6E6E73",
            wraplength=570,
            justify="left",
            font=ctk.CTkFont(family=font, size=12),
        ).pack(anchor="w", padx=28, pady=(0, 14))
        card = ctk.CTkFrame(
            window,
            fg_color="#FFFFFF",
            border_color="#E3E3E8",
            border_width=1,
            corner_radius=28,
        )
        card.pack(fill="both", expand=True, padx=22, pady=(0, 22))
        self._xiaomi_settings_status = ctk.CTkLabel(
            card,
            text="",
            text_color="#1D1D1F",
            font=ctk.CTkFont(family=font, size=13, weight="bold"),
        )
        self._xiaomi_settings_status.pack(anchor="w", padx=22, pady=(18, 8))
        self._xiaomi_devices_frame = ctk.CTkFrame(
            card, fg_color="transparent", corner_radius=0
        )
        self._xiaomi_devices_frame.pack(fill="both", expand=True, padx=22, pady=(0, 12))
        self._xiaomi_setup_button = ctk.CTkButton(
            card,
            text="连接或更换设备",
            command=self._launch_xiaomi_setup_tool,
            height=42,
            corner_radius=21,
            fg_color="#007AFF",
            hover_color="#1687FF",
            font=ctk.CTkFont(family=font, size=12, weight="bold"),
        )
        self._xiaomi_setup_button.pack(fill="x", padx=22, pady=(0, 10))
        ctk.CTkButton(
            card,
            text="刷新设备列表",
            command=self._refresh_xiaomi_settings_status,
            height=36,
            corner_radius=18,
            fg_color="#F0F0F3",
            hover_color="#E2E2E7",
            text_color="#1D1D1F",
            font=ctk.CTkFont(family=font, size=11),
        ).pack(fill="x", padx=22, pady=(0, 20))
        self._refresh_xiaomi_settings_status()

    def _xiaomi_connected_devices(self) -> list[dict[str, str]]:
        token_path = Path(__file__).resolve().parent.parent / "xiaomi_token.txt"
        return self._read_xiaomi_devices(token_path)

    @staticmethod
    def _read_xiaomi_devices(token_path: Path) -> list[dict[str, str]]:
        if not token_path.is_file():
            return []
        values: dict[str, str] = {}
        for line in token_path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip().lower()] = value.strip()
        token = values.get("token", "")
        valid = bool(values.get("ip")) and len(token) == 32 and all(
            character in "0123456789abcdefABCDEF" for character in token
        )
        if not valid:
            return []
        return [{key: values.get(key, "") for key in ("did", "name", "model", "ip")}]

    def _xiaomi_token_configured(self) -> bool:
        return bool(self._xiaomi_connected_devices())

    def _refresh_xiaomi_settings_status(self) -> None:
        label = getattr(self, "_xiaomi_settings_status", None)
        if label is None or not label.winfo_exists():
            return
        devices = self._xiaomi_connected_devices()
        label.configure(
            text=f"已连接的家具 · {len(devices)}" if devices else "尚未连接家具",
            text_color="#34C759" if devices else "#8E8E93",
        )
        container = getattr(self, "_xiaomi_devices_frame", None)
        if container is None or not container.winfo_exists():
            return
        for child in container.winfo_children():
            child.destroy()
        if not devices:
            ctk.CTkLabel(
                container,
                text="连接设备后，它会显示在这里。",
                text_color="#8E8E93",
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=11),
            ).pack(anchor="w", pady=10)
            return
        for device in devices:
            device_card = ctk.CTkFrame(
                container,
                fg_color="#F7F7F9",
                border_color="#E3E3E8",
                border_width=1,
                corner_radius=18,
            )
            device_card.pack(fill="x", pady=4)
            name = device.get("name") or "米家设备"
            details = " · ".join(
                value
                for value in (
                    device.get("model") or "未知型号",
                    device.get("ip") or "IP 未知",
                )
                if value
            )
            ctk.CTkLabel(
                device_card,
                text=name,
                text_color="#1D1D1F",
                font=ctk.CTkFont(
                    family="Microsoft YaHei UI", size=12, weight="bold"
                ),
            ).pack(anchor="w", padx=14, pady=(10, 2))
            ctk.CTkLabel(
                device_card,
                text=details,
                text_color="#6E6E73",
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=10),
            ).pack(anchor="w", padx=14, pady=(0, 10))

    def _launch_xiaomi_setup_tool(self) -> None:
        python = Path(sys.executable)
        console_python = python.with_name("python.exe")
        if console_python.is_file():
            python = console_python
        self._xiaomi_setup_button.configure(state="disabled", text="连接工具已打开")
        process = subprocess.Popen(
            [str(python), "-m", "assistant_app.adapters.xiaomi_token_setup"],
            cwd=str(Path(__file__).resolve().parent.parent),
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )

        def wait_for_tool() -> None:
            process.wait()
            if not self.closing:
                self.root.after(0, self._finish_xiaomi_setup_tool)

        threading.Thread(target=wait_for_tool, name="xiaomi-setup", daemon=True).start()

    def _finish_xiaomi_setup_tool(self) -> None:
        button = getattr(self, "_xiaomi_setup_button", None)
        if button is not None and button.winfo_exists():
            button.configure(state="normal", text="连接或更换设备")
        self._refresh_xiaomi_settings_status()

    def _open_starrail_settings(self) -> None:
        existing = getattr(self, "_starrail_settings_window", None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            self._refresh_starrail_settings_status()
            return
        window = ctk.CTkToplevel(self.root)
        self._starrail_settings_window = window
        window.title("星铁日常")
        window.geometry("620x410+230+120")
        window.minsize(570, 380)
        window.configure(fg_color="#F5F5F7")
        window.transient(self.root)
        self.root.after(50, apply_windows_round_corners, window)
        font = "Microsoft YaHei UI"
        ctk.CTkLabel(
            window,
            text="星铁日常",
            text_color="#1D1D1F",
            font=ctk.CTkFont(family=font, size=25, weight="bold"),
        ).pack(anchor="w", padx=28, pady=(24, 4))
        ctk.CTkLabel(
            window,
            text="定位三月七小助手后，猫猫可以启动它并点击“完整运行”。",
            text_color="#6E6E73",
            wraplength=550,
            justify="left",
            font=ctk.CTkFont(family=font, size=12),
        ).pack(anchor="w", padx=28, pady=(0, 14))
        card = ctk.CTkFrame(
            window,
            fg_color="#FFFFFF",
            border_color="#E3E3E8",
            border_width=1,
            corner_radius=28,
        )
        card.pack(fill="both", expand=True, padx=22, pady=(0, 22))
        self._starrail_settings_status = ctk.CTkLabel(
            card,
            text="",
            text_color="#6E6E73",
            wraplength=520,
            justify="left",
            font=ctk.CTkFont(family=font, size=11),
        )
        self._starrail_settings_status.pack(anchor="w", padx=22, pady=(20, 12))
        ctk.CTkButton(
            card,
            text="打开项目页面  ↗",
            command=lambda: webbrowser.open(
                SKILLS_BY_ID["starrail-dailies"].project_url, new=0
            ),
            height=40,
            corner_radius=20,
            fg_color="#E8F2FF",
            hover_color="#D8E9FF",
            text_color="#007AFF",
            font=ctk.CTkFont(family=font, size=11, weight="bold"),
        ).pack(fill="x", padx=22, pady=(0, 10))
        ctk.CTkButton(
            card,
            text="定位已安装的 March7th Launcher.exe",
            command=self._choose_starrail_executable,
            height=40,
            corner_radius=20,
            fg_color="#007AFF",
            hover_color="#1687FF",
            font=ctk.CTkFont(family=font, size=11, weight="bold"),
        ).pack(fill="x", padx=22, pady=(0, 20))
        self._refresh_starrail_settings_status()

    def _starrail_executable(self) -> str:
        automation = self.config.get("automation", {})
        skill_config = automation.get("starrail_dailies", {}) if isinstance(automation, dict) else {}
        return str(skill_config.get("executable") or "") if isinstance(skill_config, dict) else ""

    def _refresh_starrail_settings_status(self) -> None:
        label = getattr(self, "_starrail_settings_status", None)
        if label is None or not label.winfo_exists():
            return
        executable = self._starrail_executable()
        path = Path(executable) if executable else None
        configured = bool(path and path.is_file())
        label.configure(
            text=f"已定位：{path}" if configured else "尚未定位三月七小助手。",
            text_color="#34C759" if configured else "#8E8E93",
        )

    def _choose_starrail_executable(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择 March7th Launcher.exe",
            filetypes=[("Windows 应用", "*.exe"), ("所有文件", "*.*")],
            parent=self._starrail_settings_window,
        )
        if not selected:
            return
        path = Path(selected)
        if path.suffix.lower() != ".exe":
            messagebox.showerror(
                "无法使用该文件", "请选择 March7th Launcher.exe。", parent=self._starrail_settings_window
            )
            return
        skill_config = {
            "enabled": True,
            "executable": str(path),
            "requires_admin": True,
            "button_text": "完整运行",
        }
        self.config.setdefault("automation", {})["starrail_dailies"] = skill_config
        save_local_setting("automation", "starrail_dailies", skill_config)
        self._set_skill_enabled("starrail-dailies", True)
        self._refresh_starrail_settings_status()

    def _open_about_page(self) -> None:
        existing = getattr(self, "_about_window", None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            return
        window = ctk.CTkToplevel(self.root)
        self._about_window = window
        window.title("关于猫猫")
        window.geometry("620x520+220+100")
        window.minsize(560, 480)
        window.configure(fg_color="#F5F5F7")
        window.transient(self.root)
        self.root.after(50, apply_windows_round_corners, window)
        font = "Microsoft YaHei UI"

        hero = ctk.CTkFrame(
            window,
            fg_color="#FFFFFF",
            border_color="#E3E3E8",
            border_width=1,
            corner_radius=38,
        )
        hero.pack(fill="x", padx=26, pady=(26, 14))
        ctk.CTkLabel(
            hero,
            text="喵~",
            text_color="#1D1D1F",
            font=ctk.CTkFont(family=font, size=34, weight="bold"),
        ).pack(anchor="w", padx=28, pady=(24, 2))
        ctk.CTkLabel(
            hero,
            text="猫猫是一只住在你电脑里的私人语音助手。",
            text_color="#6E6E73",
            font=ctk.CTkFont(family=font, size=14),
        ).pack(anchor="w", padx=28, pady=(0, 20))

        facts = ctk.CTkFrame(
            window,
            fg_color="#FFFFFF",
            border_color="#E3E3E8",
            border_width=1,
            corner_radius=30,
        )
        facts.pack(fill="both", expand=True, padx=26, pady=(0, 26))
        rows = (
            ("生日", "2026 年 8 月 9 日"),
            ("开发者", "MaoMao contributors"),
            ("会做什么", "语音对话、本地记忆、电脑操作、定时任务与可管理技能"),
            ("隐私", "记忆与权限保存在本机；敏感操作仍由你确认"),
        )
        for row, (title, value) in enumerate(rows):
            ctk.CTkLabel(
                facts,
                text=title,
                text_color="#8E8E93",
                font=ctk.CTkFont(family=font, size=11),
            ).grid(row=row, column=0, sticky="nw", padx=(24, 18), pady=(18 if row == 0 else 12, 8))
            ctk.CTkLabel(
                facts,
                text=value,
                text_color="#1D1D1F",
                font=ctk.CTkFont(family=font, size=13, weight="bold" if row < 2 else "normal"),
                wraplength=390,
                justify="left",
            ).grid(row=row, column=1, sticky="nw", padx=(0, 24), pady=(18 if row == 0 else 12, 8))
        facts.grid_columnconfigure(1, weight=1)

    def _open_api_key_page(self) -> None:
        existing = getattr(self, "_api_keys_window", None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            return
        window = ctk.CTkToplevel(self.root)
        self._api_keys_window = window
        window.title("API 密钥")
        window.geometry("620x470+220+110")
        window.minsize(560, 430)
        window.configure(fg_color="#F5F5F7")
        window.transient(self.root)
        self.root.after(50, apply_windows_round_corners, window)
        font = "Microsoft YaHei UI"
        status = key_configuration_status(app_paths().key_file)

        header = ctk.CTkFrame(window, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(24, 14))
        ctk.CTkLabel(
            header,
            text="API 密钥",
            text_color="#1D1D1F",
            font=ctk.CTkFont(family=font, size=25, weight="bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            header,
            text="输入内容只保存在本机 api_key.txt；留空会保留原值，页面不会显示已有密钥。",
            text_color="#6E6E73",
            font=ctk.CTkFont(family=font, size=11),
            wraplength=540,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        card = ctk.CTkFrame(
            window,
            fg_color="#FFFFFF",
            border_color="#E3E3E8",
            border_width=1,
            corner_radius=30,
        )
        card.pack(fill="both", expand=True, padx=26, pady=(0, 26))
        card.grid_columnconfigure(0, weight=1)
        self._api_key_entries = {}
        providers = (
            ("kimi_key", "Kimi API Key", status["kimi_key"], status["kimi_key_from_environment"]),
            ("mimo_key", "MiMo API Key", status["mimo_key"], status["mimo_key_from_environment"]),
        )
        for row, (name, label, configured, from_environment) in enumerate(providers):
            state_text = "环境变量已配置" if from_environment else ("已配置" if configured else "未配置")
            ctk.CTkLabel(
                card,
                text=f"{label}  ·  {state_text}",
                text_color="#1D1D1F",
                font=ctk.CTkFont(family=font, size=12, weight="bold"),
            ).grid(row=row * 2, column=0, sticky="w", padx=22, pady=(18 if row == 0 else 12, 5))
            entry = ctk.CTkEntry(
                card,
                placeholder_text="输入新的 Key（留空则不修改）",
                show="•",
                height=38,
                corner_radius=19,
                border_width=0,
                fg_color="#F0F0F3",
                font=ctk.CTkFont(family=font, size=12),
            )
            entry.grid(row=row * 2 + 1, column=0, sticky="ew", padx=22)
            self._api_key_entries[name] = entry

        ctk.CTkButton(
            card,
            text="保存",
            command=self._save_api_keys_from_ui,
            height=40,
            corner_radius=20,
            fg_color="#007AFF",
            hover_color="#1687FF",
            font=ctk.CTkFont(family=font, size=12, weight="bold"),
        ).grid(row=4, column=0, sticky="ew", padx=22, pady=22)

    def _save_api_keys_from_ui(self) -> None:
        entries = getattr(self, "_api_key_entries", {})
        try:
            save_api_keys(
                app_paths().key_file,
                kimi_key=entries["kimi_key"].get(),
                mimo_key=entries["mimo_key"].get(),
            )
        except (KeyError, ValueError, OSError) as exc:
            messagebox.showerror("无法保存", str(exc), parent=self._api_keys_window)
            return
        for entry in entries.values():
            entry.delete(0, "end")
        messagebox.showinfo(
            "已保存",
            "API 密钥已保存在本机。请从托盘选择“重启猫猫”让所有连接使用新 Key。",
            parent=self._api_keys_window,
        )

    def _managed_permission_items(self) -> list[dict]:
        items = self._permission_records()
        configured_apps = self.config.get("applications", {}).get("allowlist", {})
        if isinstance(configured_apps, dict):
            items.extend(
                {
                    "key": f"application:{name}",
                    "tool": "open_app",
                    "category": "应用与窗口",
                    "label": f"已允许应用 · {name}",
                    "created_at": "应用白名单",
                }
                for name in sorted(configured_apps)
            )
        database = Database(app_paths().database)
        try:
            shortcuts = database.list_visual_actions(100)
        finally:
            database.close()
        items.extend(
            {
                "key": f"visual:{item['id']}",
                "tool": "run_visual_shortcut",
                "category": "常用动作",
                "label": f"{item['app_name']} · {item['description']}",
                "created_at": f"使用 {item['use_count']} 次",
            }
            for item in shortcuts
        )
        return items

    def _open_permission_manager(self) -> None:
        existing = getattr(self, "_permissions_window", None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            self._refresh_permission_manager(getattr(self, "_permission_selected_category", "全部"))
            return

        window = ctk.CTkToplevel(self.root)
        self._permissions_window = window
        window.title("权限与常用动作")
        window.geometry("760x620+170+70")
        window.minsize(650, 520)
        window.configure(fg_color="#F5F5F7")
        window.transient(self.root)
        self.root.after(50, apply_windows_round_corners, window)
        font = "Microsoft YaHei UI"

        header = ctk.CTkFrame(window, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(24, 12))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text="权限与常用动作",
            text_color="#1D1D1F",
            font=ctk.CTkFont(family=font, size=25, weight="bold"),
        ).grid(row=0, column=0, sticky="w")
        self._permission_count_label = ctk.CTkLabel(
            header,
            text="",
            text_color="#007AFF",
            fg_color="#E8F2FF",
            corner_radius=14,
            height=28,
            padx=12,
            font=ctk.CTkFont(family=font, size=11, weight="bold"),
        )
        self._permission_count_label.grid(row=0, column=1, sticky="e")
        ctk.CTkLabel(
            header,
            text="普通操作确认一次后会记住；高风险操作仍会每次确认。删除后下次会重新询问。",
            text_color="#6E6E73",
            font=ctk.CTkFont(family=font, size=12),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        categories = ["全部", "网站", "应用与窗口", "自动化", "常用动作", "其他"]
        category_bar = ctk.CTkFrame(window, fg_color="transparent")
        category_bar.pack(fill="x", padx=22, pady=(0, 10))
        for column in range(3):
            category_bar.grid_columnconfigure(column, weight=1)
        self._permission_category_buttons = {}
        for index, category in enumerate(categories):
            button = ctk.CTkButton(
                category_bar,
                text=category,
                command=lambda value=category: self._refresh_permission_manager(value),
                height=30,
                corner_radius=15,
                font=ctk.CTkFont(family=font, size=11),
            )
            button.grid(
                row=index // 3,
                column=index % 3,
                sticky="ew",
                padx=4,
                pady=3,
            )
            self._permission_category_buttons[category] = button

        self._permission_list = ctk.CTkScrollableFrame(
            window, fg_color="transparent", corner_radius=0
        )
        self._permission_list.pack(fill="both", expand=True, padx=22, pady=(0, 22))
        self._permission_list.grid_columnconfigure(0, weight=1)
        self._refresh_permission_manager("全部")

    def _delete_managed_permission(self, key: str) -> None:
        if key.startswith("visual:"):
            database = Database(app_paths().database)
            try:
                database.delete_visual_action(int(key.split(":", 1)[1]))
            finally:
                database.close()
        elif key.startswith("application:"):
            app_name = key.split(":", 1)[1]
            applications = self.config.setdefault("applications", {})
            allowlist = applications.get("allowlist", {})
            if isinstance(allowlist, dict):
                allowlist.pop(app_name, None)
                applications["allowlist"] = allowlist
                save_local_setting("applications", "allowlist", allowlist)
        else:
            self._remove_permission(key)
        self._refresh_permission_manager(getattr(self, "_permission_selected_category", "全部"))

    def _refresh_permission_manager(self, selected: str = "全部") -> None:
        window = getattr(self, "_permissions_window", None)
        container = getattr(self, "_permission_list", None)
        if window is None or container is None or not window.winfo_exists():
            return
        self._permission_selected_category = selected
        items = self._managed_permission_items()
        visible = [
            item for item in items
            if selected == "全部" or str(item.get("category")) == selected
        ]
        self._permission_count_label.configure(text=f"共 {len(items)} 个")
        for name, button in self._permission_category_buttons.items():
            active = name == selected
            button.configure(
                fg_color="#007AFF" if active else "#ECECF0",
                hover_color="#1687FF" if active else "#E1E1E6",
                text_color="#FFFFFF" if active else "#3A3A3C",
            )
        for child in container.winfo_children():
            child.destroy()
        if not visible:
            ctk.CTkLabel(
                container,
                text="这里还没有记录",
                text_color="#8E8E93",
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=13),
            ).grid(row=0, column=0, pady=28)
            return
        for row, item in enumerate(visible):
            card = ctk.CTkFrame(
                container,
                fg_color="#FFFFFF",
                border_color="#E3E3E8",
                border_width=1,
                corner_radius=24,
            )
            card.grid(row=row, column=0, sticky="ew", pady=4)
            card.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                card,
                text=str(item.get("label") or item.get("key")),
                text_color="#1D1D1F",
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=13, weight="bold"),
                wraplength=500,
                justify="left",
            ).grid(row=0, column=0, sticky="w", padx=18, pady=(12, 2))
            ctk.CTkLabel(
                card,
                text=f"{item.get('category', '其他')} · {item.get('created_at', '')}",
                text_color="#6E6E73",
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=11),
            ).grid(row=1, column=0, sticky="w", padx=18, pady=(0, 12))
            ctk.CTkButton(
                card,
                text="删除",
                command=lambda value=str(item.get("key") or ""): self._delete_managed_permission(value),
                width=62,
                height=30,
                corner_radius=15,
                fg_color="#FFE8E7",
                hover_color="#FFDAD8",
                text_color="#D70015",
            ).grid(row=0, column=1, rowspan=2, sticky="e", padx=16)

    def _open_schedule_manager(self) -> None:
        existing = getattr(self, "_schedule_window", None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            self._refresh_schedule_manager()
            return

        window = ctk.CTkToplevel(self.root)
        self._schedule_window = window
        window.title("定时任务")
        window.geometry("760x650+170+70")
        window.minsize(650, 540)
        window.configure(fg_color="#F5F5F7")
        window.transient(self.root)
        self.root.after(50, apply_windows_round_corners, window)

        font = "Microsoft YaHei UI"
        header = ctk.CTkFrame(window, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(24, 12))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text="定时任务",
            text_color="#1D1D1F",
            font=ctk.CTkFont(family=font, size=25, weight="bold"),
        ).grid(row=0, column=0, sticky="w")
        self._schedule_count_label = ctk.CTkLabel(
            header,
            text="",
            text_color="#007AFF",
            fg_color="#E8F2FF",
            corner_radius=14,
            height=28,
            padx=12,
            font=ctk.CTkFont(family=font, size=11, weight="bold"),
        )
        self._schedule_count_label.grid(row=0, column=1, sticky="e")
        ctk.CTkLabel(
            header,
            text="猫猫在运行或缩到托盘时会按本机时间执行；普通任务也不会额外语音通知。",
            text_color="#6E6E73",
            font=ctk.CTkFont(family=font, size=12),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        editor = ctk.CTkFrame(
            window,
            fg_color="#FFFFFF",
            border_color="#E3E3E8",
            border_width=1,
            corner_radius=24,
        )
        editor.pack(fill="x", padx=22, pady=(0, 12))
        editor.grid_columnconfigure(0, weight=1)
        editor.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            editor,
            text="任务内容",
            text_color="#6E6E73",
            font=ctk.CTkFont(family=font, size=11),
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=18, pady=(14, 4))
        self._schedule_command_entry = ctk.CTkEntry(
            editor,
            placeholder_text="例如：帮我过星铁日常",
            height=38,
            corner_radius=19,
            border_width=0,
            fg_color="#F0F0F3",
            font=ctk.CTkFont(family=font, size=12),
        )
        self._schedule_command_entry.grid(
            row=1, column=0, columnspan=2, sticky="ew", padx=18
        )
        ctk.CTkLabel(
            editor,
            text="首次执行（YYYY-MM-DD HH:MM）",
            text_color="#6E6E73",
            font=ctk.CTkFont(family=font, size=11),
        ).grid(row=2, column=0, sticky="w", padx=(18, 8), pady=(11, 4))
        ctk.CTkLabel(
            editor,
            text="重复方式",
            text_color="#6E6E73",
            font=ctk.CTkFont(family=font, size=11),
        ).grid(row=2, column=1, sticky="w", padx=(8, 18), pady=(11, 4))
        self._schedule_time_entry = ctk.CTkEntry(
            editor,
            height=36,
            corner_radius=18,
            border_width=0,
            fg_color="#F0F0F3",
            font=ctk.CTkFont(family=font, size=12),
        )
        self._schedule_time_entry.grid(row=3, column=0, sticky="ew", padx=(18, 8))
        next_hour = (datetime.now() + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        self._schedule_time_entry.insert(0, next_hour.strftime("%Y-%m-%d %H:%M"))
        self._schedule_repeat = tk.StringVar(value="每天")
        ctk.CTkOptionMenu(
            editor,
            values=["仅一次", "每天"],
            variable=self._schedule_repeat,
            height=36,
            corner_radius=18,
            fg_color="#F0F0F3",
            button_color="#E4E4E9",
            button_hover_color="#D9D9DF",
            text_color="#1D1D1F",
            font=ctk.CTkFont(family=font, size=12),
        ).grid(row=3, column=1, sticky="ew", padx=(8, 18))
        self._schedule_silent = tk.BooleanVar(value=True)
        ctk.CTkSwitch(
            editor,
            text="静默执行（不播报、不弹出窗口）",
            variable=self._schedule_silent,
            progress_color="#007AFF",
            text_color="#3A3A3C",
            font=ctk.CTkFont(family=font, size=11),
        ).grid(row=4, column=0, sticky="w", padx=18, pady=16)
        ctk.CTkButton(
            editor,
            text="添加任务",
            command=self._create_schedule_from_ui,
            height=38,
            corner_radius=19,
            width=120,
            fg_color="#007AFF",
            hover_color="#1687FF",
            font=ctk.CTkFont(family=font, size=12, weight="bold"),
        ).grid(row=4, column=1, sticky="e", padx=18, pady=12)

        self._schedule_list = ctk.CTkScrollableFrame(
            window,
            fg_color="transparent",
            corner_radius=0,
        )
        self._schedule_list.pack(fill="both", expand=True, padx=22, pady=(0, 22))
        self._schedule_list.grid_columnconfigure(0, weight=1)
        self._refresh_schedule_manager()

    def _create_schedule_from_ui(self) -> None:
        command = self._schedule_command_entry.get().strip()
        raw_time = self._schedule_time_entry.get().strip()
        if not command:
            messagebox.showwarning("缺少任务", "请填写到点后要做的事情。", parent=self._schedule_window)
            return
        try:
            run_at = datetime.strptime(raw_time, "%Y-%m-%d %H:%M")
            database = Database(app_paths().database)
            try:
                database.create_scheduled_task(
                    command,
                    run_at,
                    repeat_rule="daily" if self._schedule_repeat.get() == "每天" else "once",
                    silent=self._schedule_silent.get(),
                )
            finally:
                database.close()
        except ValueError as exc:
            messagebox.showerror("无法添加", str(exc), parent=self._schedule_window)
            return
        self._schedule_command_entry.delete(0, "end")
        self.status.set("定时任务已添加")
        self._refresh_schedule_manager()

    def _cancel_schedule_from_ui(self, task_id: int) -> None:
        if getattr(self, "_active_scheduled_task_id", 0) == task_id:
            self._scheduled_task_cancel_event.set()
        database = Database(app_paths().database)
        try:
            database.cancel_scheduled_task(task_id)
        finally:
            database.close()
        self._refresh_schedule_manager()

    def _delete_schedule_from_ui(self, task_id: int) -> None:
        if not messagebox.askyesno(
            "删除定时任务",
            f"确定删除定时任务 #{task_id} 吗？",
            parent=self._schedule_window,
        ):
            return
        database = Database(app_paths().database)
        try:
            database.delete_scheduled_task(task_id)
        finally:
            database.close()
        self._refresh_schedule_manager()

    def _refresh_schedule_manager(self) -> None:
        window = getattr(self, "_schedule_window", None)
        task_list = getattr(self, "_schedule_list", None)
        if window is None or task_list is None or not window.winfo_exists():
            return
        database = Database(app_paths().database)
        try:
            tasks = database.list_scheduled_tasks(include_disabled=True)
        finally:
            database.close()
        enabled_count = sum(bool(task["enabled"]) for task in tasks)
        self._schedule_count_label.configure(text=f"启用 {enabled_count} / 共 {len(tasks)} 个")
        for child in task_list.winfo_children():
            child.destroy()
        if not tasks:
            ctk.CTkLabel(
                task_list,
                text="还没有定时任务",
                text_color="#8E8E93",
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=13),
            ).grid(row=0, column=0, pady=28)
            return
        status_names = {
            "pending": "等待执行",
            "running": "执行中",
            "succeeded": "上次成功",
            "failed": "上次失败",
            "cancelled": "已停用",
        }
        for row, task in enumerate(tasks):
            card = ctk.CTkFrame(
                task_list,
                fg_color="#FFFFFF",
                border_color="#E3E3E8",
                border_width=1,
                corner_radius=24,
            )
            card.grid(row=row, column=0, sticky="ew", pady=4)
            card.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                card,
                text=f"#{task['id']}  {task['command']}",
                text_color="#1D1D1F",
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=13, weight="bold"),
                wraplength=470,
                justify="left",
            ).grid(row=0, column=0, sticky="w", padx=18, pady=(12, 2))
            repeat_label = "每天" if task["repeat_rule"] == "daily" else "仅一次"
            mode = "静默" if task["silent"] else "普通"
            state = status_names.get(str(task["last_status"]), str(task["last_status"]))
            ctk.CTkLabel(
                card,
                text=f"{repeat_label} · {task['next_run_at'][:16].replace('T', ' ')} · {mode} · {state}",
                text_color="#6E6E73",
                font=ctk.CTkFont(family="Microsoft YaHei UI", size=11),
            ).grid(row=1, column=0, sticky="w", padx=18, pady=(0, 12))
            actions = ctk.CTkFrame(card, fg_color="transparent")
            actions.grid(row=0, column=1, rowspan=2, sticky="e", padx=14)
            if task["enabled"]:
                ctk.CTkButton(
                    actions,
                    text="停用",
                    command=lambda value=int(task["id"]): self._cancel_schedule_from_ui(value),
                    width=58,
                    height=30,
                    corner_radius=15,
                    fg_color="#ECECF0",
                    hover_color="#E1E1E6",
                    text_color="#3A3A3C",
                ).pack(side="left", padx=3)
            ctk.CTkButton(
                actions,
                text="删除",
                command=lambda value=int(task["id"]): self._delete_schedule_from_ui(value),
                width=58,
                height=30,
                corner_radius=15,
                fg_color="#FFE8E7",
                hover_color="#FFDAD8",
                text_color="#D70015",
            ).pack(side="left", padx=3)


def main() -> int:
    if not _acquire_single_instance():
        return 0
    # CustomTkinter performs its own DPI scaling; applying Tk scaling a second
    # time makes cards overflow on 150% Windows displays.
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")
    ctk.set_widget_scaling(1.0)
    ctk.set_window_scaling(1.0)
    configure_windows_app_identity()
    root = ctk.CTk()

    def open_main_window() -> None:
        window = RoundedAssistantWindow(root)
        key_status = key_configuration_status(app_paths().key_file)
        if not key_status["kimi_key"] and not key_status["mimo_key"]:
            root.after(450, window._open_api_key_page)

    missing_components = missing_startup_components()
    if missing_components:
        StartupComponentView(root, missing_components, open_main_window)
    else:
        open_main_window()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
