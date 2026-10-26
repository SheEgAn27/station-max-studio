from __future__ import annotations

import json
import math
import os
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
import certifi
from PIL import Image, ImageDraw, ImageEnhance, ImageOps

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

from station_network import (
    MediaServer,
    YandexCaptchaError,
    find_home_assistant_token,
    find_station_ip,
    list_stations,
    load_settings,
    qr_login,
    read_home_assistant_token,
    save_settings,
    send_gif,
    token_from_cookies,
)


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

APP_NAME = "Station Max Studio"
APP_VERSION = "2.1.3"

WEBVIEW2_CLIENT_ID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_INSTALLER_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
WEB_LOGIN_RESULT_ENV = "STATION_MAX_STUDIO_WEB_LOGIN_RESULT"

APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
ASSET_DIR = RESOURCE_DIR / "assets"
LOGO_SOURCE = ASSET_DIR / "SheEgAn-logo.png"
READY_DIR = APP_DIR / "Готовые GIF"
DATA_DIR = Path(os.environ.get("APPDATA", APP_DIR)) / "Station Max Studio"
HISTORY_PATH = DATA_DIR / "history.json"
SCREEN_SIZE = (25, 16)
MAX_FRAMES = 6000
MAX_GIF_FRAME_SECONDS = 655.35
MAX_STATIC_SECONDS = MAX_FRAMES * MAX_GIF_FRAME_SECONDS

READY_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpeg", ".mpg"}

COLORS = {
    "window": "#0a0d18",
    "sidebar": "#111522",
    "panel": "#171b2a",
    "panel_alt": "#1d2233",
    "border": "#30374d",
    "text": "#f7f4ff",
    "muted": "#9aa3bd",
    "accent": "#8b35ff",
    "accent_hover": "#a855f7",
    "accent_dark": "#3c245c",
    "success": "#22c55e",
    "danger": "#ef4444",
}


class WebView2MissingError(RuntimeError):
    pass


def webview2_runtime_version() -> str:
    """Return the installed Evergreen WebView2 version using Microsoft's registry keys."""
    if sys.platform != "win32":
        return ""
    try:
        import winreg
    except ImportError:
        return ""

    locations = (
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}"),
        (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}"),
    )
    for root, path in locations:
        try:
            with winreg.OpenKey(root, path) as key:
                version = str(winreg.QueryValueEx(key, "pv")[0]).strip()
            if version and version != "0.0.0.0":
                return version
        except OSError:
            continue
    return ""


def _application_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve())]


def _cookies_to_records(cookies) -> list[dict]:
    records = []
    for cookie in cookies or []:
        if not hasattr(cookie, "items"):
            continue
        for name, morsel in cookie.items():
            records.append(
                {
                    "name": name,
                    "value": morsel.value,
                    "domain": morsel["domain"],
                    "path": morsel["path"],
                }
            )
    return records


class CursorOnlySlider(ctk.CTkSlider):
    """Slider that ignores wheel input while keeping panel scrolling intact."""

    def _mouse_scroll_event(self, event):
        parent = self.master
        while parent is not None:
            if isinstance(parent, ctk.CTkScrollableFrame):
                delta = getattr(event, "delta", 0)
                if delta:
                    units = -int(delta / 6) if sys.platform.startswith("win") else (-1 if delta > 0 else 1)
                    parent._parent_canvas.yview_scroll(units, "units")
                break
            parent = getattr(parent, "master", None)
        return None

FIT_MODES = {
    "Вписать целиком": "contain",
    "Заполнить с обрезкой": "cover",
    "Растянуть": "stretch",
}

DURATIONS = {
    "10 секунд": 10,
    "1 минута": 60,
    "10 минут": 600,
    "1 час": 3600,
    "Максимум (~45,5 суток)": MAX_STATIC_SECONDS,
}

DURATION_UNITS = {
    "секунд": 1,
    "минут": 60,
    "часов": 3600,
    "дней": 86400,
}


def safe_name(value: str) -> str:
    value = re.sub(r"[^\w\-. ]+", "_", value, flags=re.UNICODE).strip(" ._")
    return value[:70] or "media"


def editor_defaults() -> dict:
    return {
        "fit_mode": "contain",
        "zoom": 1.0,
        "pan_x": 0.0,
        "pan_y": 0.0,
        "brightness": 1.0,
        "contrast": 1.25,
        "invert": False,
        "flip": False,
        "rotation": 0,
    }


def transform_frame(source: Image.Image, options: dict) -> Image.Image:
    rgba = source.convert("RGBA")
    if options.get("flip"):
        rgba = ImageOps.mirror(rgba)
    rotation = int(options.get("rotation", 0)) % 360
    if rotation:
        rgba = rgba.rotate(rotation, expand=True, resample=Image.Resampling.BICUBIC)

    target_w, target_h = SCREEN_SIZE
    fit_mode = options.get("fit_mode", "contain")
    if fit_mode == "stretch":
        width, height = target_w, target_h
    else:
        contain_scale = min(target_w / rgba.width, target_h / rgba.height)
        cover_scale = max(target_w / rgba.width, target_h / rgba.height)
        scale = cover_scale if fit_mode == "cover" else contain_scale
        width = max(1, round(rgba.width * scale))
        height = max(1, round(rgba.height * scale))

    zoom = max(0.25, min(4.0, float(options.get("zoom", 1.0))))
    width = max(1, round(width * zoom))
    height = max(1, round(height * zoom))
    rgba = rgba.resize((width, height), Image.Resampling.LANCZOS)

    pan_x = max(-1.0, min(1.0, float(options.get("pan_x", 0.0))))
    pan_y = max(-1.0, min(1.0, float(options.get("pan_y", 0.0))))
    center_x = (target_w - width) / 2
    center_y = (target_h - height) / 2
    travel_x = abs(target_w - width) / 2
    travel_y = abs(target_h - height) / 2
    x = round(center_x - pan_x * travel_x if width >= target_w else center_x + pan_x * travel_x)
    y = round(center_y - pan_y * travel_y if height >= target_h else center_y + pan_y * travel_y)

    canvas = Image.new("RGBA", SCREEN_SIZE, (0, 0, 0, 255))
    canvas.alpha_composite(rgba, (x, y))
    gray = canvas.convert("L")
    gray = ImageEnhance.Brightness(gray).enhance(float(options.get("brightness", 1.0)))
    gray = ImageEnhance.Contrast(gray).enhance(float(options.get("contrast", 1.25)))
    if options.get("invert"):
        gray = ImageOps.invert(gray)
    return gray


