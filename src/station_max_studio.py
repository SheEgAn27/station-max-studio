from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageEnhance, ImageOps

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

from station_network import (
    MediaServer,
    find_home_assistant_token,
    find_station_ip,
    list_stations,
    load_settings,
    qr_login,
    read_home_assistant_token,
    save_settings,
    send_gif,
)


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

APP_NAME = "Station Max Studio"
APP_VERSION = "2.1.3"

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
            self._row_…7871 tokens truncated…: list_stations(token), self.populate_stations, "Загружаю список колонок…")

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


if __name__ == "__main__":
    if os.environ.get("STATION_MAX_STUDIO_NETWORK_TEST") == "1":
        packaged_network_diagnostic()
    elif os.environ.get("STATION_MAX_STUDIO_MEDIA_TEST") == "1":
        packaged_media_diagnostic()
    elif os.environ.get("STATION_MAX_STUDIO_LOGO_TEST") == "1":
        packaged_logo_diagnostic()
    else:
        app = StationMaxStudio()
        app.mainloop()
