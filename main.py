"""Minimal drag-and-drop interface for Dpyle."""

from __future__ import annotations

from io import BytesIO
import threading
import sys
import os
import subprocess
import xml.etree.ElementTree as ET
import re
import winreg
from pathlib import Path
import tkinter as tk

import customtkinter as ctk
import requests
from PIL import Image, ImageDraw, ImageOps
from pygments import lex
from pygments.lexers import ClassNotFound, TextLexer, get_lexer_for_filename
from pygments.token import Comment, Keyword, Name, Number, Operator, String, Token
from tkinterdnd2 import DND_FILES, TkinterDnD

from backend import (
    WORK_FOLDER,
    DiscordWebhook,
    DiscordWebhookCheck,
    check_discord_webhook,
    decompile_with_api,
    decompiled_file_for,
    find_discord_webhooks,
    hidden_files,
    launch_extractor,
    record_decompilation,
    extract_resources,
    resource_folders,
    resource_file_for_member,
    reset_work_folder,
    send_discord_webhook_message,
)


TRANSLATIONS_FILE = Path(__file__).resolve().parent / "translations.xml"


def load_translation_catalog() -> dict[str, dict[str, str]]:
    root = ET.parse(TRANSLATIONS_FILE).getroot()
    catalog = {
        "fr": {
            element.attrib["id"]: element.text or ""
            for element in root.findall("text")
        }
    }
    for language in root.findall("language"):
        code = language.attrib["code"]
        catalog[code] = {
            element.attrib["id"]: element.text or ""
            for element in language.findall("text")
        }
    return catalog


TRANSLATION_CATALOG = load_translation_catalog()
TRANSLATIONS = TRANSLATION_CATALOG["fr"]


def t(key: str, **values: object) -> str:
    return TRANSLATIONS[key].format(**values)


def file_tree_sort_key(
    entry: Path,
    resource_directories: dict[str, str],
) -> tuple[bool, str]:
    relative_path = entry.relative_to(WORK_FOLDER).as_posix()
    source_file = resource_directories.get(relative_path)
    if source_file:
        return True, Path(source_file).name.lower()
    return entry.is_file(), entry.name.lower()