def static_frames(frame: Image.Image, seconds: float):
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Время показа должно быть больше нуля")
    if seconds > MAX_STATIC_SECONDS:
        raise ValueError("Максимальное время показа — 45 дней 12 часов 15 минут")
    total_ms = max(100, round(seconds * 1000))
    count = max(2, math.ceil(total_ms / 655350))
    delay = max(20, min(655350, total_ms // count))
    corner = frame.getpixel((24, 15))
    alternate = 1 if corner == 0 else corner - 1
    frames = []
    for index in range(count):
        current = frame.copy()
        if index % 2:
            current.putpixel((24, 15), alternate)
        frames.append(current)
    return frames, [delay] * len(frames)


def keep_frames_distinct(frames: list[Image.Image]) -> None:
    for index, frame in enumerate(frames):
        value = frame.getpixel((24, 15))
        frame.putpixel((24, 15), (value & 0xFE) | (index & 1))


def save_gif(frames, durations, destination: Path) -> None:
    if not frames:
        raise ValueError("В файле нет кадров")
    frames[0].save(
        destination,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        disposal=2,
        optimize=False,
    )


def unique_output(source: Path, suffix="") -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    extra = f"-{suffix}" if suffix else ""
    return READY_DIR / f"{safe_name(source.stem)}-{stamp}{extra}-25x16.gif"


def convert_image(source: Path, options: dict, seconds: float) -> Path:
    destination = unique_output(source)
    with Image.open(source) as image:
        count = getattr(image, "n_frames", 1)
        if count > MAX_FRAMES:
            raise ValueError(f"Слишком много кадров: {count}. Максимум: {MAX_FRAMES}")
        frames = []
        durations = []
        for index in range(count):
            image.seek(index)
            frames.append(transform_frame(image, options))
            durations.append(max(20, min(655350, int(image.info.get("duration", 100)))))
    if len(frames) == 1:
        frames, durations = static_frames(frames[0], seconds)
    else:
        keep_frames_distinct(frames)
    save_gif(frames, durations, destination)
    return destination


def ffmpeg_executable() -> str:
    if imageio_ffmpeg is None:
        raise RuntimeError("Модуль обработки видео отсутствует в этой сборке")
    return imageio_ffmpeg.get_ffmpeg_exe()


def first_frame(source: Path) -> Image.Image:
    if source.suffix.lower() in VIDEO_EXTENSIONS:
        with tempfile.TemporaryDirectory(prefix="station-max-preview-") as temp:
            output = Path(temp) / "preview.png"
            result = subprocess.run(
                [
                    ffmpeg_executable(),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    str(output),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0 or not output.exists():
                raise RuntimeError((result.stderr or "Не удалось прочитать видео").strip())
            with Image.open(output) as image:
                return image.convert("RGBA").copy()
    with Image.open(source) as image:
        image.seek(0)
        return image.convert("RGBA").copy()


def convert_video(
    source: Path,
    options: dict,
    fps: int,
    max_seconds: int,
    progress=None,
) -> Path:
    destination = unique_output(source, "video")
    with tempfile.TemporaryDirectory(prefix="station-max-video-") as temp:
        pattern = str(Path(temp) / "frame-%06d.png")
        command = [
            ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-t",
            str(max_seconds),
            "-vf",
            f"fps={fps}",
            "-frames:v",
            str(MAX_FRAMES),
            pattern,
        ]
        if progress:
            progress("Извлекаю кадры из видео…")
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(120, max_seconds * 5),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or "FFmpeg не смог обработать видео").strip())
        paths = sorted(Path(temp).glob("frame-*.png"))
        if not paths:
            raise RuntimeError("В видео не найдено ни одного кадра")
        frames = []
        for index, path in enumerate(paths):
            if progress and index % 25 == 0:
                progress(f"Готовлю кадры: {index + 1}/{len(paths)}")
            with Image.open(path) as image:
                frames.append(transform_frame(image, options))
        keep_frames_distinct(frames)
        delay = max(20, round(1000 / fps))
        save_gif(frames, [delay] * len(frames), destination)
    return destination


def save_drawing(pixels, seconds: float) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = READY_DIR / f"drawing-{stamp}-25x16.gif"
    frame = Image.new("L", SCREEN_SIZE)
    frame.putdata([value for row in pixels for value in row])
    frames, durations = static_frames(frame, seconds)
    save_gif(frames, durations, destination)
    return destination


def render_led_preview(frame: Image.Image, cell=22) -> Image.Image:
    frame = frame.convert("L")
    width, height = SCREEN_SIZE[0] * cell, SCREEN_SIZE[1] * cell
    output = Image.new("RGB", (width, height), "#080a11")
    draw = ImageDraw.Draw(output)
    for y in range(SCREEN_SIZE[1]):
        for x in range(SCREEN_SIZE[0]):
            value = frame.getpixel((x, y))
            x0, y0 = x * cell, y * cell
            draw.rounded_rectangle(
                (x0 + 2, y0 + 2, x0 + cell - 3, y0 + cell - 3),
                radius=4,
                fill=(value, value, value),
                outline=(38, 43, 59),
                width=1,
            )
    return output


def read_history() -> list[dict]:
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def append_history(path: Path, action: str) -> None:
    history = read_history()
    history.insert(
        0,
        {
            "file": str(path),
            "name": path.name,
            "action": action,
            "time": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        },
    )
    HISTORY_PATH.write_text(
        json.dumps(history[:100], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class MediaEditor(ctk.CTkFrame):
    def __init__(self, master, app, video=False):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.video = video
        self.source_path: Path | None = None
        self.source_frame: Image.Image | None = None
        self.preview_ctk = None
        self.rotation = 0

        self.fit_var = tk.StringVar(value="Вписать целиком")
        self.zoom_var = tk.DoubleVar(value=1.0)
        self.pan_x_var = tk.DoubleVar(value=0.0)
        self.pan_y_var = tk.DoubleVar(value=0.0)
        self.brightness_var = tk.DoubleVar(value=1.0)
        self.contrast_var = tk.DoubleVar(value=1.25)
        self.invert_var = tk.BooleanVar(value=False)
        self.flip_var = tk.BooleanVar(value=False)
        self.duration_value_var = tk.StringVar(value="1")
        self.duration_unit_var = tk.StringVar(value="часов")
        self.fps_var = tk.StringVar(value="10")
        self.video_length_var = tk.StringVar(value="30")

        self.grid_columnconfigure(0, minsize=430)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_controls()
        self._build_preview()

    def _heading(self, master, text, subtitle=""):
        ctk.CTkLabel(
            master,
            text=text,
            text_color=COLORS["text"],
            font=ctk.CTkFont("Segoe UI", 23, "bold"),
            anchor="w",
        ).pack(fill="x")
        if subtitle:
            ctk.CTkLabel(
                master,
                text=subtitle,
                text_color=COLORS["muted"],
                font=ctk.CTkFont("Segoe UI", 13),
                anchor="w",
                justify="left",
                wraplength=380,
            ).pack(fill="x", pady=(3, 15))

    def _build_controls(self):
        left = ctk.CTkScrollableFrame(
            self,
            width=420,
            fg_color=COLORS["panel"],
            corner_radius=18,
            scrollbar_button_color=COLORS["accent_dark"],
        )
        self.controls_scroll = left
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        title = "Видео" if self.video else "Изображение"
        subtitle = (
            "Выберите ролик — первый кадр появится в предпросмотре."
            if self.video
            else "Выберите фото или GIF и сразу настройте вид матрицы."
        )
        self._heading(left, title, subtitle)

        upload = ctk.CTkFrame(
            left,
            fg_color=COLORS["panel_alt"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=16,
        )
        upload.pack(fill="x", pady=(0, 18))
        ctk.CTkLabel(
            upload,
            text="▣" if not self.video else "▶",
            font=ctk.CTkFont("Segoe UI Symbol", 34),
            text_color="#bfa4ff",
        ).pack(pady=(18, 4))
        ctk.CTkLabel(
            upload,
            text="Фото, GIF" if not self.video else "MP4, MOV, AVI, MKV, WebM",
            text_color=COLORS["muted"],
            font=ctk.CTkFont("Segoe UI", 12),
        ).pack()
        ctk.CTkButton(
            upload,
            text="Выбрать файл",
            height=44,
            corner_radius=12,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            font=ctk.CTkFont("Segoe UI", 14, "bold"),
            command=self.choose_file,
        ).pack(fill="x", padx=18, pady=(12, 12))
        self.file_label = ctk.CTkLabel(
            upload,
            text="Файл не выбран",
            text_color=COLORS["muted"],
            font=ctk.CTkFont("Segoe UI", 11),
            wraplength=350,
        )
        self.file_label.pack(padx=16, pady=(0, 14))

        ctk.CTkLabel(
            left,
            text="Редактор",
            text_color=COLORS["text"],
            font=ctk.CTkFont("Segoe UI", 17, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 10))

        self._row_label(left, "Размещение")
        ctk.CTkOptionMenu(
            left,
            values=list(FIT_MODES),
            variable=self.fit_var,
            command=lambda _value: self.update_preview(),
            height=38,
            corner_radius=10,
            fg_color=COLORS["panel_alt"],
            button_color=COLORS["accent_dark"],
            button_hover_color=COLORS["accent"],
        ).pack(fill="x", pady=(0, 12))

        self._slider(left, "Масштаб", self.zoom_var, 0.5, 3.0, lambda v: f"{v:.2f}×")
        self._slider(left, "Сдвиг по горизонтали", self.pan_x_var, -1.0, 1.0, lambda v: f"{v:+.2f}")
        self._slider(left, "Сдвиг по вертикали", self.pan_y_var, -1.0, 1.0, lambda v: f"{v:+.2f}")
        self._slider(left, "Яркость", self.brightness_var, 0.2, 2.0, lambda v: f"{v:.2f}")
        self._slider(left, "Контраст", self.contrast_var, 0.3, 3.0, lambda v: f"{v:.2f}")

        edit_buttons = ctk.CTkFrame(left, fg_color="transparent")
        edit_buttons.pack(fill="x", pady=(3, 10))
        for column in range(3):
            edit_buttons.grid_columnconfigure(column, weight=1)
        ctk.CTkButton(
            edit_buttons,
            text="↶ 90°",
            fg_color=COLORS["panel_alt"],
            hover_color=COLORS["accent_dark"],
            command=lambda: self.rotate(-90),
        ).grid(row=0, column=0, sticky="ew", padx=(0, 5))
        ctk.CTkButton(
            edit_buttons,
            text="↷ 90°",
            fg_color=COLORS["panel_alt"],
            hover_color=COLORS["accent_dark"],
            command=lambda: self.rotate(90),
        ).grid(row=0, column=1, sticky="ew", padx=5)
        ctk.CTkButton(
            edit_buttons,
            text="Сбросить",
            fg_color=COLORS["panel_alt"],
            hover_color=COLORS["accent_dark"],
            command=self.reset_editor,
        ).grid(row=0, column=2, sticky="ew", padx=(5, 0))

        switches = ctk.CTkFrame(left, fg_color="transparent")
        switches.pack(fill="x", pady=(0, 14))
        ctk.CTkSwitch(
            switches,
            text="Отразить",
            variable=self.flip_var,
            command=self.update_preview,
            progress_color=COLORS["accent"],
        ).pack(side="left", padx=(0, 18))
        ctk.CTkSwitch(
            switches,
            text="Инверсия",
            variable=self.invert_var,
            command=self.update_preview,
            progress_color=COLORS["accent"],
        ).pack(side="left")

        if self.video:
            video_row = ctk.CTkFrame(left, fg_color="transparent")
            video_row.pack(fill="x", pady=(0, 14))
            video_row.grid_columnconfigure((0, 1), weight=1)
            self._compact_option(video_row, 0, "Кадров/с", self.fps_var, ["5", "10", "15", "20"])
            self._compact_option(video_row, 1, "Макс. секунд", self.video_length_var, ["10", "30", "60", "120"])
        else:
            self._row_label(left, "Время показа статичного фото")
            duration_row = ctk.CTkFrame(left, fg_color="transparent")
            duration_row.pack(fill="x", pady=(0, 7))
            duration_row.grid_columnconfigure(0, weight=1)
            ctk.CTkEntry(
                duration_row,
                textvariable=self.duration_value_var,
                height=38,
                fg_color=COLORS["panel_alt"],
                border_color=COLORS["border"],
                placeholder_text="Введите число",
            ).grid(row=0, column=0, sticky="ew", padx=(0, 7))
            ctk.CTkOptionMenu(
                duration_row,
                values=list(DURATION_UNITS),
                variable=self.duration_unit_var,
                width=112,
                height=38,
                corner_radius=10,
                fg_color=COLORS["panel_alt"],
                button_color=COLORS["accent_dark"],
                button_hover_color=COLORS["accent"],
            ).grid(row=0, column=1, sticky="e")
            ctk.CTkButton(
                left,
                text="Установить максимум — 45 д 12 ч 15 мин",
                height=34,
                fg_color=COLORS["panel_alt"],
                hover_color=COLORS["accent_dark"],
                command=self.set_max_duration,
            ).pack(fill="x", pady=(0, 14))

        actions = ctk.CTkFrame(left, fg_color="transparent")
        actions.pack(fill="x", pady=(2, 18))
        actions.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(
            actions,
            text="Сохранить GIF",
            height=44,
            corner_radius=12,
            fg_color=COLORS["panel_alt"],
            hover_color=COLORS["accent_dark"],
            border_width=1,
            border_color=COLORS["border"],
            command=lambda: self.convert(False),
        ).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(
            actions,
            text="➤  Отправить",
            height=44,
            corner_radius=12,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            font=ctk.CTkFont("Segoe UI", 14, "bold"),
            command=lambda: self.convert(True),
        ).grid(row=0, column=1, sticky="ew", padx=(6, 0))

    def _build_preview(self):
        right = ctk.CTkFrame(self, fg_color=COLORS["panel"], corner_radius=18)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            right,
            text="Предпросмотр матрицы",
            text_color=COLORS["text"],
            font=ctk.CTkFont("Segoe UI", 23, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 0))
        ctk.CTkLabel(
            right,
            text="Именно так изображение будет выглядеть на Станции Макс",
            text_color=COLORS["muted"],
            font=ctk.CTkFont("Segoe UI", 13),
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=24, pady=(2, 12))

        preview_shell = ctk.CTkFrame(
            right,
            fg_color="#0b0e16",
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=18,
        )
        preview_shell.grid(row=2, column=0, sticky="nsew", padx=20, pady=(0, 12))
        self.preview_label = ctk.CTkLabel(
            preview_shell,
            text="Выберите файл для мгновенного предпросмотра",
            text_color=COLORS["muted"],
            font=ctk.CTkFont("Segoe UI", 14),
        )
        self.preview_label.pack(expand=True, fill="both", padx=12, pady=12)

        self.preview_info = ctk.CTkLabel(
            right,
            text="25 × 16 пикселей • 256 градаций серого",
            text_color="#c4b5fd",
            fg_color=COLORS["panel_alt"],
            corner_radius=10,
            font=ctk.CTkFont("Segoe UI", 12),
        )
        self.preview_info.grid(row=3, column=0, pady=(0, 12), ipadx=12, ipady=5)

        recent = ctk.CTkFrame(right, fg_color=COLORS["panel_alt"], corner_radius=14)
        recent.grid(row=4, column=0, sticky="ew", padx=20, pady=(0, 20))
        ctk.CTkLabel(
            recent,
            text="Последние файлы",
            text_color=COLORS["text"],
            font=ctk.CTkFont("Segoe UI", 14, "bold"),
        ).pack(anchor="w", padx=14, pady=(10, 4))
        self.recent_frame = ctk.CTkFrame(recent, fg_color="transparent")
        self.recent_frame.pack(fill="x", padx=10, pady=(0, 10))
        self.refresh_recent()

    def _row_label(self, master, text):
        ctk.CTkLabel(
            master,
            text=text,
            text_color=COLORS["muted"],
            font=ctk.CTkFont("Segoe UI", 12),
            anchor="w",
        ).pack(fill="x", pady=(0, 5))

    def _slider(self, master, title, variable, start, end, formatter):
        row = ctk.CTkFrame(master, fg_color="transparent")
        row.pack(fill="x", pady=(0, 3))
        label = ctk.CTkLabel(row, text=title, text_color=COLORS["muted"], anchor="w")
        label.pack(side="left")
        value_label = ctk.CTkLabel(row, text=formatter(variable.get()), text_color="#c4b5fd")
        value_label.pack(side="right")

        def changed(value):
            value_label.configure(text=formatter(float(value)))
            self.update_preview()

        slider = CursorOnlySlider(
            master,
            from_=start,
            to=end,
            variable=variable,
            command=changed,
            progress_color=COLORS["accent"],
            button_color="#f4f0ff",
            button_hover_color="white",
        )
        slider.pack(fill="x", pady=(0, 10))

    def _compact_option(self, master, column, title, variable, values):
        box = ctk.CTkFrame(master, fg_color="transparent")
        box.grid(row=0, column=column, sticky="ew", padx=(0, 6) if column == 0 else (6, 0))
        ctk.CTkLabel(box, text=title, text_color=COLORS["muted"], anchor="w").pack(fill="x")
        ctk.CTkOptionMenu(
            box,
            values=values,
            variable=variable,
            fg_color=COLORS["panel_alt"],
            button_color=COLORS["accent_dark"],
            height=36,
        ).pack(fill="x", pady=(4, 0))

    def set_max_duration(self):
        self.duration_value_var.set(str(int(MAX_STATIC_SECONDS)))
        self.duration_unit_var.set("секунд")

    def photo_duration_seconds(self) -> float:
        raw = self.duration_value_var.get().strip().replace(",", ".")
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError("Введите время показа числом") from exc
        seconds = value * DURATION_UNITS[self.duration_unit_var.get()]
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Время показа должно быть больше нуля")
        if seconds > MAX_STATIC_SECONDS:
            raise ValueError("Максимальное время показа — 45 дней 12 часов 15 минут")
        return seconds

    def choose_file(self):
        if self.video:
            filetypes = [("Видео", "*.mp4 *.mov *.avi *.mkv *.webm *.m4v *.mpeg *.mpg"), ("Все файлы", "*.*")]
        else:
            filetypes = [("Изображения", "*.png *.jpg *.jpeg *.gif *.webp *.bmp *.tif *.tiff"), ("Все файлы", "*.*")]
        path = filedialog.askopenfilename(title="Выберите файл", filetypes=filetypes)
        if path:
            self.load_path(Path(path))

    def load_path(self, path: Path):
        if not path.exists():
            messagebox.showerror("Station Max Studio", "Файл не найден")
            return
        valid = VIDEO_EXTENSIONS if self.video else IMAGE_EXTENSIONS
        if path.suffix.lower() not in valid:
            messagebox.showerror("Station Max Studio", "Неподдерживаемый формат файла")
            return
        self.source_path = path
        self.file_label.configure(text=path.name)
        self.app.run_job(
            lambda: first_frame(path),
            self._loaded,
            "Создаю предпросмотр…",
        )

    def _loaded(self, image):
        self.source_frame = image
        self.update_preview()
        kind = "Видео" if self.video else "Источник"
        self.preview_info.configure(text=f"{kind}: {image.width}×{image.height}  →  экран 25×16")

    def settings(self):
        return {
            "fit_mode": FIT_MODES[self.fit_var.get()],
            "zoom": float(self.zoom_var.get()),
            "pan_x": float(self.pan_x_var.get()),
            "pan_y": float(self.pan_y_var.get()),
            "brightness": float(self.brightness_var.get()),
            "contrast": float(self.contrast_var.get()),
            "invert": bool(self.invert_var.get()),
            "flip": bool(self.flip_var.get()),
            "rotation": self.rotation,
        }

    def update_preview(self):
        if self.source_frame is None:
            return
        try:
            transformed = transform_frame(self.source_frame, self.settings())
            rendered = render_led_preview(transformed)
            self.preview_ctk = ctk.CTkImage(
                light_image=rendered,
                dark_image=rendered,
                size=rendered.size,
            )
            self.preview_label.configure(image=self.preview_ctk, text="")
        except Exception as exc:
            self.preview_label.configure(image=None, text=f"Не удалось показать: {exc}")

    def rotate(self, degrees):
        self.rotation = (self.rotation + degrees) % 360
        self.update_preview()

    def reset_editor(self):
        defaults = editor_defaults()
        self.fit_var.set("Вписать целиком")
        self.zoom_var.set(defaults["zoom"])
        self.pan_x_var.set(defaults["pan_x"])
        self.pan_y_var.set(defaults["pan_y"])
        self.brightness_var.set(defaults["brightness"])
        self.contrast_var.set(defaults["contrast"])
        self.invert_var.set(defaults["invert"])
        self.flip_var.set(defaults["flip"])
        self.rotation = 0
        self.update_preview()

    def convert(self, send_after):
        if self.source_path is None:
            messagebox.showinfo("Station Max Studio", "Сначала выберите файл")
            return
        source = self.source_path
        options = self.settings()
        try:
            if self.video:
                video_fps = int(self.fps_var.get())
                video_seconds = int(self.video_length_var.get())
                photo_seconds = None
            else:
                video_fps = video_seconds = None
                photo_seconds = self.photo_duration_seconds()
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Station Max Studio", str(exc))
            return

        def progress(text):
            self.app.after(0, lambda: self.app.set_status(text))

        def job():
            if self.video:
                output = convert_video(
                    source,
                    options,
                    video_fps,
                    video_seconds,
                    progress,
                )
            else:
                output = convert_image(source, options, photo_seconds)
            if send_after:
                self.app.send_path(output)
            return output

        self.app.run_job(
            job,
            lambda output: self._converted(output, send_after),
            "Преобразую файл…",
        )

    def _converted(self, output, sent):
        append_history(output, "Отправлено на Станцию" if sent else "Сохранено")
        self.app.refresh_library_views()
        message = "Файл сохранён и отправлен" if sent else "GIF сохранён"
        messagebox.showinfo("Station Max Studio", f"{message}:\n{output}")

    def refresh_recent(self):
        if not hasattr(self, "recent_frame"):
            return
        for widget in self.recent_frame.winfo_children():
            widget.destroy()
        paths = sorted(READY_DIR.glob("*.gif"), key=lambda p: p.stat().st_mtime, reverse=True)
        paths = [path for path in paths if not path.name.startswith("SheEgAn-connect")][:4]
        if not paths:
            ctk.CTkLabel(
                self.recent_frame,
                text="Здесь появятся последние сохранённые GIF",
                text_color=COLORS["muted"],
            ).pack(anchor="w", padx=4, pady=5)
            return
        for path in paths:
            ctk.CTkButton(
                self.recent_frame,
                text=path.stem[:22],
                height=32,
                fg_color="#252b40",
                hover_color=COLORS["accent_dark"],
                command=lambda item=path: self.open_recent(item),
            ).pack(side="left", padx=4, fill="x", expand=True)

    def open_recent(self, path: Path):
        if self.video:
            self.app.show_page("screen")
            self.app.image_editor.load_path(path)
        else:
            self.load_path(path)


class DrawingPage(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.pixels = [[0 for _ in range(25)] for _ in range(16)]
        self.cell = 27
        self.brush = tk.IntVar(value=255)
        self.brush_size = tk.IntVar(value=1)
        self.duration = tk.StringVar(value="1 час")
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        controls = ctk.CTkFrame(self, width=300, fg_color=COLORS["panel"], corner_radius=18)
        controls.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        controls.grid_propagate(False)
        ctk.CTkLabel(controls, text="Рисование", font=ctk.CTkFont("Segoe UI", 23, "bold")).pack(anchor="w", padx=20, pady=(20, 4))
        ctk.CTkLabel(controls, text="ЛКМ — рисовать, ПКМ — стирать", text_color=COLORS["muted"]).pack(anchor="w", padx=20, pady=(0, 20))
        ctk.CTkLabel(controls, text="Яркость светодиода", text_color=COLORS["muted"]).pack(anchor="w", padx=20)
        CursorOnlySlider(controls, from_=0, to=255, variable=self.brush, progress_color=COLORS["accent"]).pack(fill="x", padx=20, pady=(6, 16))
        ctk.CTkLabel(controls, text="Размер кисти", text_color=COLORS["muted"]).pack(anchor="w", padx=20)
        ctk.CTkOptionMenu(controls, values=["1", "2", "3", "4"], command=lambda value: self.brush_size.set(int(value)), fg_color=COLORS["panel_alt"], button_color=COLORS["accent_dark"]).pack(fill="x", padx=20, pady=(6, 16))
        for text, command in (("Очистить", self.clear), ("Залить", self.fill), ("Инвертировать", self.invert)):
            ctk.CTkButton(controls, text=text, fg_color=COLORS["panel_alt"], hover_color=COLORS["accent_dark"], command=command).pack(fill="x", padx=20, pady=4)
        ctk.CTkLabel(controls, text="Время показа", text_color=COLORS["muted"]).pack(anchor="w", padx=20, pady=(20, 4))
        ctk.CTkOptionMenu(controls, values=list(DURATIONS), variable=self.duration, fg_color=COLORS["panel_alt"], button_color=COLORS["accent_dark"]).pack(fill="x", padx=20)
        ctk.CTkButton(controls, text="Сохранить рисунок", fg_color=COLORS["panel_alt"], hover_color=COLORS["accent_dark"], command=lambda: self.save(False)).pack(fill="x", padx=20, pady=(22, 6))
        ctk.CTkButton(controls, text="➤  Отправить на Станцию", fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], command=lambda: self.save(True)).pack(fill="x", padx=20)

        board_frame = ctk.CTkFrame(self, fg_color=COLORS["panel"], corner_radius=18)
        board_frame.grid(row=0, column=1, sticky="nsew")
        ctk.CTkLabel(board_frame, text="Матрица 25 × 16", font=ctk.CTkFont("Segoe UI", 23, "bold")).pack(anchor="w", padx=24, pady=(20, 0))
        ctk.CTkLabel(board_frame, text="Каждая клетка соответствует одному светодиоду Макса", text_color=COLORS["muted"]).pack(anchor="w", padx=24, pady=(2, 10))
        self.canvas = tk.Canvas(board_frame, width=25 * self.cell, height=16 * self.cell, bg="#070910", highlightthickness=0)
        self.canvas.pack(expand=True, padx=20, pady=20)
        self.canvas.bind("<Button-1>", lambda event: self.paint(event, False))
        self.canvas.bind("<B1-Motion>", lambda event: self.paint(event, False))
        self.canvas.bind("<Button-3>", lambda event: self.paint(event, True))
        self.canvas.bind("<B3-Motion>", lambda event: self.paint(event, True))
        self.draw()

    def paint(self, event, erase):
        x, y = event.x // self.cell, event.y // self.cell
        if not (0 <= x < 25 and 0 <= y < 16):
            return
        value = 0 if erase else max(0, min(255, int(self.brush.get())))
        radius = max(1, int(self.brush_size.get())) - 1
        for yy in range(max(0, y - radius), min(16, y + radius + 1)):
            for xx in range(max(0, x - radius), min(25, x + radius + 1)):
                self.pixels[yy][xx] = value
        self.draw()

    def draw(self):
        self.canvas.delete("all")
        for y in range(16):
            for x in range(25):
                value = self.pixels[y][x]
                color = f"#{value:02x}{value:02x}{value:02x}"
                x0, y0 = x * self.cell, y * self.cell
                self.canvas.create_rectangle(x0 + 1, y0 + 1, x0 + self.cell - 2, y0 + self.cell - 2, fill=color, outline="#2d3347", width=1)

    def clear(self):
        self.pixels = [[0 for _ in range(25)] for _ in range(16)]
        self.draw()

    def fill(self):
        value = max(0, min(255, int(self.brush.get())))
        self.pixels = [[value for _ in range(25)] for _ in range(16)]
        self.draw()

    def invert(self):
        self.pixels = [[255 - value for value in row] for row in self.pixels]
        self.draw()

    def save(self, send_after):
        snapshot = [row[:] for row in self.pixels]

        def job():
            output = save_drawing(snapshot, DURATIONS[self.duration.get()])
            if send_after:
                self.app.send_path(output)
            return output

        self.app.run_job(job, lambda output: self.saved(output, send_after), "Сохраняю рисунок…")

    def saved(self, output, sent):
        append_history(output, "Отправлено на Станцию" if sent else "Сохранено")
        self.app.refresh_library_views()
        messagebox.showinfo("Station Max Studio", ("Рисунок отправлен:\n" if sent else "Рисунок сохранён:\n") + str(output))


class LibraryPage(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(header, text="Готовые файлы", font=ctk.CTkFont("Segoe UI", 25, "bold")).pack(side="left")
        ctk.CTkButton(header, text="Открыть папку", width=130, fg_color=COLORS["panel_alt"], hover_color=COLORS["accent_dark"], command=lambda: os.startfile(READY_DIR)).pack(side="right")
        self.list_frame = ctk.CTkScrollableFrame(self, fg_color=COLORS["panel"], corner_radius=18)
        self.list_frame.pack(fill="both", expand=True)
        self.refresh()

    def refresh(self):
        for widget in self.list_frame.winfo_children():
            widget.destroy()
        paths = sorted(READY_DIR.glob("*.gif"), key=lambda p: p.stat().st_mtime, reverse=True)
        paths = [path for path in paths if not path.name.startswith("SheEgAn-connect")]
        if not paths:
            ctk.CTkLabel(self.list_frame, text="Пока нет готовых GIF", text_color=COLORS["muted"], font=ctk.CTkFont("Segoe UI", 16)).pack(pady=60)
            return
        for path in paths:
            row = ctk.CTkFrame(self.list_frame, fg_color=COLORS["panel_alt"], corner_radius=12)
            row.pack(fill="x", padx=8, pady=5)
            info = ctk.CTkFrame(row, fg_color="transparent")
            info.pack(side="left", fill="x", expand=True, padx=14, pady=10)
            ctk.CTkLabel(info, text=path.name, font=ctk.CTkFont("Segoe UI", 13, "bold"), anchor="w").pack(fill="x")
            ctk.CTkLabel(info, text=f"{path.stat().st_size / 1024:.1f} КБ • {datetime.fromtimestamp(path.stat().st_mtime):%d.%m.%Y %H:%M}", text_color=COLORS["muted"], anchor="w").pack(fill="x")
            ctk.CTkButton(row, text="Редактировать", width=110, fg_color="#272d42", hover_color=COLORS["accent_dark"], command=lambda item=path: self.edit(item)).pack(side="right", padx=5)
            ctk.CTkButton(row, text="Отправить", width=100, fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], command=lambda item=path: self.send(item)).pack(side="right", padx=(5, 14))

    def edit(self, path):
        self.app.show_page("screen")
        self.app.image_editor.load_path(path)

    def send(self, path):
        self.app.run_job(lambda: self.app.send_path(path), lambda _value: self.sent(path), "Отправляю GIF…")

    def sent(self, path):
        append_history(path, "Отправлено на Станцию")
        self.app.history_page.refresh()
        messagebox.showinfo("Station Max Studio", "GIF отправлен на Станцию")


class HistoryPage(ctk.CTkFrame):
    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(header, text="История", font=ctk.CTkFont("Segoe UI", 25, "bold")).pack(side="left")
        ctk.CTkButton(header, text="Очистить", width=100, fg_color=COLORS["panel_alt"], hover_color=COLORS["danger"], command=self.clear).pack(side="right")
        self.list_frame = ctk.CTkScrollableFrame(self, fg_color=COLORS["panel"], corner_radius=18)
        self.list_frame.pack(fill="both", expand=True)
        self.refresh()

    def refresh(self):
        for widget in self.list_frame.winfo_children():
            widget.destroy()
        history = read_history()
        if not history:
            ctk.CTkLabel(self.list_frame, text="История пока пуста", text_color=COLORS["muted"], font=ctk.CTkFont("Segoe UI", 16)).pack(pady=60)
            return
        for item in history:
            row = ctk.CTkFrame(self.list_frame, fg_color=COLORS["panel_alt"], corner_radius=12)
            row.pack(fill="x", padx=8, pady=5)
            ctk.CTkLabel(row, text="●", text_color=COLORS["success"] if "Отправлено" in item.get("action", "") else "#a78bfa").pack(side="left", padx=(14, 8))
            text = ctk.CTkFrame(row, fg_color="transparent")
            text.pack(side="left", fill="x", expand=True, pady=9)
            ctk.CTkLabel(text, text=item.get("name", "Файл"), font=ctk.CTkFont("Segoe UI", 13, "bold"), anchor="w").pack(fill="x")
            ctk.CTkLabel(text, text=item.get("action", "") + " • " + item.get("time", ""), text_color=COLORS["muted"], anchor="w").pack(fill="x")

    def clear(self):
        HISTORY_PATH.write_text("[]", encoding="utf-8")
        self.refresh()


class CookiesLoginDialog(ctk.CTkToplevel):
    def __init__(self, owner, submit):
        super().__init__(owner)
        self.submit = submit
        self.title("Вход через Cookies")
        self.geometry("720x570")
        self.minsize(620, 500)
        self.configure(fg_color=COLORS["window"])
        self.transient(owner)
        self.grab_set()

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=24, pady=22)
        ctk.CTkLabel(
            content,
            text="Вход через Cookies",
            font=ctk.CTkFont("Segoe UI", 24, "bold"),
            anchor="w",
        ).pack(fill="x")
        ctk.CTkLabel(
            content,
            text=(
                "Этот способ нужен, когда Яндекс показывает «Вы не робот?» и блокирует QR-вход.\n\n"
                "1. Откройте Яндекс и войдите в нужный аккаунт, пройдя проверку.\n"
                "2. Экспортируйте cookies домена yandex.ru в JSON или скопируйте строку Cookie.\n"
                "3. Вставьте их ниже. Приложение обменяет cookies на токен и не будет их сохранять."
            ),
            text_color=COLORS["muted"],
            justify="left",
            anchor="w",
            wraplength=650,
        ).pack(fill="x", pady=(8, 12))

        actions = ctk.CTkFrame(content, fg_color="transparent")
        actions.pack(fill="x", pady=(0, 10))
        ctk.CTkButton(
            actions,
            text="Открыть Яндекс",
            fg_color=COLORS["panel_alt"],
            hover_color=COLORS["accent_dark"],
            command=lambda: webbrowser.open("https://passport.yandex.ru/profile"),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            actions,
            text="Загрузить Cookies JSON…",
            fg_color=COLORS["panel_alt"],
            hover_color=COLORS["accent_dark"],
            command=self.load_json,
        ).pack(side="left")

        self.cookies = ctk.CTkTextbox(
            content,
            fg_color=COLORS["panel"],
            border_width=1,
            border_color=COLORS["border"],
            wrap="word",
        )
        self.cookies.pack(fill="both", expand=True)
        ctk.CTkLabel(
            content,
            text="Не отправляйте cookies другим людям: они дают доступ к аккаунту.",
            text_color="#fbbf24",
            anchor="w",
        ).pack(fill="x", pady=(10, 12))

        footer = ctk.CTkFrame(content, fg_color="transparent")
        footer.pack(fill="x")
        ctk.CTkButton(
            footer,
            text="Отмена",
            fg_color=COLORS["panel_alt"],
            hover_color=COLORS["accent_dark"],
            command=self.destroy,
        ).pack(side="right")
        ctk.CTkButton(
            footer,
            text="Войти",
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            command=self.confirm,
        ).pack(side="right", padx=(0, 8))

    def load_json(self):
        path = filedialog.askopenfilename(
            title="Выберите экспортированный Cookies JSON",
            parent=self,
            filetypes=(("JSON", "*.json"), ("Все файлы", "*.*")),
        )
        if not path:
            return
        try:
            value = Path(path).read_text(encoding="utf-8-sig")
        except OSError as exc:
            messagebox.showerror("Вход через Cookies", str(exc), parent=self)
            return
        self.cookies.delete("1.0", "end")
        self.cookies.insert("1.0", value)

    def confirm(self):
        value = self.cookies.get("1.0", "end").strip()
        if not value:
            messagebox.showinfo("Вход через Cookies", "Вставьте cookies или загрузите JSON-файл", parent=self)
            return
        self.destroy()
        self.submit(value)


class ConnectionDialog(ctk.CTkToplevel):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.settings = dict(owner.connection)
        self.station_map = {}
        self.working = False
        self.title("Настройка подключения")
        self.geometry("720x690")
        self.minsize(680, 620)
        self.configure(fg_color=COLORS["window"])
        self.transient(owner)
        self.grab_set()

        content = ctk.CTkScrollableFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=22, pady=20)
        ctk.CTkLabel(content, text="Подключение к Станции", font=ctk.CTkFont("Segoe UI", 26, "bold"), anchor="w").pack(fill="x")
        ctk.CTkLabel(content, text="Откройте безопасное окно Яндекса, войдите и пройдите проверку — остальное приложение сделает само.", text_color=COLORS["muted"], anchor="w", wraplength=650).pack(fill="x", pady=(2, 16))

        account = self._section(content, "1. Аккаунт Яндекса")
        self.account_label = ctk.CTkLabel(account, text="Авторизация сохранена" if self.settings.get("x_token") else "Вход ещё не выполнен", text_color=COLORS["success"] if self.settings.get("x_token") else COLORS["muted"], anchor="w")
        self.account_label.pack(fill="x", padx=16, pady=(0, 10))
        buttons = ctk.CTkFrame(account, fg_color="transparent")
        buttons.pack(fill="x", padx=16, pady=(0, 16))
        buttons.grid_columnconfigure((0, 1), weight=1)
        self.web_login_button = ctk.CTkButton(buttons, text="Войти в Яндекс", height=42, fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], command=self.login)
        self.web_login_button.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.login_button = ctk.CTkButton(buttons, text="Войти по QR", fg_color=COLORS["panel_alt"], hover_color=COLORS["accent_dark"], command=self.qr_login_action)
        self.login_button.grid(row=1, column=0, sticky="ew", padx=(0, 4), pady=(0, 8))
        self.cookies_button = ctk.CTkButton(buttons, text="Cookies вручную", fg_color=COLORS["panel_alt"], hover_color=COLORS["accent_dark"], command=self.cookies_login)
        self.cookies_button.grid(row=1, column=1, sticky="ew", padx=(4, 0), pady=(0, 8))
        self.import_button = ctk.CTkButton(buttons, text="Импорт из Home Assistant", fg_color=COLORS["panel_alt"], hover_color=COLORS["accent_dark"], command=self.import_ha)
        self.import_button.grid(row=2, column=0, sticky="ew", padx=(0, 4))
        self.settings_button = ctk.CTkButton(buttons, text="Импорт settings.json", fg_color=COLORS["panel_alt"], hover_color=COLORS["accent_dark"], command=self.import_settings)
        self.settings_button.grid(row=2, column=1, sticky="ew", padx=(4, 0))

        station = self._section(content, "2. Выбор колонки")
        self.station_var = tk.StringVar(value="Выберите колонку")
        self.station_menu = ctk.CTkOptionMenu(station, variable=self.station_var, values=["Выберите колонку"], command=self.station_selected, fg_color=COLORS["panel_alt"], button_color=COLORS["accent_dark"], height=40)
        self.station_menu.pack(fill="x", padx=16, pady=(0, 10))
        self.refresh_button = ctk.CTkButton(station, text="Обновить список", fg_color=COLORS["panel_alt"], hover_color=COLORS["accent_dark"], command=self.refresh_stations)
        self.refresh_button.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkLabel(station, text="IP колонки", text_color=COLORS["muted"], anchor="w").pack(fill="x", padx=16)
        ip_row = ctk.CTkFrame(station, fg_color="transparent")
        ip_row.pack(fill="x", padx=16, pady=(5, 16))
        self.ip_var = tk.StringVar(value=self.settings.get("station_ip", ""))
        ctk.CTkEntry(ip_row, textvariable=self.ip_var, height=40, fg_color=COLORS["panel_alt"], border_color=COLORS["border"]).pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.find_button = ctk.CTkButton(ip_row, text="Найти в сети", width=150, fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], command=self.find_ip)
        self.find_button.pack(side="right")

        local = self._section(content, "3. Локальная раздача")
        local_row = ctk.CTkFrame(local, fg_color="transparent")
        local_row.pack(fill="x", padx=16, pady=(0, 12))
        ctk.CTkLabel(local_row, text="Порт встроенного сервера", text_color=COLORS["muted"]).pack(side="left")
        self.port_var = tk.StringVar(value=str(self.settings.get("http_port", 8765)))
        ctk.CTkEntry(local_row, textvariable=self.port_var, width=100, height=36, fg_color=COLORS["panel_alt"], border_color=COLORS["border"]).pack(side="right")
        self.status_var = tk.StringVar(value="")
        ctk.CTkLabel(content, textvariable=self.status_var, text_color=COLORS["muted"], wraplength=620).pack(fill="x", pady=10)
        footer = ctk.CTkFrame(content, fg_color="transparent")
        footer.pack(fill="x", pady=(5, 8))
        ctk.CTkButton(footer, text="Отмена", fg_color=COLORS["panel_alt"], hover_color=COLORS["accent_dark"], command=self.destroy).pack(side="right")
        ctk.CTkButton(footer, text="Сохранить и подключиться", fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"], command=self.save).pack(side="right", padx=(0, 8))

        if self.settings.get("x_token"):
            self.after(180, self.refresh_stations)

    def _section(self, master, title):
        frame = ctk.CTkFrame(master, fg_color=COLORS["panel"], corner_radius=15)
        frame.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(frame, text=title, font=ctk.CTkFont("Segoe UI", 17, "bold"), anchor="w").pack(fill="x", padx=16, pady=(14, 10))
        return frame

    def set_working(self, value, status=""):
        self.working = value
        self.status_var.set(status or "")
        state = "disabled" if value else "normal"
        for button in (
            self.web_login_button,
            self.login_button,
            self.cookies_button,
            self.import_button,
            self.settings_button,
            self.refresh_button,
            self.find_button,
        ):
            button.configure(state=state)

    def run_job(self, job, done, status="Подождите…"):
        if self.working:
            return
        self.set_working(True, status)

        def worker():
            try:
                result = job()
            except Exception as exc:
                details = traceback.format_exc()
                self.after(0, lambda error=exc, trace=details: self.failed(error, trace))
                return
            self.after(0, lambda: self.completed(result, done))

        threading.Thread(target=worker, daemon=True).start()

    def completed(self, result, done):
        self.set_working(False, "")
        done(result)

    def failed(self, error, details):
        status = "QR-вход заблокирован — нажмите «Войти в Яндекс»" if isinstance(error, YandexCaptchaError) else "Ошибка"
        self.set_working(False, status)
        log = DATA_DIR / "last-error.log"
        log.write_text(details, encoding="utf-8")
        messagebox.showerror("Настройка подключения", f"{error}\n\nПодробности: {log}", parent=self)

    def login(self):
        version = webview2_runtime_version()
        if not version:
            install = messagebox.askyesno(
                "Компонент WebView2 не найден",
                "Для безопасного окна входа требуется Microsoft Edge WebView2 Runtime.\n\n"
                "Скачать и установить официальный компонент Microsoft сейчас?",
                parent=self,
            )
            if install:
                self.run_job(
                    self._install_webview2_job,
                    self.webview2_installed,
                    "Устанавливаю Microsoft WebView2…",
                )
            else:
                self.status_var.set("Можно использовать QR, Home Assistant, settings.json или Cookies вручную")
            return
        self.start_embedded_login()

    def start_embedded_login(self):
        self.run_job(
            self._embedded_login_job,
            self.login_finished,
            "В окне Яндекса войдите в аккаунт и пройдите проверку…",
        )

    def _embedded_login_job(self):
        result_path = Path(tempfile.gettempdir()) / f"station-max-login-{uuid.uuid4().hex}.json"
        environment = os.environ.copy()
        environment[WEB_LOGIN_RESULT_ENV] = str(result_path)
        try:
            process = subprocess.run(
                _application_command(),
                env=environment,
                timeout=12 * 60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if not result_path.exists():
                raise RuntimeError(
                    f"Окно входа завершилось без результата (код {process.returncode})"
                )
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("Время входа истекло. Откройте вход ещё раз") from exc
        except (OSError, ValueError) as exc:
            raise RuntimeError("Не удалось получить результат встроенного входа") from exc
        finally:
            try:
                result_path.unlink(missing_ok=True)
            except OSError:
                pass

        if result.get("status") == "success" and result.get("token"):
            token = str(result["token"])
            return token, list_stations(token)
        if result.get("code") == "webview2_missing":
            raise WebView2MissingError(result.get("message") or "Microsoft WebView2 Runtime не установлен")
        if result.get("code") == "cancelled":
            raise RuntimeError("Вход отменён пользователем")
        raise RuntimeError(result.get("message") or "Не удалось войти через встроенное окно")

    def _install_webview2_job(self):
        installer = Path(tempfile.gettempdir()) / f"MicrosoftEdgeWebview2Setup-{uuid.uuid4().hex}.exe"
        request = urllib.request.Request(WEBVIEW2_INSTALLER_URL, headers={"User-Agent": "Station Max Studio"})
        context = ssl.create_default_context(cafile=certifi.where())
        try:
            with urllib.request.urlopen(request, timeout=60, context=context) as response:
                with installer.open("wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
            process = subprocess.run(
                [str(installer), "/silent", "/install"],
                timeout=6 * 60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if process.returncode not in (0, 3010):
                raise RuntimeError(f"Установщик WebView2 завершился с кодом {process.returncode}")
            for _ in range(15):
                version = webview2_runtime_version()
                if version:
                    return version
                time.sleep(1)
            raise RuntimeError("WebView2 установлен, но Windows пока его не обнаруживает. Перезапустите приложение")
        finally:
            try:
                installer.unlink(missing_ok=True)
            except OSError:
                pass

    def webview2_installed(self, version):
        messagebox.showinfo(
            "Microsoft WebView2",
            f"Компонент установлен: {version}\nСейчас откроется вход в Яндекс.",
            parent=self,
        )
        self.start_embedded_login()

    def qr_login_action(self):
        def status(text):
            self.after(0, lambda: self.status_var.set(text))

        self.run_job(lambda: self._login_job(status), self.login_finished, "Открываю Яндекс…")

    def _login_job(self, status):
        token = qr_login(webbrowser.open, status)
        return token, list_stations(token)

    def cookies_login(self):
        CookiesLoginDialog(self, self.start_cookie_login)

    def start_cookie_login(self, value):
        self.run_job(
            lambda: self._cookies_login_job(value),
            self.login_finished,
            "Проверяю cookies и загружаю список колонок…",
        )

    def _cookies_login_job(self, value):
        token = token_from_cookies(value)
        return token, list_stations(token)

    def import_ha(self):
        folder = filedialog.askdirectory(title="Выберите config Home Assistant или корень HassWP", parent=self)
        if folder:
            self.run_job(lambda: self._import_job(folder), self.login_finished, "Читаю настройки Home Assistant…")

    def _import_job(self, folder):
        token = read_home_assistant_token(folder)
        return token, list_stations(token)

    def import_settings(self):
        path = filedialog.askopenfilename(
            title="Выберите settings.json с работающего ПК",
            parent=self,
            filetypes=(("Station Max Studio settings", "settings.json"), ("JSON", "*.json"), ("Все файлы", "*.*")),
        )
        if path:
            self.run_job(
                lambda: self._import_settings_job(path),
                self.settings_import_finished,
                "Проверяю настройки и загружаю список колонок…",
            )

    def _import_settings_job(self, path):
        try:
            imported = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("Не удалось прочитать settings.json") from exc
        if not isinstance(imported, dict) or not imported.get("x_token"):
            raise RuntimeError("В settings.json нет сохранённой авторизации x_token")
        token = str(imported["x_token"]).strip()
        return token, list_stations(token), imported

    def settings_import_finished(self, result):
        token, stations, imported = result
        for key in ("station_name", "device_id", "platform", "http_port"):
            if imported.get(key):
                self.settings[key] = imported[key]
        # На другом ПК или Wi-Fi старый IP обычно неверен — ищем колонку заново.
        self.settings["station_ip"] = ""
        self.ip_var.set("")
        self.port_var.set(str(self.settings.get("http_port", 8765)))
        self.login_finished((token, stations))
        self.status_var.set("Настройки импортированы. Нажмите «Найти в сети» для определения IP")

    def login_finished(self, result):
        token, stations = result
        self.settings["x_token"] = token
        self.account_label.configure(text="Авторизация сохранена", text_color=COLORS["success"])
        self.populate_stations(stations)

    def refresh_stations(self):
        token = self.settings.get("x_token")
        if not token:
            messagebox.showinfo("Station Max Studio", "Сначала войдите в Яндекс", parent=self)
            return
        self.run_job(lambda: list_stations(token), self.populate_stations, "Загружаю список колонок…")

    def populate_stations(self, stations):
        values = []
        self.station_map = {}
        for station in stations:
            house = f" • {station['house']}" if station.get("house") else ""
            label = f"{station['name']}{house} • {station['platform']}"
            values.append(label)
            self.station_map[label] = station
        self.station_menu.configure(values=values or ["Колонки не найдены"])
        current = next((label for label, item in self.station_map.items() if item["device_id"] == self.settings.get("device_id")), "")
        if not current and len(values) == 1:
            current = values[0]
        if current:
            self.station_var.set(current)
            self.station_selected(current)
        self.status_var.set(f"Найдено колонок: {len(stations)}")

    def station_selected(self, label):
        station = self.station_map.get(label)
        if station:
            self.settings.update(station_name=station["name"], device_id=station["device_id"], platform=station["platform"])

    def find_ip(self):
        self.station_selected(self.station_var.get())
        if not all(self.settings.get(key) for key in ("x_token", "device_id", "platform")):
            messagebox.showinfo("Station Max Studio", "Сначала выберите колонку", parent=self)
            return

        def progress(text):
            self.after(0, lambda: self.status_var.set(text))

        self.run_job(
            lambda: find_station_ip(self.settings["x_token"], self.settings["device_id"], self.settings["platform"], self.ip_var.get().strip(), progress),
            self.ip_found,
            "Ищу колонку…",
        )

    def ip_found(self, address):
        self.ip_var.set(address)
        self.status_var.set(f"Колонка найдена: {address}")

    def save(self):
        self.station_selected(self.station_var.get())
        try:
            port = int(self.port_var.get())
            if not 1024 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror("Station Max Studio", "Порт должен быть числом от 1024 до 65535", parent=self)
            return
        if not all(self.settings.get(key) for key in ("x_token", "device_id", "platform")):
            messagebox.showerror("Station Max Studio", "Войдите в Яндекс и выберите колонку", parent=self)
            return
        if not self.ip_var.get().strip():
            messagebox.showerror("Station Max Studio", "Найдите колонку или укажите IP", parent=self)
            return
        self.settings["station_ip"] = self.ip_var.get().strip()
        self.settings["http_port"] = port
        self.settings.pop("show_logo_on_connect", None)
        self.owner.apply_connection(self.settings)
        self.destroy()


class StationMaxStudio(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION} — by SheEgAn")
        self.geometry("1350x820")
        self.minsize(1120, 700)
        self.configure(fg_color=COLORS["window"])
        self.connection = load_settings()
        self.connection.pop("show_logo_on_connect", None)
        if not self.connection.get("x_token"):
            imported = find_home_assistant_token()
            if imported:
                self.connection["x_token"] = imported
                save_settings(self.connection)
        self.media_server = MediaServer(READY_DIR, int(self.connection.get("http_port", 8765)))
        self.connection["http_port"] = self.media_server.port
        save_settings(self.connection)
        self.busy = False
        self.nav_buttons = {}
        self.pages = {}
        self.current_page = "screen"

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_sidebar()
        self._build_header()
        self._build_content()
        self._build_statusbar()
        self.show_page("screen")
        self.protocol("WM_DELETE_WINDOW", self.close_app)

        if os.environ.get("STATION_MAX_STUDIO_SMOKE") == "1":
            self.withdraw()
            self.after(1000, self.close_app)
        elif os.environ.get("STATION_MAX_STUDIO_UI_TEST") != "1":
            if not all(self.connection.get(key) for key in ("x_token", "device_id", "platform", "station_ip")):
                self.after(450, self.open_settings)
            else:
                self.after(900, lambda: self.check_connection(show_logo=True))

    def _logo_image(self, size=(56, 56)):
        if not LOGO_SOURCE.exists():
            return None
        with Image.open(LOGO_SOURCE) as image:
            rgba = image.convert("RGBA")
            gray = ImageOps.invert(rgba.convert("L"))
            alpha = gray.point(lambda value: value)
            colored = Image.new("RGBA", rgba.size, (147, 51, 234, 255))
            colored.putalpha(alpha)
            return ctk.CTkImage(light_image=colored, dark_image=colored, size=size)

    def _build_sidebar(self):
        sidebar = ctk.CTkFrame(self, width=220, fg_color=COLORS["sidebar"], corner_radius=0)
        sidebar.grid(row=0, column=0, rowspan=3, sticky="nsew")
        sidebar.grid_propagate(False)
        brand = ctk.CTkFrame(sidebar, fg_color="transparent")
        brand.pack(fill="x", padx=18, pady=(22, 25))
        logo = self._logo_image((48, 48))
        self.sidebar_logo = logo
        ctk.CTkLabel(brand, image=logo, text="" if logo else "◉").pack(side="left")
        title = ctk.CTkFrame(brand, fg_color="transparent")
        title.pack(side="left", padx=(10, 0))
        ctk.CTkLabel(title, text="Station Max", font=ctk.CTkFont("Segoe UI", 17, "bold"), anchor="w").pack(anchor="w")
        ctk.CTkLabel(title, text="by SheEgAn", font=ctk.CTkFont("Segoe UI", 10), text_color="#bfa4ff", anchor="w").pack(anchor="w")

        items = [
            ("screen", "▣", "Экран"),
            ("drawing", "✎", "Рисование"),
            ("video", "▶", "Видео"),
            ("library", "▤", "Готовые файлы"),
            ("history", "◷", "История"),
        ]
        for key, icon, label in items:
            button = ctk.CTkButton(
                sidebar,
                text=f"{icon}   {label}",
                height=48,
                corner_radius=11,
                anchor="w",
                fg_color="transparent",
                hover_color="#242a40",
                text_color="#c7cce0",
                font=ctk.CTkFont("Segoe UI", 14),
                command=lambda page=key: self.show_page(page),
            )
            button.pack(fill="x", padx=12, pady=3)
            self.nav_buttons[key] = button

        ctk.CTkButton(
            sidebar,
            text="⚙   Настройки",
            height=46,
            anchor="w",
            fg_color="transparent",
            hover_color="#242a40",
            text_color="#c7cce0",
            command=self.open_settings,
        ).pack(side="bottom", fill="x", padx=12, pady=(0, 18))

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color="transparent", height=94)
        header.grid(row=0, column=1, sticky="ew", padx=24, pady=(15, 8))
        header.grid_columnconfigure(0, weight=1)
        title = ctk.CTkFrame(header, fg_color="transparent")
        title.grid(row=0, column=0, sticky="w")
        self.page_title = ctk.CTkLabel(title, text="Экран Станции Макс", font=ctk.CTkFont("Segoe UI", 27, "bold"), anchor="w")
        self.page_title.pack(anchor="w")
        ctk.CTkLabel(title, text="Фото, видео и рисунки для матрицы 25×16", text_color=COLORS["muted"], font=ctk.CTkFont("Segoe UI", 13), anchor="w").pack(anchor="w", pady=(2, 0))

        self.connection_pill = ctk.CTkFrame(header, fg_color=COLORS["panel"], corner_radius=14, border_width=1, border_color=COLORS["border"])
        self.connection_pill.grid(row=0, column=1, padx=(12, 10), sticky="e")
        self.connection_dot = ctk.CTkLabel(self.connection_pill, text="●", text_color=COLORS["muted"], font=ctk.CTkFont("Segoe UI", 13))
        self.connection_dot.pack(side="left", padx=(14, 7), pady=10)
        pill_text = ctk.CTkFrame(self.connection_pill, fg_color="transparent")
        pill_text.pack(side="left", padx=(0, 16), pady=8)
        self.connection_name = ctk.CTkLabel(pill_text, text="Проверка…", font=ctk.CTkFont("Segoe UI", 12, "bold"), anchor="w")
        self.connection_name.pack(anchor="w")
        self.connection_ip = ctk.CTkLabel(pill_text, text=self.connection.get("station_ip") or "не настроено", text_color=COLORS["muted"], font=ctk.CTkFont("Segoe UI", 10), anchor="w")
        self.connection_ip.pack(anchor="w")

    def _build_content(self):
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=1, column=1, sticky="nsew", padx=24, pady=(0, 8))
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(0, weight=1)

        self.image_editor = MediaEditor(content, self, video=False)
        self.video_editor = MediaEditor(content, self, video=True)
        self.drawing_page = DrawingPage(content, self)
        self.library_page = LibraryPage(content, self)
        self.history_page = HistoryPage(content)
        self.pages = {
            "screen": self.image_editor,
            "video": self.video_editor,
            "drawing": self.drawing_page,
            "library": self.library_page,
            "history": self.history_page,
        }
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")

    def _build_statusbar(self):
        bar = ctk.CTkFrame(self, fg_color="transparent", height=34)
        bar.grid(row=2, column=1, sticky="ew", padx=26, pady=(0, 10))
        self.status_bar = bar
        self.status_var = tk.StringVar(value="")
        self.status_label = ctk.CTkLabel(bar, textvariable=self.status_var, text_color=COLORS["muted"], anchor="w")
        self.progress = ctk.CTkProgressBar(bar, mode="indeterminate", width=170, progress_color=COLORS["accent"])
        self.progress.set(0)
        self.status_bar.grid_remove()

    def show_page(self, key):
        self.current_page = key
        titles = {"screen": "Экран Станции Макс", "drawing": "Рисование", "video": "Видео", "library": "Готовые файлы", "history": "История отправок"}
        self.page_title.configure(text=titles[key])
        self.pages[key].tkraise()
        for name, button in self.nav_buttons.items():
            selected = name == key
            button.configure(
                fg_color="#342350" if selected else "transparent",
                text_color="white" if selected else "#c7cce0",
                border_width=1 if selected else 0,
                border_color="#7542c8" if selected else COLORS["sidebar"],
            )
        if key == "library":
            self.library_page.refresh()
        elif key == "history":
            self.history_page.refresh()

    def set_status(self, text):
        self.status_var.set(text)

    def show_activity(self, text):
        self.status_var.set(text)
        self.status_bar.grid()
        self.status_label.pack(side="left")
        self.progress.pack(side="right")
        self.progress.start()

    def hide_activity(self):
        self.progress.stop()
        self.progress.set(0)
        self.progress.pack_forget()
        self.status_label.pack_forget()
        self.status_var.set("")
        self.status_bar.grid_remove()

    def run_job(self, job, done=None, status="Работаю…"):
        if self.busy:
            messagebox.showinfo("Station Max Studio", "Дождитесь завершения текущей операции")
            return
        self.busy = True
        self.show_activity(status)

        def worker():
            try:
                result = job()
            except Exception as exc:
                details = traceback.format_exc()
                self.after(0, lambda error=exc, trace=details: self.job_failed(error, trace))
                return
            self.after(0, lambda: self.job_done(result, done))

        threading.Thread(target=worker, daemon=True).start()

    def job_done(self, result, done):
        self.busy = False
        self.hide_activity()
        if done:
            done(result)

    def job_failed(self, error, details):
        self.busy = False
        self.hide_activity()
        log = DATA_DIR / "last-error.log"
        log.write_text(details, encoding="utf-8")
        messagebox.showerror("Station Max Studio", f"{error}\n\nПодробности сохранены:\n{log}")

    def refresh_library_views(self):
        self.library_page.refresh()
        self.history_page.refresh()
        self.image_editor.refresh_recent()
        self.video_editor.refresh_recent()

    def connection_state(self, connected, text=None):
        self.connection_dot.configure(text_color=COLORS["success"] if connected else COLORS["danger"])
        self.connection_name.configure(text=text or ("Подключена" if connected else "Нет подключения"))
        self.connection_ip.configure(text=self.connection.get("station_ip") or "не настроено")

    def open_settings(self):
        ConnectionDialog(self)

    def apply_connection(self, settings):
        previous_port = self.media_server.port
        self.connection = dict(settings)
        requested_port = int(self.connection.get("http_port", 8765))
        if requested_port != previous_port:
            self.media_server.close()
            self.media_server = MediaServer(READY_DIR, requested_port)
            self.connection["http_port"] = self.media_server.port
        save_settings(self.connection)
        self.connection_ip.configure(text=self.connection.get("station_ip", ""))
        self.after(200, lambda: self.check_connection(show_logo=True))

    def ensure_connection_logo(self) -> Path:
        if not LOGO_SOURCE.exists():
            raise RuntimeError("Не найден логотип SheEgAn внутри приложения")
        destination = READY_DIR / "SheEgAn-connect-max-v2.gif"
        if destination.exists() and destination.stat().st_size > 250_000:
            return destination
        with Image.open(LOGO_SOURCE) as logo:
            # Keep the white negative-space version from the original file:
            # on the 25x16 matrix this becomes the outlined logo shown in the
            # user's reference photo.
            options = editor_defaults()
            options.update(fit_mode="contain", contrast=1.35, zoom=1.0)
            frame = transform_frame(logo, options)
        frames, durations = static_frames(frame, MAX_STATIC_SECONDS)
        save_gif(frames, durations, destination)
        return destination

    def check_connection(self, show_logo=False):
        if not all(self.connection.get(key) for key in ("x_token", "device_id", "platform", "station_ip")):
            self.connection_state(False, "Не настроена")
            return

        def job():
            address = find_station_ip(
                self.connection["x_token"],
                self.connection["device_id"],
                self.connection["platform"],
                self.connection.get("station_ip", ""),
            )
            self.connection["station_ip"] = address
            save_settings(self.connection)
            if show_logo:
                logo = self.ensure_connection_logo()
                self.media_server.self_test(logo)
                send_gif(
                    address,
                    self.connection["x_token"],
                    self.connection["device_id"],
                    self.connection["platform"],
                    self.media_server.url_for(logo, address),
                )
            return address

        self.run_job(job, lambda address: self.connected(address, show_logo), "Подключаюсь к Станции…")

    def connected(self, address, logo_sent):
        self.connection["station_ip"] = address
        self.connection_state(True, "Подключена")

    def send_path(self, path: Path):
        if not all(self.connection.get(key) for key in ("x_token", "device_id", "platform", "station_ip")):
            raise RuntimeError("Сначала настройте подключение к колонке")
        self.media_server.self_test(path)
        address = self.connection["station_ip"]
        url = self.media_server.url_for(path, address)
        try:
            send_gif(address, self.connection["x_token"], self.connection["device_id"], self.connection["platform"], url)
        except Exception:
            address = find_station_ip(self.connection["x_token"], self.connection["device_id"], self.connection["platform"], address)
            self.connection["station_ip"] = address
            save_settings(self.connection)
            url = self.media_server.url_for(path, address)
            send_gif(address, self.connection["x_token"], self.connection["device_id"], self.connection["platform"], url)
        return path

    def close_app(self):
        try:
            self.media_server.close()
        finally:
            self.destroy()


def packaged_network_diagnostic():
    destination = Path(os.environ.get("TEMP", APP_DIR)) / "station-max-studio-network-test.txt"
    try:
        settings = load_settings()
        stations = list_stations(settings.get("x_token", ""))
        selected = next((item for item in stations if item["device_id"] == settings.get("device_id")), stations[0])
        address = find_station_ip(settings["x_token"], selected["device_id"], selected["platform"], settings.get("station_ip", ""))
        report = f"OK\nstations={len(stations)}\nselected={selected['name']}\nip={address}\n"
    except Exception:
        report = "ERROR\n" + traceback.format_exc()
    destination.write_text(report, encoding="utf-8")


def packaged_media_diagnostic():
    destination = Path(os.environ.get("TEMP", APP_DIR)) / "station-max-studio-media-test.txt"
    try:
        source = Path(os.environ["STATION_MAX_STUDIO_MEDIA_SOURCE"])
        preview = first_frame(source)
        output = convert_video(source, editor_defaults(), fps=8, max_seconds=2)
        with Image.open(output) as animation:
            report = (
                f"OK\npreview={preview.width}x{preview.height}\n"
                f"gif={animation.width}x{animation.height}\n"
                f"frames={getattr(animation, 'n_frames', 1)}\noutput={output}\n"
            )
    except Exception:
        report = "ERROR\n" + traceback.format_exc()
    destination.write_text(report, encoding="utf-8")


def packaged_logo_diagnostic():
    destination = Path(os.environ.get("TEMP", APP_DIR)) / "station-max-studio-logo-test.txt"
    try:
        output = StationMaxStudio.ensure_connection_logo(None)
        with Image.open(output) as animation:
            report = (
                f"OK\ngif={animation.width}x{animation.height}\n"
                f"frames={getattr(animation, 'n_frames', 1)}\n"
                f"bytes={output.stat().st_size}\noutput={output}\n"
            )
    except Exception:
        report = "ERROR\n" + traceback.format_exc()
    destination.write_text(report, encoding="utf-8")


def embedded_yandex_login():
    """Run the interactive Yandex login in a dedicated WebView2 child process."""
    result_path = Path(os.environ[WEB_LOGIN_RESULT_ENV])
    result_lock = threading.Lock()
    state = {"written": False}

    def write_result(payload):
        with result_lock:
            if state["written"]:
                return False
            result_path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            state["written"] = True
            return True

    version = webview2_runtime_version()
    if not version:
        write_result(
            {
                "status": "error",
                "code": "webview2_missing",
                "message": "Microsoft Edge WebView2 Runtime не установлен",
            }
        )
        return

    try:
        import webview
    except Exception as exc:
        write_result(
            {
                "status": "error",
                "code": "webview_unavailable",
                "message": f"Компонент встроенного браузера не загрузился: {exc}",
            }
        )
        return

    window = webview.create_window(
        "Вход в Яндекс — Station Max Studio",
        "https://passport.yandex.ru/profile",
        width=960,
        height=760,
        min_size=(720, 560),
        resizable=True,
        text_select=True,
        zoomable=True,
        background_color="#0a0d18",
    )

    def monitor_login():
        deadline = time.monotonic() + 11 * 60
        while not state["written"] and time.monotonic() < deadline:
            try:
                records = _cookies_to_records(window.get_cookies())
                names = {item["name"].lower() for item in records}
                if {"session_id", "sessionid2"}.intersection(names):
                    token = token_from_cookies(json.dumps(records, ensure_ascii=False))
                    if write_result({"status": "success", "token": token}):
                        window.destroy()
                    return
            except Exception:
                # During redirects cookies can be temporarily incomplete. The next
                # pass retries after Yandex finishes the current navigation.
                pass
            time.sleep(1.25)

        if not state["written"]:
            write_result(
                {
                    "status": "error",
                    "code": "timeout",
                    "message": "Время входа истекло. Откройте окно входа ещё раз",
                }
            )
            try:
                window.destroy()
            except Exception:
                pass

    def closed():
        write_result(
            {
                "status": "error",
                "code": "cancelled",
                "message": "Вход отменён пользователем",
            }
        )

    window.events.closed += closed
    try:
        webview.start(
            monitor_login,
            gui="edgechromium",
            debug=False,
            private_mode=True,
        )
    except Exception as exc:
        message = str(exc)
        code = "webview2_missing" if "webview2" in message.lower() else "webview_error"
        write_result({"status": "error", "code": code, "message": message})
    if not state["written"]:
        write_result(
            {
                "status": "error",
                "code": "cancelled",
                "message": "Окно входа было закрыто",
            }
        )


def packaged_webview_diagnostic():
    destination = Path(os.environ.get("TEMP", APP_DIR)) / "station-max-studio-webview-test.txt"
    try:
        import webview

        version = webview2_runtime_version()
        if not version:
            raise RuntimeError("WebView2 Runtime not found")
        window = webview.create_window(
            "Station Max Studio WebView2 test",
            html="<html><body>OK</body></html>",
            hidden=True,
        )
        loaded = threading.Event()
        window.events.loaded += loaded.set

        def close_test():
            if not loaded.wait(15):
                raise TimeoutError("WebView2 page did not finish loading")
            time.sleep(0.4)
            window.destroy()

        webview.start(close_test, gui="edgechromium", debug=False, private_mode=True)
        report = f"OK\nwebview2={version}\n"
    except Exception:
        report = "ERROR\n" + traceback.format_exc()
    destination.write_text(report, encoding="utf-8")


if __name__ == "__main__":
    if os.environ.get(WEB_LOGIN_RESULT_ENV):
        embedded_yandex_login()
    elif os.environ.get("STATION_MAX_STUDIO_WEBVIEW_TEST") == "1":
        packaged_webview_diagnostic()
    elif os.environ.get("STATION_MAX_STUDIO_NETWORK_TEST") == "1":
        packaged_network_diagnostic()
    elif os.environ.get("STATION_MAX_STUDIO_MEDIA_TEST") == "1":
        packaged_media_diagnostic()
    elif os.environ.get("STATION_MAX_STUDIO_LOGO_TEST") == "1":
        packaged_logo_diagnostic()
    else:
        app = StationMaxStudio()
        app.mainloop()