class SmoothScrollableFrame(ctk.CTkScrollableFrame):
    """Scrollable frame with animated vertical wheel scrolling."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._smooth_scroll_job: str | None = None
        self._smooth_scroll_target = 0.0

    def _mouse_wheel_all(self, event: tk.Event) -> str | None:
        if not self._check_if_valid_scroll(event.widget):
            return

        canvas = self._parent_canvas
        if sys.platform.startswith("win"):
            if self._shift_pressed:
                return
            scroll_pixels = -(event.delta / 120) * 45
        elif sys.platform == "darwin":
            if self._shift_pressed:
                return
            scroll_pixels = -event.delta * 6
        else:
            scroll_pixels = -1 * 45 if event.num == 4 else 45

        scroll_region = canvas.bbox("all")
        if scroll_region is None or canvas.winfo_height() <= 0:
            return
        content_height = scroll_region[3] - scroll_region[1]
        viewport_height = canvas.winfo_height()
        max_scroll = max(0, content_height - viewport_height)
        if max_scroll <= 0:
            return

        if self._smooth_scroll_job is None:
            self._smooth_scroll_target = canvas.yview()[0]
        self._smooth_scroll_target = min(
            max(self._smooth_scroll_target + (scroll_pixels / content_height), 0.0),
            1.0,
        )
        if self._smooth_scroll_job is None:
            self._animate_scroll()
        return "break"

    def _animate_scroll(self) -> None:
        canvas = self._parent_canvas
        scroll_region = canvas.bbox("all")
        if scroll_region is None:
            self._smooth_scroll_job = None
            return

        content_height = scroll_region[3] - scroll_region[1]
        max_scroll = max(0, content_height - canvas.winfo_height())
        if max_scroll <= 0:
            self._smooth_scroll_job = None
            return

        current_scroll = canvas.yview()[0]
        distance = self._smooth_scroll_target - current_scroll
        if abs(distance) < 0.002:
            canvas.yview_moveto(self._smooth_scroll_target)
            self._smooth_scroll_job = None
            return

        next_scroll = current_scroll + distance * 0.3
        canvas.yview_moveto(next_scroll)
        self._smooth_scroll_job = self.after(8, self._animate_scroll)

    def set_scrollbar_visible(self, visible: bool) -> None:
        if visible:
            self._scrollbar.grid()
        else:
            self._scrollbar.grid_remove()


class DpyleGUI(ctk.CTk, TkinterDnD.DnDWrapper):
    """Drop an executable and display its extracted folder tree."""

    def __init__(self) -> None:
        super().__init__()
        self.TkdndVersion = TkinterDnD._require(self)
        reset_work_folder()
        self.title("Dpyle")
        window_width = 1200
        window_height = 700
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        position_x = max(0, (screen_width - window_width) // 2)
        position_y = max(0, (screen_height - window_height) // 2)
        self.geometry(
            f"{window_width}x{window_height}+{position_x}+{position_y}"
        )
        self.minsize(800, 500)
        self.configure(fg_color=("#101010", "#0c0c0c"))

        self.drop_zone = ctk.CTkFrame(
            self,
            corner_radius=18,
            fg_color=("#101010", "#101010"),
        )
        self.drop_zone.pack(fill="both", expand=True, padx=24, pady=24)
        self.drop_target_register(DND_FILES)
        self.dnd_bind("<<Drop>>", self.on_drop)

        self.drop_zone_title = t("drop_title")
        self.drop_zone_subtitle = t("drop_subtitle")
        self.drop_zone_title_label = ctk.CTkLabel(
            self.drop_zone,
            text=self.drop_zone_title,
            font=ctk.CTkFont(size=24, weight="bold"),
            anchor="center",
        )
        self.drop_zone_title_label.pack(expand=True, anchor="center")
        self.drop_zone_subtitle_label = ctk.CTkLabel(
            self.drop_zone,
            text=self.drop_zone_subtitle,
            text_color="gray",
            anchor="center",
        )
        self.drop_zone_subtitle_label.pack(pady=(0, 40), anchor="center")
        self.language_label = ctk.CTkLabel(
            self.drop_zone,
            text=t("language_label"),
            text_color="gray",
        )
        self.language_label.pack(pady=(0, 4), anchor="center")
        self.language_selector = ctk.CTkOptionMenu(
            self.drop_zone,
            values=["Français", "English"],
            command=self.change_language,
            width=140,
        )
        self.language_selector.pack(pady=(0, 24), anchor="center")

        self.workspace = ctk.CTkFrame(
            self,
            fg_color=("#101010", "#0c0c0c"),
        )
        self.workspace.pack_forget()

        self.file_panel = ctk.CTkFrame(
            self.workspace,
            width=300,
            corner_radius=18,
            fg_color=("#141414", "#101010"),
        )
        self.file_panel.pack(side="left", fill="both")
        self.file_panel.pack_propagate(False)

        self.file_panel_title = t("file_list_title")
        self.file_list = SmoothScrollableFrame(
            self.file_panel,
            corner_radius=0,
            border_width=0,
            fg_color="transparent",
        )
        self.file_list.pack(fill="both", expand=True, padx=0, pady=0)
        self.file_list.set_scrollbar_visible(False)
        for widget in (
            self.file_panel,
            self.file_list,
            self.file_list._parent_frame,
            self.file_list._parent_canvas,
            self.file_list._scrollbar,
        ):
            widget.bind("<Enter>", self.show_file_list_scrollbar, add="+")
            widget.bind("<Leave>", self.schedule_file_list_scrollbar_hide, add="+")

        self.webhook_panel = ctk.CTkFrame(
            self.workspace,
            width=320,
            corner_radius=0,
            fg_color=("#181818", "#111111"),
        )
        self.webhook_panel.pack_forget()
        self.webhook_panel.pack_propagate(False)
        self.webhook_panel_title_label = ctk.CTkLabel(
            self.webhook_panel,
            text=t("webhook_panel_title"),
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w",
        )
        self.webhook_panel_title_label.pack(fill="x", padx=16, pady=(18, 12))
        self.webhook_details = ctk.CTkFrame(
            self.webhook_panel,
            fg_color="transparent",
        )
        self.webhook_details.pack(fill="both", expand=True, padx=8, pady=(0, 12))

        self.editor_panel = ctk.CTkFrame(
            self.workspace,
            corner_radius=0,
            fg_color="#151515",
            border_width=0,
        )
        self.editor_panel.pack(side="left", fill="both", expand=True)
        self.line_numbers = tk.Canvas(
            self.editor_panel,
            width=48,
            bg="#1b1b1b",
            highlightthickness=0,
            borderwidth=0,
        )
        self.line_numbers.pack(side="left", fill="y")
        self.editor_viewport = ctk.CTkFrame(
            self.editor_panel,
            corner_radius=0,
            fg_color="#151515",
            border_width=0,
        )
        self.editor_viewport.pack(side="left", fill="both", expand=True)
        self.editor_viewport.grid_rowconfigure(0, weight=1)
        self.editor_viewport.grid_columnconfigure(0, weight=1)
        self.editor = tk.Text(
            self.editor_viewport,
            wrap="none",
            undo=False,
            state="disabled",
            bg="#151515",
            fg="#d4d4d4",
            insertbackground="#d4d4d4",
            selectbackground="#264f78",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=("Consolas", 12),
            padx=8,
            pady=0,
        )
        self.editor.grid(row=0, column=0, sticky="nsew")
        self.editor_scrollbar = ctk.CTkScrollbar(
            self.editor_viewport,
            command=self.editor.yview,
            fg_color="#151515",
            button_color="#383838",
            button_hover_color="#4a4a4a",
            corner_radius=0,
        )
        self.editor_scrollbar.grid(row=0, column=1, sticky="ns")
        self.editor_horizontal_scrollbar = ctk.CTkScrollbar(
            self.editor_viewport,
            orientation="horizontal",
            command=self.editor.xview,
            fg_color="#151515",
            button_color="#383838",
            button_hover_color="#4a4a4a",
            corner_radius=0,
        )
        self.editor_horizontal_scrollbar.grid(row=1, column=0, sticky="ew")
        self._line_number_update_job: str | None = None
        self.editor.configure(
            yscrollcommand=self.update_editor_scroll,
            xscrollcommand=self.editor_horizontal_scrollbar.set,
        )
        self.editor.bind(
            "<Configure>",
            lambda _event: self.schedule_line_numbers_update(),
        )
        self.editor.bind(
            "<MouseWheel>",
            lambda _event: self.schedule_line_numbers_update(),
        )
        self.editor_context_menu = tk.Menu(self.editor, tearoff=False)
        self.editor_context_menu.add_command(
            label=t("copy"),
            command=self.copy_selected_text,
        )
        self.editor.bind("<Button-3>", self.show_editor_context_menu)
        self.editor.bind(
            "<Shift-MouseWheel>",
            lambda event: self.editor.xview_scroll(
                -1 if event.delta > 0 else 1,
                "units",
            ),
        )
        self.unavailable_panel = ctk.CTkFrame(self.editor_panel, fg_color="transparent")
        self.unavailable_logo = tk.Canvas(
            self.unavailable_panel,
            width=100,
            height=100,
            bg="#151515",
            highlightthickness=0,
        )
        self.unavailable_logo.create_polygon(
            84,
            17,
            81,
            18,
            43,
            77,
            19,
            56,
            16,
            54,
            13,
            55,
            12,
            58,
            13,
            61,
            41,
            86,
            44,
            87,
            47,
            85,
            88,
            24,
            89,
            21,
            88,
            18,
            fill="#39d353",
            outline="#39d353",
        )
        self.unavailable_title = ctk.CTkLabel(
            self.unavailable_panel,
            text=t("unavailable_default"),
            font=("Segoe UI", 22, "bold"),
            wraplength=1000,
        )
        self.unavailable_title.pack(pady=(140, 12), anchor="center")
        self.unavailable_logo.pack_forget()
        self.decompile_button = ctk.CTkButton(
            self.unavailable_panel,
            text=t("decompile"),
            command=self.decompile_current_file,
            width=180,
            height=38,
        )
        self.decompile_button.pack(anchor="center")
        self.decompilation_spinner = tk.Canvas(
            self.unavailable_panel,
            width=32,
            height=32,
            bg="#151515",
            highlightthickness=0,
        )
        self._spinner_job: str | None = None
        self._spinner_angle = 0
        self.decompilation_spinner.pack_forget()
        self.unavailable_reason = ctk.CTkFrame(
            self.unavailable_panel,
            fg_color="#007acc",
            corner_radius=18,
        )
        self.unavailable_reason_label = ctk.CTkLabel(
            self.unavailable_reason,
            text="",
            text_color="white",
            fg_color="transparent",
            font=ctk.CTkFont(size=13),
            wraplength=500,
            justify="left",
            padx=16,
            pady=16,
        )
        self.unavailable_reason_label.pack()
        self.unavailable_reason.pack(pady=12, padx=24, anchor="center")
        self.current_file: Path | None = None
        self.executable_name: str | None = None
        self.main_pyc_candidate: Path | None = None
        self._file_load_id = 0
        self._resource_extractions_in_progress: set[Path] = set()
        self._expanded_directories: set[str] = set()
        self._loading_job: str | None = None
        self._rendering_source = False
        self._decompilation_in_progress = False
        self._webhook_avatar_images: dict[str, ctk.CTkImage] = {}
        self.zip_tooltip: tk.Toplevel | None = None
        self._tooltip_animation_job: str | None = None
        self._scrollbar_hide_job: str | None = None

        self.status_bar = ctk.CTkFrame(
            self,
            height=24,
            corner_radius=0,
            fg_color=("#007acc", "#007acc"),
        )
        self.status_bar.pack(fill="x", side="bottom")
        self.status_bar.pack_propagate(False)
        self.status_bar.pack_forget()
        self.status = ctk.CTkLabel(
            self.status_bar,
            text=t("ready"),
            anchor="w",
            text_color="white",
        )
        self.decompilation_status = ctk.CTkLabel(
            self.status_bar,
            text="",
            anchor="w",
            text_color="white",
        )
        self.decompilation_status.pack(
            fill="x",
            side="left",
            padx=8,
            pady=0,
        )
        self.status.pack(
            fill="x",
            expand=True,
            side="left",
            padx=8,
            pady=0,
        )

    def change_language(self, language_name: str) -> None:
        global TRANSLATIONS
        language_code = "en" if language_name == "English" else "fr"
        TRANSLATIONS = TRANSLATION_CATALOG[language_code]
        self.drop_zone_title = t("drop_title")
        self.drop_zone_subtitle = t("drop_subtitle")
        self.drop_zone_title_label.configure(text=self.drop_zone_title)
        self.drop_zone_subtitle_label.configure(text=self.drop_zone_subtitle)
        self.language_label.configure(text=t("language_label"))
        self.webhook_panel_title_label.configure(text=t("webhook_panel_title"))
        self.unavailable_title.configure(text=t("unavailable_default"))
        self.decompile_button.configure(text=t("decompile"))
        self.status.configure(text=t("ready"))
        self.file_panel_title = t("file_list_title")

    def show_file_list_scrollbar(self, _event: tk.Event) -> None:
        if self._scrollbar_hide_job is not None:
            self.after_cancel(self._scrollbar_hide_job)
            self._scrollbar_hide_job = None
        self.file_list.set_scrollbar_visible(True)

    def schedule_file_list_scrollbar_hide(self, _event: tk.Event) -> None:
        if self._scrollbar_hide_job is not None:
            self.after_cancel(self._scrollbar_hide_job)
        self._scrollbar_hide_job = self.after(100, self.hide_file_list_scrollbar)

    def hide_file_list_scrollbar(self) -> None:
        self._scrollbar_hide_job = None
        widget = self.winfo_containing(self.winfo_pointerx(), self.winfo_pointery())
        if widget is not None:
            current = widget
            while current is not None:
                if current in {
                    self.file_panel,
                    self.file_list,
                    self.file_list._parent_frame,
                    self.file_list._parent_canvas,
                    self.file_list._scrollbar,
                }:
                    return
                current = getattr(current, "master", None)
        self.file_list.set_scrollbar_visible(False)

    def on_drop(self, event) -> None:
        paths = self.tk.splitlist(event.data)
        if len(paths) != 1:
            self.show_error(t("drop_single"))
            return

        executable = Path(paths[0])
        if executable.suffix.lower() != ".exe" or not executable.is_file():
            self.show_error(t("drop_invalid"))
            return

        self.executable_name = executable.stem.lower()
        self._expanded_directories.clear()
        self.status.configure(text=t("extracting", name=executable.name))
        def worker() -> None:
            try:
                reset_work_folder()
                launch_extractor(str(executable))
            except Exception as error:
                self.after(0, lambda: self.show_error(str(error)))
            else:
                self.after(0, self.show_tree)

        threading.Thread(target=worker, daemon=True).start()

    def show_tree(self) -> None:
        self.drop_zone.pack_forget()
        self.populate_tree()
        self.workspace.pack(fill="both", expand=True, padx=0, pady=0)
        self.status_bar.pack_forget()
        self.find_main_pyc()

    def find_main_pyc(self) -> None:
        self.status.configure(text=t("searching_main"))

        def worker() -> None:
            candidates = list(WORK_FOLDER.rglob("*.pyc"))
            candidate = max(candidates, key=self.main_pyc_score, default=None)
            self.after(0, lambda: self.show_main_pyc_candidate(candidate))

        threading.Thread(target=worker, daemon=True).start()

    def main_pyc_score(self, file_path: Path) -> tuple[int, int, int]:
        name = file_path.stem.lower()
        excluded_names = (
            "pyimod",
            "pyiboot",
            "pyi_rth",
            "python",
            "struct",
            "pyarmor",
        )
        score = 0
        if any(name.startswith(prefix) for prefix in excluded_names):
            score -= 1000
        if name in {"main", "__main__", "__main__"}:
            score += 500
        if name in {"app", "run", "start", "cli", "launcher"}:
            score += 250
        if self.executable_name and name == self.executable_name:
            score += 450
        score -= len(file_path.relative_to(WORK_FOLDER).parts) * 20
        try:
            size = file_path.stat().st_size
        except OSError:
            size = 0
        return score, size, -len(file_path.name)

    def show_main_pyc_candidate(self, candidate: Path | None) -> None:
        if candidate is None:
            self.status.configure(text=t("no_python_module"))
            return
        self.main_pyc_candidate = candidate
        self.populate_tree()
        self.status.configure(text=candidate.name)
        self.open_file(candidate)
        self.after_idle(self.decompile_current_file)

    def populate_tree(self, expanded_directory: str | None = None) -> None:
        for widget in self.file_list.winfo_children():
            widget.destroy()
        if not WORK_FOLDER.exists():
            return
        if expanded_directory is not None:
            self._expanded_directories.add(expanded_directory)
        self._expanded_directories = {
            relative_path
            for relative_path in self._expanded_directories
            if (WORK_FOLDER / Path(relative_path)).is_dir()
        }

        # Keep the breathing room inside the scrollable content area.
        ctk.CTkFrame(
            self.file_list,
            height=10,
            fg_color="transparent",
        ).pack(fill="x")
        self.add_directory(
            self.file_list,
            WORK_FOLDER,
            0,
            hidden_files(),
            resource_folders(),
        )
        ctk.CTkFrame(
            self.file_list,
            height=10,
            fg_color="transparent",
        ).pack(fill="x")

    def add_directory(
        self,
        parent,
        directory: Path,
        depth: int,
        hidden: set[str],
        resource_directories: dict[str, str],
    ) -> None:
        entries = [
            entry
            for entry in sorted(
                directory.iterdir(),
                key=lambda item: file_tree_sort_key(
                    item,
                    resource_directories,
                ),
            )
            if entry.relative_to(WORK_FOLDER).as_posix() not in hidden
        ]
        if not entries:
            ctk.CTkLabel(
                parent,
                text=t("empty_folder"),
                anchor="w",
                text_color="gray",
                font=ctk.CTkFont(size=11),
            ).pack(fill="x", padx=(18, 8), pady=2)
            return

        for entry in entries:
            if entry.is_dir():
                relative_path = entry.relative_to(WORK_FOLDER).as_posix()
                source_file = resource_directories.get(relative_path)
                if source_file:
                    folder_icon, icon_color = self.file_icon(Path(source_file))
                    folder_name = Path(source_file).name
                else:
                    folder_icon, icon_color = "📁", "#f2c94c"
                    folder_name = entry.name
                row = ctk.CTkFrame(parent, fg_color="transparent")
                if depth:
                    ctk.CTkLabel(row, text="", width=16).pack(side="left")
                icon_label = ctk.CTkLabel(
                    row,
                    text=folder_icon,
                    width=38,
                    anchor="e",
                    font=("Segoe UI Emoji", 16),
                    text_color=icon_color,
                )
                icon_label.pack(side="left", padx=(0, 4))
                folder_button = ctk.CTkButton(
                    row,
                    text=folder_name,
                    anchor="w",
                    fg_color="transparent",
                    hover_color=("#141414", "#101010"),
                    text_color=("gray10", "gray90"),
                )
                folder_button.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=1)
                row.pack(fill="x", pady=1)
                self.bind_button_hover(folder_button)
                branch_frame = ctk.CTkFrame(parent, fg_color="transparent")
                ctk.CTkLabel(branch_frame, text="", width=16).pack(side="left")
                children_frame = ctk.CTkFrame(branch_frame, fg_color="transparent")
                children_frame.pack(side="left", fill="x", expand=True)
                self.add_directory(
                    children_frame,
                    entry,
                    depth + 1,
                    hidden,
                    resource_directories,
                )
                if relative_path in self._expanded_directories:
                    branch_frame.pack(fill="x", after=row)
                    if not source_file:
                        icon_label.configure(text="📂")
                else:
                    branch_frame.pack_forget()
                folder_button.configure(
                    command=lambda button=folder_button, icon=icon_label,
                    row_frame=row, frame=branch_frame,
                    virtual_icon=folder_icon if source_file else None,
                    folder_path=relative_path:
                    self.toggle_directory(
                        button,
                        icon,
                        row_frame,
                        frame,
                        virtual_icon,
                        folder_path,
                    )
                )
                continue

            icon, icon_color = self.file_icon(entry)
            row = ctk.CTkFrame(parent, fg_color="transparent")
            if depth:
                ctk.CTkLabel(row, text="", width=16).pack(side="left")
            ctk.CTkLabel(
                row,
                text=icon,
                width=38,
                anchor="e",
                font=("Segoe UI Emoji", 16),
                text_color=icon_color,
            ).pack(side="left", padx=(0, 4))
            file_button = ctk.CTkButton(
                row,
                text=entry.name,
                anchor="w",
                fg_color="transparent",
                hover_color=("#141414", "#101010"),
                text_color=(
                    "#39d353",
                    "#39d353",
                ) if self.main_pyc_candidate and entry.resolve() == self.main_pyc_candidate
                else ("gray10", "gray90"),
                command=lambda file_path=entry: self.open_file(file_path),
            )
            file_button.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=1)
            row.pack(fill="x", pady=1)
            self.bind_button_hover(file_button)
            if self.main_pyc_candidate and entry.resolve() == self.main_pyc_candidate:
                file_button.bind(
                    "<Enter>",
                    lambda _event, file_row=row: self.show_main_pyc_tooltip(file_row),
                    add="+",
                )
                file_button.bind(
                    "<Leave>",
                    lambda _event: self.hide_zip_tooltip(),
                    add="+",
                )
            if entry.suffix.lower() == ".zip":
                file_button.bind(
                    "<Enter>",
                    lambda _event, file_row=row:
                    self.show_external_app_tooltip(
                        file_row,
                        t("associated_app"),
                    ),
                    add="+",
                )
                file_button.bind(
                    "<Leave>",
                    lambda _event: self.hide_zip_tooltip(),
                    add="+",
                )

    def toggle_directory(
        self,
        folder_button: ctk.CTkButton,
        icon_label: ctk.CTkLabel,
        row: ctk.CTkFrame,
        children_frame: ctk.CTkFrame,
        virtual_icon: str | None = None,
        folder_path: str | None = None,
    ) -> None:
        if children_frame.winfo_manager():
            children_frame.pack_forget()
            icon_label.configure(text=virtual_icon or "📁")
            if folder_path is not None:
                self._expanded_directories.discard(folder_path)
        else:
            children_frame.pack(fill="x", after=row)
            icon_label.configure(text=virtual_icon or "📂")
            if folder_path is not None:
                self._expanded_directories.add(folder_path)

    def bind_button_hover(self, button: ctk.CTkButton) -> None:
        button._hover_animation_job = None
        button._hover_animation_progress = 0.0
        button.bind(
            "<Enter>",
            lambda _event: self.animate_button_hover(button, True),
            add="+",
        )
        button.bind(
            "<Leave>",
            lambda _event: self.animate_button_hover(button, False),
            add="+",
        )

    def animate_button_hover(self, button: ctk.CTkButton, entering: bool) -> None:
        if button._hover_animation_job is not None:
            self.after_cancel(button._hover_animation_job)
        button._hover_animation_entering = entering
        self.run_button_hover_animation(button)

    def run_button_hover_animation(self, button: ctk.CTkButton) -> None:
        try:
            if not button.winfo_exists():
                button._hover_animation_job = None
                return
        except tk.TclError:
            button._hover_animation_job = None
            return

        change = 0.125 if button._hover_animation_entering else -0.125
        button._hover_animation_progress = min(
            1.0,
            max(0.0, button._hover_animation_progress + change),
        )
        progress = button._hover_animation_progress
        light_value = round(255 - (255 - 204) * progress)
        dark_value = round(20 + (64 - 20) * progress)
        button.configure(
            fg_color=(
                f"#{light_value:02x}{light_value:02x}{light_value:02x}",
                f"#{dark_value:02x}{dark_value:02x}{dark_value:02x}",
            )
        )
        if 0.0 < progress < 1.0:
            button._hover_animation_job = self.after(
                16,
                lambda: self.run_button_hover_animation(button),
            )
        else:
            button._hover_animation_job = None
            if not button._hover_animation_entering:
                try:
                    if button.winfo_exists():
                        button.configure(fg_color="transparent")
                except tk.TclError:
                    return

    @staticmethod
    def file_icon(file_path: Path) -> tuple[str, str]:
        icons = {
            ".txt": ("📄", "#d4d4d4"),
            ".pyc": ("⚙️", "#f2c94c"),
            ".zip": ("💼", "#f2994a"),
            ".ico": ("🖼️", "#56ccf2"),
            ".pyd": ("🧩", "#bb86fc"),
            ".pyz": ("📦", "#56ccf2"),
            ".dll": ("🪟", "#6fcf97"),
            ".py": ("🐍", "#776fcf"),
        }
        return icons.get(file_path.suffix.lower(), ("📄", "#d4d4d4"))

    def show_external_app_tooltip(self, row: ctk.CTkFrame, app_name: str) -> None:
        self.hide_zip_tooltip()
        if app_name == t("associated_app"):
            app_name = self.zip_associated_app_name()
        tooltip = self.create_file_tooltip(
            text=t("open_with", app=app_name),
            bg="#252526",
            fg="#d4d4d4",
        )
        self.position_file_tooltip(tooltip, row)
        self.zip_tooltip = tooltip
        self.animate_file_tooltip(tooltip)

    def create_file_tooltip(
        self,
        text: str,
        bg: str,
        fg: str,
        wraplength: int = 300,
    ) -> tk.Toplevel:
        tooltip = tk.Toplevel(self)
        tooltip.overrideredirect(True)
        tooltip.attributes("-topmost", True)
        tooltip.configure(bg=bg)
        tooltip_frame = ctk.CTkFrame(
            tooltip,
            corner_radius=0,
            border_width=1,
            border_color="#3a3a3a" if bg == "#252526" else "#168bd0",
            fg_color=bg,
        )
        tooltip_frame.pack()
        ctk.CTkLabel(
            tooltip_frame,
            text=text,
            text_color=fg,
            font=ctk.CTkFont(size=13),
            wraplength=wraplength,
            justify="left",
        ).pack(padx=10, pady=5)
        tooltip.update_idletasks()
        return tooltip

    def position_file_tooltip(
        self,
        tooltip: tk.Toplevel,
        row: ctk.CTkFrame,
        outer_margin: int = 12,
    ) -> None:
        file_list = self.file_list._parent_frame
        list_left = file_list.winfo_rootx()
        list_right = list_left + file_list.winfo_width()
        center_x = list_left + file_list.winfo_width() // 2
        max_width = max(1, file_list.winfo_width() - (outer_margin * 2))
        tooltip_width = min(tooltip.winfo_width(), max_width)
        x = center_x - tooltip_width // 2
        x = max(list_left + outer_margin, min(x, list_right - outer_margin - tooltip_width))
        y = row.winfo_rooty() + row.winfo_height() + 3
        tooltip.geometry(f"{tooltip_width}x{tooltip.winfo_height()}+{x}+{y}")

    def show_main_pyc_tooltip(self, row: ctk.CTkFrame) -> None:
        self.hide_zip_tooltip()
        tooltip_width = max(100, self.file_list.winfo_width() - 48)
        tooltip = self.create_file_tooltip(
            text=t("main_candidate_tooltip"),
            bg="#007acc",
            fg="white",
            wraplength=tooltip_width,
        )
        self.position_file_tooltip(tooltip, row, outer_margin=24)
        self.zip_tooltip = tooltip
        self.animate_file_tooltip(tooltip)

    def animate_file_tooltip(self, tooltip: tk.Toplevel) -> None:
        try:
            tooltip.attributes("-alpha", 0.0)
        except tk.TclError:
            return
        self._tooltip_animation_step = 0
        self._tooltip_animation_job = self.after(
            16,
            lambda: self.run_file_tooltip_animation(tooltip),
        )

    def run_file_tooltip_animation(self, tooltip: tk.Toplevel) -> None:
        if tooltip is not self.zip_tooltip or not tooltip.winfo_exists():
            self._tooltip_animation_job = None
            return
        self._tooltip_animation_step += 1
        alpha = min(1.0, self._tooltip_animation_step / 8)
        try:
            tooltip.attributes("-alpha", alpha)
        except tk.TclError:
            self._tooltip_animation_job = None
            return
        if alpha < 1.0:
            self._tooltip_animation_job = self.after(
                16,
                lambda: self.run_file_tooltip_animation(tooltip),
            )
        else:
            self._tooltip_animation_job = None

    def hide_zip_tooltip(self) -> None:
        if self.zip_tooltip is not None:
            if self._tooltip_animation_job is not None:
                self.after_cancel(self._tooltip_animation_job)
                self._tooltip_animation_job = None
            self.zip_tooltip.destroy()
            self.zip_tooltip = None

    @staticmethod
    def zip_associated_app_name() -> str:
        def appx_package_name(prog_id: str) -> str | None:
            if not prog_id.lower().startswith("appx"):
                return None
            package_root = (
                r"Software\Classes\Local Settings\Software\Microsoft"
                r"\Windows\CurrentVersion\AppModel\PackageRepository"
                rf"\Extensions\ProgIDs\{prog_id}"
            )
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, package_root) as key:
                    package_name = winreg.EnumValue(key, 0)[0]
            except (FileNotFoundError, OSError, IndexError):
                return None
            package_name = package_name.split("_", 1)[0]
            return package_name.rsplit(".", 1)[-1] or None

        try:
            prog_id = DpyleGUI.registry_default_value(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.zip\UserChoice",
                "ProgId",
            )
            if not prog_id:
                prog_id = DpyleGUI.registry_default_value(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Classes\.zip",
                )
            if not prog_id:
                prog_id = DpyleGUI.registry_default_value(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"Software\Classes\.zip",
                )
            if prog_id:
                app_name = appx_package_name(prog_id)
                if app_name:
                    return app_name
                command = (
                    DpyleGUI.registry_default_value(
                        winreg.HKEY_CURRENT_USER,
                        fr"Software\Classes\{prog_id}\shell\open\command",
                    )
                    or DpyleGUI.registry_default_value(
                        winreg.HKEY_LOCAL_MACHINE,
                        fr"Software\Classes\{prog_id}\shell\open\command",
                    )
                )
                if command:
                    match = re.match(r'\s*"([^"]+)"|(\S+)', command)
                    executable = match.group(1) or match.group(2) if match else ""
                    if executable and Path(executable).stem.lower() not in {"explorer", "rundll32"}:
                        return Path(executable).stem.replace("_", " ").title()

                # The class default value is often the file type description
                # (for example "Compressed Folder"), not the associated app.
                display_name = (
                    DpyleGUI.registry_default_value(
                        winreg.HKEY_CURRENT_USER,
                        fr"Software\Classes\{prog_id}",
                    )
                    or DpyleGUI.registry_default_value(
                        winreg.HKEY_LOCAL_MACHINE,
                        fr"Software\Classes\{prog_id}",
                    )
                )
                display_name_lower = display_name.lower() if display_name else ""
                generic_zip_type = (
                    "compressed" in display_name_lower
                    and "folder" in display_name_lower
                ) or display_name_lower in {
                    "compressed folder",
                    "zip archive",
                    "dossier compressé",
                    "archive zip",
                }
                if display_name and not generic_zip_type:
                    return display_name

                prog_name = re.sub(r"\.(zip|archive)$", "", prog_id, flags=re.I)
                if prog_name and prog_name.lower() != "compressedfolder":
                    return prog_name.replace(".", " ").replace("_", " ").title()

            open_with_key = (
                r"Software\Classes\.zip\OpenWithProgids"
            )
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, open_with_key) as key:
                for index in range(winreg.QueryInfoKey(key)[1]):
                    open_with_prog_id, _, _ = winreg.EnumValue(key, index)
                    app_name = appx_package_name(open_with_prog_id)
                    if app_name:
                        return app_name
        except OSError:
            pass
        return t("default_app")

    @staticmethod
    def registry_default_value(
        root: int,
        sub_key: str,
        value_name: str | None = None,
    ) -> str | None:
        try:
            with winreg.OpenKey(root, sub_key) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
                return str(value).strip() or None
        except (FileNotFoundError, OSError):
            return None

    def open_file(self, file_path: Path) -> None:
        resource_hacker = Path(__file__).resolve().parent / "ResourceHacker.exe"

        if file_path.suffix.lower() in {".dll", ".pyd"}:
            resolved_path = file_path.resolve()
            if resolved_path in self._resource_extractions_in_progress:
                return

        self._file_load_id += 1
        load_id = self._file_load_id
        self.status.configure(text=file_path.name)
        self.show_webhook_panel([], load_id)
        try:
            relative_path = file_path.relative_to(WORK_FOLDER).as_posix()
        except ValueError:
            relative_path = ""
        resource_source = (
            resource_file_for_member(relative_path)
            if relative_path and file_path.suffix.lower() != ".txt"
            else None
        )
        if resource_source:
            self.stop_loading()
            self.status.configure(text="")
            try:
                if not resource_hacker.is_file():
                    raise FileNotFoundError(
                        f"{resource_hacker.name} est introuvable."
                    )
                subprocess.Popen(
                    [
                        str(resource_hacker),
                        str(WORK_FOLDER / Path(resource_source)),
                    ],
                    cwd=str(resource_hacker.parent),
                )
            except OSError as error:
                self.show_error(
                    t("cannot_open", name=file_path.name, error=error)
                )
                return
            self.current_file = WORK_FOLDER / Path(resource_source)
            return
        if file_path.suffix.lower() in {".dll", ".pyd"}:
            self.stop_loading()
            self.status.configure(
                text=t("resource_extracting", name=file_path.name)
            )
            self._resource_extractions_in_progress.add(file_path.resolve())
            self.current_file = file_path

            def worker() -> None:
                try:
                    extract_resources(file_path)
                except (
                    OSError,
                    subprocess.SubprocessError,
                    ET.ParseError,
                    UnicodeError,
                    ValueError,
                ) as error:
                    self.after(
                        0,
                        lambda error=error: self.finish_resource_extraction(
                            load_id,
                            file_path,
                            error,
                        ),
                    )
                    return
                self.after(
                    0,
                    lambda: self.finish_resource_extraction(
                        load_id,
                        file_path,
                    ),
                )

            threading.Thread(target=worker, daemon=True).start()
            return
        if file_path.suffix.lower() == ".zip":
            self.stop_loading()
            self.status.configure(text="")
            try:
                os.startfile(str(file_path))
            except OSError as error:
                self.show_error(t("cannot_open", name=file_path.name, error=error))
                return
            self.current_file = file_path
            return
        if file_path.suffix.lower() == ".pyc":
            mapped_file = decompiled_file_for(
                file_path.relative_to(WORK_FOLDER).as_posix()
            )
            if mapped_file:
                self.open_file(WORK_FOLDER / Path(mapped_file))
                return
            self.stop_loading()
            self.current_file = file_path
            self.show_unavailable(file_path)
            return
        self.start_loading(file_path.name)

        def worker() -> None:
            try:
                content = file_path.read_text(encoding="utf-8")
                webhooks = find_discord_webhooks(content)
                lexer = self.get_file_lexer(file_path, content)
                highlighted_tokens = [
                    (f"token_{str(token)}", value, self.token_color(token))
                    for token, value in lex(content, lexer)
                ]
            except (UnicodeDecodeError, OSError) as error:
                self.after(
                    0,
                    lambda error=error: self.finish_file_load_error(
                        load_id, file_path, error
                    ),
                )
                return
            except Exception as error:
                self.after(
                    0,
                    lambda error=error: self.finish_file_load_error(
                        load_id, file_path, error
                    ),
                )
                return

            self.after(
                0,
                lambda: self.finish_file_load(
                    load_id,
                    file_path,
                    content,
                    highlighted_tokens,
                    webhooks,
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def finish_resource_extraction(
        self,
        load_id: int,
        file_path: Path,
        error: Exception | None = None,
    ) -> None:
        self._resource_extractions_in_progress.discard(file_path.resolve())
        if load_id != self._file_load_id:
            return
        self.stop_loading()
        if error is not None:
            self.show_error(
                t(
                    "resource_extraction_failed",
                    name=file_path.name,
                    error=error,
                )
            )
            return
        self.status.configure(text="")
        extracted_directory = (
            file_path.parent / f"EXTRACTED_{file_path.stem}"
        ).relative_to(WORK_FOLDER).as_posix()
        self.populate_tree(expanded_directory=extracted_directory)

    def start_loading(self, file_name: str) -> None:
        self.stop_loading()
        self._loading_file_name = file_name
        self.status.configure(text=self._loading_file_name)

    def stop_loading(self) -> None:
        if self._loading_job is not None:
            self.after_cancel(self._loading_job)
            self._loading_job = None

    def finish_file_load_error(
        self,
        load_id: int,
        file_path: Path,
        error: Exception,
    ) -> None:
        if load_id != self._file_load_id:
            return
        self.stop_loading()
        if isinstance(error, UnicodeDecodeError):
            self.current_file = file_path
            self.show_unavailable(file_path)
            return
        self.show_error(t("cannot_read", name=file_path.name, error=error))

    def finish_file_load(
        self,
        load_id: int,
        file_path: Path,
        content: str,
        highlighted_tokens: list[tuple[str, str, str]],
        webhooks: list[DiscordWebhook],
    ) -> None:
        if load_id != self._file_load_id:
            return
        self.current_file = file_path
        self.hide_unavailable()
        self.show_webhook_panel(webhooks, load_id)
        self.start_source_render(
            load_id,
            content,
            file_path,
            highlighted_tokens,
        )

    def show_webhook_panel(
        self,
        webhooks: list[DiscordWebhook],
        load_id: int,
    ) -> None:
        for child in self.webhook_details.winfo_children():
            child.destroy()
        if not webhooks:
            if self.webhook_panel.winfo_manager():
                self.editor_panel.pack_forget()
                self.webhook_panel.pack_forget()
                self.editor_panel.pack(
                    side="left",
                    fill="both",
                    expand=True,
                )
            return

        if not self.webhook_panel.winfo_manager():
            self.editor_panel.pack_forget()
        self.webhook_panel.pack(side="right", fill="y")
        self.editor_panel.pack(side="left", fill="both", expand=True)
        for webhook in webhooks:
            card = ctk.CTkFrame(
                self.webhook_details,
                corner_radius=12,
                fg_color=("#e8e8e8", "#181818"),
            )
            card.pack(fill="x", padx=4, pady=6)
            avatar = ctk.CTkLabel(
                card,
                text="?",
                width=56,
                height=56,
                corner_radius=28,
                padx=0,
                pady=0,
                fg_color=("#5865f2", "#5865f2"),
                text_color="white",
                font=ctk.CTkFont(size=24, weight="bold"),
            )
            avatar.pack(pady=(14, 8))
            name = ctk.CTkLabel(
                card,
                text=t("webhook_name_unavailable"),
                font=ctk.CTkFont(size=14, weight="bold"),
                wraplength=270,
            )
            name.pack(fill="x", padx=12, pady=2)
            ctk.CTkLabel(
                card,
                text=t("webhook_id", identifier=webhook.webhook_id),
                anchor="w",
            ).pack(fill="x", padx=12, pady=3)
            ctk.CTkLabel(
                card,
                text=t(
                    "webhook_lines",
                    lines=", ".join(map(str, webhook.lines)),
                ),
                anchor="w",
            ).pack(fill="x", padx=12, pady=(3, 12))
            status = ctk.CTkLabel(
                card,
                text=t("webhook_checking"),
                anchor="w",
                wraplength=270,
                justify="left",
            )
            status.pack(fill="x", padx=12, pady=(0, 12))
            message_box = ctk.CTkEntry(
                card,
                width=260,
                placeholder_text=t("webhook_message_placeholder"),
            )
            message_box.pack(fill="x", padx=12, pady=(0, 8))
            message_box.configure(state="disabled")
            send_button = ctk.CTkButton(
                card,
                text=t("webhook_send"),
                state="disabled",
            )
            send_button.pack(fill="x", padx=12, pady=(0, 12))
            send_button.bind("<Return>", lambda _event: "break", add="+")
            send_button.bind("<KP_Enter>", lambda _event: "break", add="+")
            send_button.configure(
                command=lambda item=webhook, textbox=message_box, button=send_button, result_label=status:
                self.send_webhook_message(
                    item,
                    textbox,
                    button,
                    result_label,
                    load_id,
                )
            )
            ctk.CTkButton(
                card,
                text=t("webhook_copy_url"),
                command=lambda item=webhook, result_label=status:
                self.copy_webhook_url(item, result_label),
            ).pack(fill="x", padx=12, pady=(0, 12))
            message_box.bind(
                "<Return>",
                lambda _event, item=webhook, entry=message_box,
                button=send_button, result_label=status:
                self.submit_webhook_message(
                    item,
                    entry,
                    button,
                    result_label,
                    load_id,
                ),
            )
            message_box.bind(
                "<KP_Enter>",
                lambda _event, item=webhook, entry=message_box,
                button=send_button, result_label=status:
                self.submit_webhook_message(
                    item,
                    entry,
                    button,
                    result_label,
                    load_id,
                ),
            )
            threading.Thread(
                target=self.check_webhook_status,
                args=(
                    webhook,
                    avatar,
                    name,
                    status,
                    message_box,
                    send_button,
                    load_id,
                ),
                daemon=True,
            ).start()

    def copy_webhook_url(
        self,
        webhook: DiscordWebhook,
        status_label: ctk.CTkLabel,
    ) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(webhook.url)
        except tk.TclError as error:
            self.show_error(
                t("generic_error", message=t("webhook_copy_failed", error=error))
            )
            return
        status_label.configure(
            text=t("webhook_url_copied"),
            text_color="#39d353",
        )

    def check_webhook_status(
        self,
        webhook: DiscordWebhook,
        avatar_label: ctk.CTkLabel,
        name_label: ctk.CTkLabel,
        status_label: ctk.CTkLabel,
        message_box: ctk.CTkEntry,
        send_button: ctk.CTkButton,
        load_id: int,
    ) -> None:
        try:
            result = check_discord_webhook(webhook)
        except requests.RequestException:
            result = DiscordWebhookCheck("network_error")

        if result.status == "exists":
            message = t("webhook_ready")
        elif result.status == "missing":
            message = t("webhook_missing")
        elif result.status == "network_error":
            message = t("webhook_network_error")
        elif result.status == "invalid_response":
            message = t("webhook_check_failed", status="invalide")
        else:
            message = t(
                "webhook_check_failed",
                status=result.status.removeprefix("http_"),
            )

        self.after(
            0,
            lambda: self.update_webhook_status(
                avatar_label,
                name_label,
                status_label,
                message_box,
                send_button,
                load_id,
                message,
                result,
                webhook.webhook_id,
            ),
        )

    def update_webhook_status(
        self,
        avatar_label: ctk.CTkLabel,
        name_label: ctk.CTkLabel,
        status_label: ctk.CTkLabel,
        message_box: ctk.CTkEntry,
        send_button: ctk.CTkButton,
        load_id: int,
        message: str,
        result: DiscordWebhookCheck,
        webhook_id: str,
    ) -> None:
        if load_id != self._file_load_id:
            return
        try:
            if not status_label.winfo_exists():
                return
            if result.status == "missing":
                avatar_label.configure(
                    text="×",
                    fg_color="#d93636",
                )
                name_label.configure(
                    text=t("webhook_missing_title"),
                    text_color="#ff5c5c",
                )
                status_label.configure(
                    text=message,
                    text_color="#ff5c5c",
                )
            elif result.status == "exists":
                if result.avatar_data:
                    try:
                        avatar_image = Image.open(
                            BytesIO(result.avatar_data)
                        ).convert("RGBA")
                        avatar_image = ImageOps.fit(
                            avatar_image,
                            (56, 56),
                            method=Image.Resampling.LANCZOS,
                        )
                        mask_scale = 4
                        avatar_mask = Image.new(
                            "L",
                            (56 * mask_scale, 56 * mask_scale),
                            0,
                        )
                        ImageDraw.Draw(avatar_mask).ellipse(
                            (0, 0, 56 * mask_scale - 1, 56 * mask_scale - 1),
                            fill=255,
                        )
                        avatar_mask = avatar_mask.resize(
                            (56, 56),
                            Image.Resampling.LANCZOS,
                        )
                        avatar_image.putalpha(avatar_mask)
                    except OSError:
                        avatar_label.configure(
                            image=None,
                            text=(result.name[:1].upper() if result.name else "?"),
                            fg_color="#5865f2",
                        )
                    else:
                        image = ctk.CTkImage(
                            light_image=avatar_image,
                            dark_image=avatar_image,
                            size=(56, 56),
                        )
                        self._webhook_avatar_images[webhook_id] = image
                        avatar_label.configure(
                            image=image,
                            text="",
                            fg_color="transparent",
                        )
                else:
                    avatar_label.configure(
                        image=None,
                        text=(result.name[:1].upper() if result.name else "?"),
                        fg_color="#5865f2",
                    )
                name_label.configure(
                    text=result.name or t("webhook_name_unavailable"),
                    text_color=("gray10", "gray90"),
                )
                status_label.configure(
                    text=message,
                    text_color="#39d353",
                )
                message_box.configure(state="normal")
                send_button.configure(state="normal")
            else:
                status_label.configure(text=message)
        except tk.TclError:
            return

    def send_webhook_message(
        self,
        webhook: DiscordWebhook,
        message_box: ctk.CTkEntry,
        send_button: ctk.CTkButton,
        status_label: ctk.CTkLabel,
        load_id: int,
    ) -> None:
        message = message_box.get().strip()
        if not message:
            status_label.configure(text=t("webhook_message_empty"))
            return
        if len(message) > 2000:
            status_label.configure(text=t("webhook_message_too_long"))
            return

        send_button.configure(state="disabled")
        message_box.configure(state="disabled")
        status_label.configure(text=t("webhook_sending"))

        def worker() -> None:
            try:
                result = send_discord_webhook_message(webhook, message)
            except requests.RequestException:
                result = "network_error"
            self.after(
                0,
                lambda: self.finish_webhook_send(
                    result,
                    message_box,
                    send_button,
                    status_label,
                    load_id,
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def submit_webhook_message(
        self,
        webhook: DiscordWebhook,
        message_box: ctk.CTkEntry,
        send_button: ctk.CTkButton,
        status_label: ctk.CTkLabel,
        load_id: int,
    ) -> str:
        self.send_webhook_message(
            webhook,
            message_box,
            send_button,
            status_label,
            load_id,
        )
        return "break"

    def finish_webhook_send(
        self,
        result: str,
        message_box: ctk.CTkEntry,
        send_button: ctk.CTkButton,
        status_label: ctk.CTkLabel,
        load_id: int,
    ) -> None:
        if load_id != self._file_load_id:
            return
        if result == "sent":
            message_box.configure(state="normal")
            status_label.configure(
                text=t("webhook_message_sent"),
                text_color="#39d353",
            )
            send_button.configure(state="normal")
        elif result == "missing":
            message_box.configure(state="disabled")
            send_button.configure(state="disabled")
            status_label.configure(
                text=t("webhook_missing"),
                text_color="#ff5c5c",
            )
        elif result == "network_error":
            message_box.configure(state="normal")
            status_label.configure(
                text=t("webhook_network_error"),
                text_color="#ffb347",
            )
            send_button.configure(state="normal")
        else:
            message_box.configure(state="normal")
            status_label.configure(
                text=t(
                    "webhook_check_failed",
                    status=result.removeprefix("http_"),
                ),
                text_color="#ff5c5c",
            )
            send_button.configure(state="normal")

    @staticmethod
    def get_file_lexer(file_path: Path, content: str):
        try:
            return get_lexer_for_filename(str(file_path), content=content)
        except ClassNotFound:
            return TextLexer()

    def show_unavailable(self, file_path: Path) -> None:
        if not self._decompilation_in_progress:
            self.status_bar.pack_forget()
        self.editor_viewport.pack_forget()
        self.line_numbers.pack_forget()
        self.unavailable_panel.pack(fill="both", expand=True)
        is_pyc = file_path.suffix.lower() == ".pyc"
        is_main_candidate = (
            is_pyc
            and self.main_pyc_candidate is not None
            and file_path.resolve() == self.main_pyc_candidate.resolve()
        )
        self.unavailable_logo.pack_forget()
        self.unavailable_title.pack_forget()
        self.decompile_button.pack_forget()
        self.unavailable_reason.pack_forget()
        if is_main_candidate:
            self.unavailable_logo.pack(pady=(90, 8), anchor="center")
        self.unavailable_title.configure(
            text=(
                t("main_eligible", name=file_path.name)
                if is_pyc
                else t("not_eligible", name=file_path.name)
            )
        )
        self.unavailable_title.configure(
            font=("Segoe UI", 22, "bold")
        )
        if is_pyc:
            self.decompile_button.configure(state="normal")
        self.unavailable_reason.configure(
            fg_color="#007acc" if is_main_candidate else "transparent"
        )
        self.unavailable_reason_label.configure(
            text_color="white" if is_main_candidate else "#a0a0a0"
        )
        self.unavailable_reason_label.configure(
            text=(
                (
                    t("other_reason")
                )
                if is_pyc
                else t("unsupported")
            )
        )
        self.unavailable_title.pack(
            pady=(12 if is_main_candidate else 140, 12),
            anchor="center",
        )
        if is_main_candidate:
            self.unavailable_reason.pack_forget()
        else:
            self.unavailable_reason.pack(pady=12, anchor="center")
        if is_pyc:
            self.decompile_button.pack(anchor="center")
        self.status.configure(text="")

    def hide_unavailable(self) -> None:
        self.unavailable_panel.pack_forget()
        self.status_bar.pack(fill="x", side="bottom")
        self.line_numbers.pack(side="left", fill="y")
        self.editor_viewport.pack(side="left", fill="both", expand=True)

    def decompile_current_file(self) -> None:
        if self.current_file is None or self.current_file.suffix.lower() != ".pyc":
            return
        pyc_file = self.current_file
        os.system("cls")
        self._decompilation_in_progress = True
        self.status_bar.pack(fill="x", side="bottom")
        self.decompilation_status.configure(
            text=t("decompilation_status", name=pyc_file.name)
        )
        self.decompile_button.configure(state="disabled")
        self.start_decompilation_spinner()

        def worker() -> None:
            try:
                pyc_relative = pyc_file.relative_to(WORK_FOLDER).as_posix()
                source = decompile_with_api(pyc_file)
                output_file = pyc_file.with_suffix(".py")
                output_file.write_text(source, encoding="utf-8")
                record_decompilation(
                    pyc_relative,
                    output_file.relative_to(WORK_FOLDER).as_posix(),
                )
                self.after(
                    0,
                    lambda: self.finish_decompilation([output_file], 0),
                )
            except Exception as error:
                self.after(
                    0,
                    lambda message=str(error): self.show_decompile_error(message),
                )
                return

        threading.Thread(target=worker, daemon=True).start()

    def start_decompilation_spinner(self) -> None:
        self.stop_decompilation_spinner()
        self.decompilation_spinner.pack(pady=(12, 4), anchor="center")
        self.animate_decompilation_spinner()

    def animate_decompilation_spinner(self) -> None:
        if not self._decompilation_in_progress:
            return
        self.decompilation_spinner.delete("all")
        self.decompilation_spinner.create_arc(
            5,
            5,
            27,
            27,
            start=self._spinner_angle,
            extent=285,
            style="arc",
            outline="#39d353",
            width=3,
        )
        self._spinner_angle = (self._spinner_angle + 18) % 360
        self._spinner_job = self.after(40, self.animate_decompilation_spinner)

    def stop_decompilation_spinner(self) -> None:
        if self._spinner_job is not None:
            try:
                self.after_cancel(self._spinner_job)
            except tk.TclError:
                pass
            self._spinner_job = None
        self.decompilation_spinner.pack_forget()
        self.decompilation_spinner.delete("all")

    def finish_decompilation(self, output_files: list[Path], exit_code: int | None) -> None:
        if exit_code not in (None, 0):
            self.show_decompile_error(t("decompilation_exit", code=exit_code))
            return
        if not output_files:
            self.show_decompile_error(t("decompilation_empty"))
            return
        self._decompilation_in_progress = False
        self.stop_decompilation_spinner()
        self.decompilation_status.configure(text="")
        self.populate_tree()
        self.open_file(output_files[0])
        self.show_windows_notification()

    @staticmethod
    def show_windows_notification() -> None:
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;"
            "$n.Visible=$true;"
            "$n.BalloonTipTitle='Dpyle';"
            f"$n.BalloonTipText='{t('decompilation_done')}';"
            "$n.ShowBalloonTip(5000);"
            "Start-Sleep -Seconds 6;"
            "$n.Dispose();"
        )
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                script,
            ],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def show_decompile_error(self, message: str) -> None:
        self._decompilation_in_progress = False
        self.stop_decompilation_spinner()
        self.decompilation_status.configure(text="")
        self.decompile_button.configure(state="normal")
        self.unavailable_reason_label.configure(
            text=t("decompilation_failed", message=message)
        )
        self.status.configure(text=t("decompilation_error"))

    def show_source(
        self,
        content: str,
        file_path: Path,
        highlighted_tokens: list[tuple[str, str, str]] | None = None,
    ) -> None:
        """Display already-loaded source content."""
        self.editor.configure(state="normal")
        self.editor.delete("1.0", "end")
        self.editor.tag_delete(*self.editor.tag_names())
        self.editor.insert("1.0", content)
        self.editor.configure(state="disabled")
        self.update_line_numbers()
        if highlighted_tokens is None:
            lexer = self.get_file_lexer(file_path, content)
            highlighted_tokens = [
                (f"token_{str(token)}", value, self.token_color(token))
                for token, value in lex(content, lexer)
            ]
        offset = 0
        for tag, value, color in highlighted_tokens:
            if tag not in self.editor.tag_names():
                self.editor.tag_configure(tag, foreground=color)
            start = f"1.0 + {offset} chars"
            end = f"1.0 + {offset + len(value)} chars"
            self.editor.tag_add(tag, start, end)
            offset += len(value)
        self.editor.yview_moveto(0)
        self.update_line_numbers()

    def start_source_render(
        self,
        load_id: int,
        content: str,
        file_path: Path,
        highlighted_tokens: list[tuple[str, str, str]],
    ) -> None:
        self.editor.configure(state="normal")
        self.editor.delete("1.0", "end")
        self.editor.tag_delete(*self.editor.tag_names())
        self._rendering_source = True
        self._source_render = {
            "load_id": load_id,
            "content": content,
            "file_path": file_path,
            "tokens": highlighted_tokens,
            "content_offset": 0,
            "token_index": 0,
            "token_offset": 0,
        }
        self.after_idle(self.render_source_chunk)

    def render_source_chunk(self) -> None:
        render = self._source_render
        if render["load_id"] != self._file_load_id:
            return

        content = render["content"]
        start = render["content_offset"]
        end = min(start + 16_000, len(content))
        if start < end:
            self.editor.insert("end", content[start:end])
            render["content_offset"] = end
            self.after(1, self.render_source_chunk)
            return

        self.editor.configure(state="disabled")
        self._rendering_source = False
        self.after_idle(self.apply_source_tags_chunk)

    def apply_source_tags_chunk(self) -> None:
        render = self._source_render
        if render["load_id"] != self._file_load_id:
            return

        tokens = render["tokens"]
        token_index = render["token_index"]
        offset = render["token_offset"]
        for _ in range(100):
            if token_index >= len(tokens):
                self.editor.yview_moveto(0)
                self.update_line_numbers()
                self.stop_loading()
                self.status.configure(text=render["file_path"].name)
                return
            tag, value, color = tokens[token_index]
            if tag not in self.editor.tag_names():
                self.editor.tag_configure(tag, foreground=color)
            self.editor.tag_add(tag, f"1.0 + {offset} chars", f"1.0 + {offset + len(value)} chars")
            offset += len(value)
            token_index += 1

        render["token_index"] = token_index
        render["token_offset"] = offset
        self.after(1, self.apply_source_tags_chunk)

    def update_editor_scroll(self, first: str, last: str) -> None:
        self.editor_scrollbar.set(first, last)
        self.schedule_line_numbers_update()

    def schedule_line_numbers_update(self) -> None:
        if self._line_number_update_job is None:
            self._line_number_update_job = self.after_idle(
                self.update_line_numbers
            )

    def update_line_numbers(self) -> None:
        self._line_number_update_job = None
        self.line_numbers.delete("all")
        if not self.editor.winfo_ismapped():
            return

        first_line_info = self.editor.dlineinfo("@0,0")
        if first_line_info is None:
            return
        y = first_line_info[1]
        line_height = first_line_info[3]
        while y < self.editor.winfo_height():
            visible_index = self.editor.index(f"@0,{max(0, y)}")
            line_number = int(visible_index.split(".")[0])
            self.line_numbers.create_text(
                42,
                y,
                anchor="ne",
                text=str(line_number),
                fill="#858585",
                font=("Consolas", 12),
            )
            y += line_height

    def show_editor_context_menu(self, event: tk.Event) -> str:
        self.editor_context_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def copy_selected_text(self) -> None:
        try:
            selected_text = self.editor.get("sel.first", "sel.last")
        except tk.TclError:
            return
        self.clipboard_clear()
        self.clipboard_append(selected_text)

    @staticmethod
    def token_color(token: Token) -> str:
        if token in Comment:
            return "#6a9955"
        if token in Keyword:
            return "#569cd6"
        if token in String:
            return "#ce9178"
        if token in Number:
            return "#b5cea8"
        if token in Name.Function:
            return "#dcdcaa"
        if token in Name:
            return "#9cdcfe"
        if token in Operator:
            return "#d4d4d4"
        return "#d4d4d4"

    def show_error(self, message: str) -> None:
        self.status.configure(text=t("generic_error", message=message))


if __name__ == "__main__":
    ctk.set_appearance_mode("System")
    ctk.set_default_color_theme("blue")
    app = DpyleGUI()
    app.mainloop()
