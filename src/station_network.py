"""Standalone Yandex Station Max connection helpers.

This module deliberately uses only the Python standard library.  It handles
Yandex QR authentication, device discovery, the local Glagol WebSocket and a
small HTTP server used by the Station to download generated GIF files.
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import http.cookiejar
import http.server
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from functools import partial
from pathlib import Path

import certifi


APP_NAME = "Station Max Studio"
SETTINGS_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
SETTINGS_PATH = SETTINGS_DIR / "settings.json"
DEFAULT_SETTINGS = {
    "x_token": "",
    "station_name": "",
    "device_id": "",
    "platform": "",
    "station_ip": "",
    "http_port": 8765,
}

PASSPORT_URL = "https://passport.yandex.ru"
PASSPORT_URLS = (PASSPORT_URL, "https://passport.yandex.kz")
MOBILE_PROXY = "https://mobileproxy.passport.yandex.net"
DEVICE_LIST_URL = "https://iot.quasar.yandex.ru/m/v3/user/devices"
GLAGOL_TOKEN_URL = "https://quasar.yandex.net/glagol/token"
MUSIC_TOKEN_URL = "https://oauth.mobile.yandex.net/1/token"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/128.0 Safari/537.36"
)


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    try:
        loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            settings.update(loaded)
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings: dict) -> None:
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(DEFAULT_SETTINGS)
    data.update(settings)
    temp = SETTINGS_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(SETTINGS_PATH)


def read_home_assistant_token(config_root: str | Path) -> str:
    selected = Path(config_root)
    candidates = []
    if selected.is_file():
        candidates.append(selected)
    else:
        candidates.extend(
            [
                selected / ".storage" / "core.config_entries",
                selected / "core.config_entries",
                selected / "config" / ".storage" / "core.config_entries",
                selected / "home-assistant" / ".storage" / "core.config_entries",
            ]
        )
        candidates.extend(selected.glob("HassWP*/config/.storage/core.config_entries"))
        candidates.extend(selected.glob("*/config/.storage/core.config_entries"))
    storage = next((item for item in candidates if item.is_file()), None)
    if storage is None:
        raise RuntimeError(
            "Не найден файл .storage\\core.config_entries. Выберите папку config "
            "Home Assistant или корневую папку HassWP."
        )
    try:
        payload = json.loads(storage.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Не удалось прочитать настройки Home Assistant: {storage}") from exc
    for entry in payload.get("data", {}).get("entries", []):
        if entry.get("domain") == "yandex_station":
            token = entry.get("data", {}).get("x_token", "")
            if token:
                return token
    raise RuntimeError("В выбранной папке не найдена настроенная интеграция Yandex Station")


def find_home_assistant_token() -> str:
    """Best-effort migration from common HassWP locations."""
    candidates: list[Path] = []
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    temp = Path(os.environ.get("TEMP", ""))
    home = Path.home()
    candidates.extend(
        [
            local / "HassWP" / "config",
            local / "Programs" / "HassWP" / "config",
            home / "HassWP" / "config",
            Path("C:/HassWP/config"),
        ]
    )
    if temp.is_dir():
        candidates.extend(temp.glob("*/HassWP*/config"))
        candidates.extend(temp.glob("*/*/HassWP*/config"))

    seen: set[str] = set()
    for root in candidates:
        key = str(root).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            return read_home_assistant_token(root)
        except (OSError, ValueError, RuntimeError):
            continue
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _https_context() -> ssl.SSLContext:
    """Create a TLS context even if Windows has stale SSL_* environment paths."""
    invalid_override = any(
        value and not Path(value).exists()
        for value in (
            os.environ.get("SSL_CERT_FILE"),
            os.environ.get("SSL_CERT_DIR"),
        )
    )
    if invalid_override:
        return ssl.create_default_context(cafile=certifi.where())
    try:
        return ssl.create_default_context()
    except FileNotFoundError:
        return ssl.create_default_context(cafile=certifi.where())


def _https_opener(*handlers):
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_https_context()),
        *handlers,
    )


class YandexSession:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = _https_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.csrf = ""
        self.auth_json: dict | None = None
        self.passport_url = PASSPORT_URL

    def _open(self, request: urllib.request.Request, timeout: float = 25):
        request.add_header("User-Agent", USER_AGENT)
        try:
            return self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace")
            raise RuntimeError(f"Яндекс вернул HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Не удалось подключиться к Яндексу: {exc.reason}") from exc

    def _json_request(
        self,
        url: str,
        *,
        method: str = "GET",
        json_data: dict | None = None,
        form: dict | None = None,
        headers: dict | None = None,
    ) -> dict:
        data = None
        request_headers = dict(headers or {})
        if json_data is not None:
            data = json.dumps(json_data).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(
            url, data=data, headers=request_headers, method=method
        )
        with self._open(request) as response:
            raw = response.read()
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise RuntimeError("Яндекс вернул неожиданный ответ") from exc

    def begin_qr_login(self) -> str:
        failures: list[str] = []
        for passport_url in PASSPORT_URLS:
            for attempt in range(2):
                suffix = "" if attempt == 0 else f"?nocache={int(time.time() * 1000)}"
                request = urllib.request.Request(
                    f"{passport_url}/pwl-yandex{suffix}",
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
                        "Cache-Control": "no-cache",
                        "Pragma": "no-cache",
                    },
                )
                try:
                    with self._open(request) as response:
                        html = response.read().decode("utf-8", "replace")
                except RuntimeError as exc:
                    failures.append(f"{urllib.parse.urlparse(passport_url).hostname}: {exc}")
                    break

                match = re.search(
                    r"__CSRF__\s*=\s*['\"]([^'\"]+)['\"]",
                    html,
                    re.IGNORECASE,
                )
                if match:
                    self.passport_url = passport_url
                    self.csrf = match.group(1)
                    break

                title_match = re.search(
                    r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL
                )
                title = (
                    re.sub(r"\s+", " ", title_match.group(1)).strip()
                    if title_match
                    else "без заголовка"
                )
                failures.append(
                    f"{urllib.parse.urlparse(passport_url).hostname}: страница «{title}» без CSRF"
                )
                time.sleep(0.35)

            if self.csrf:
                break

        if not self.csrf:
            detail = "; ".join(failures[-4:])
            raise RuntimeError(
                "Не удалось начать QR-авторизацию: Яндекс не вернул CSRF. "
                "Отключите VPN/прокси или выберите другой сервер VPN и повторите вход. "
                f"Ответы: {detail}"
            )

        headers = {"X-CSRF-Token": self.csrf}
        self.auth_json = self._json_request(
            f"{self.passport_url}/pwl-yandex/api/passport/auth/password/submit",
            method="POST",
            json_data={"retpath": f"{self.passport_url}/"},
            headers=headers,
        )
        track_id = self.auth_json.get("track_id", "")
        data = self._json_request(
            f"{self.passport_url}/pwl-yandex/api/passport/auth/magic/code",
            method="POST",
            form={"location_id": "0", "magic_track_id": track_id, "track_id": ""},
            headers=headers,
        )
        link = data.get("link", "")
        if not link:
            raise RuntimeError("Яндекс не вернул ссылку для QR-авторизации")
        return link

    def finish_qr_login(self, timeout: float = 180, poll_interval: float = 2) -> str:
        if not self.auth_json or not self.csrf:
            raise RuntimeError("QR-авторизация не была начата")
        deadline = time.monotonic() + timeout
        headers = {"X-CSRF-Token": self.csrf}
        while time.monotonic() < deadline:
            data = self._json_request(
                f"{self.passport_url}/pwl-yandex/api/passport/auth/magic/code/status",
                method="POST",
                json_data=self.auth_json,
                headers=headers,
            )
            if data.get("state") == "otp_auth_finished":
                track_id = data.get("trackId", "")
                request = urllib.request.Request(
                    f"{self.passport_url}/pwl-yandex/api/passport/sessions/get_session",
                    data=urllib.parse.urlencode({"track_id": track_id}).encode("utf-8"),
                    headers={
                        "X-CSRF-Token": self.csrf,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    method="POST",
                )
                with self._open(request) as response:
                    response.read()
                return self._exchange_cookies_for_token()
            time.sleep(poll_interval)
        raise TimeoutError("Время подтверждения истекло. Запустите вход ещё раз")

    def _exchange_cookies_for_token(self) -> str:
        cookie_header = "; ".join(f"{item.name}={item.value}" for item in self.jar)
        data = self._json_request(
            f"{MOBILE_PROXY}/1/bundle/oauth/token_by_sessionid",
            method="POST",
            form={
                "client_id": "c0ebe342af7d48fbbbfcf2d2eedb8f9e",
                "client_secret": "ad0a908f0aa341a182a37ecd75bc319e",
            },
            headers={
                "Ya-Client-Host": urllib.parse.urlparse(self.passport_url).hostname,
                "Ya-Client-Cookie": cookie_header,
            },
        )
        token = data.get("access_token", "")
        if not token:
            raise RuntimeError("Яндекс не выдал токен после подтверждения входа")
        return token


def qr_login(open_link, status_callback=None) -> str:
    session = YandexSession()
    link = session.begin_qr_login()
    if status_callback:
        status_callback("Открыл страницу Яндекса — подтвердите вход")
    open_link(link)
    return session.finish_qr_login()


def _session_from_token(x_token: str):
    if not x_token:
        raise RuntimeError("Сначала войдите в аккаунт Яндекса")
    jar = http.cookiejar.CookieJar()
    opener = _https_opener(urllib.request.HTTPCookieProcessor(jar))
    form = urllib.parse.urlencode(
        {"type": "x-token", "retpath": "https://www.yandex.ru"}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{MOBILE_PROXY}/1/bundle/auth/x_token/",
        data=form,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Ya-Consumer-Authorization": f"OAuth {x_token}",
        },
        method="POST",
    )
    try:
        with opener.open(request, timeout=25) as response:
            payload = json.load(response)
    except Exception as exc:
        raise RuntimeError(f"Не удалось проверить вход в Яндекс: {exc}") from exc
    if payload.get("status") != "ok":
        raise RuntimeError("Сохранённая авторизация Яндекса больше не действует")
    host = payload.get("passport_host", PASSPORT_URL).rstrip("/")
    track = urllib.parse.quote(payload.get("track_id", ""))
    request = urllib.request.Request(
        f"{host}/auth/session/?track_id={track}",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with opener.open(request, timeout=25) as response:
            response.read(1024)
    except urllib.error.HTTPError as exc:
        # The endpoint may stop on a redirect; cookies are already in the jar.
        if exc.code not in (301, 302, 303, 307, 308):
            raise
    except Exception as exc:
        raise RuntimeError(f"Не удалось создать сессию Яндекса: {exc}") from exc
    return opener


def list_stations(x_token: str) -> list[dict]:
    opener = _session_from_token(x_token)
    request = urllib.request.Request(
        DEVICE_LIST_URL, headers={"User-Agent": USER_AGENT}
    )
    try:
        with opener.open(request, timeout=25) as response:
            data = json.load(response)
    except Exception as exc:
        raise RuntimeError(f"Не удалось получить список колонок: {exc}") from exc
    if data.get("status") != "ok":
        raise RuntimeError("Яндекс не вернул список устройств")

    stations = []
    excluded = {"saturn", "mike", "cherry"}
    for household in data.get("households", []):
        for device in household.get("all", []):
            info = device.get("quasar_info") or {}
            device_id = info.get("device_id", "")
            platform = info.get("platform", "")
            device_type = device.get("type", "")
            if (
                not device_id
                or platform in excluded
                or not device.get("capabilities")
                or not device_type.startswith("devices.types.smart_speaker")
            ):
                continue
            if device.get("sharing_info"):
                continue
            stations.append(
                {
                    "name": device.get("name") or "Яндекс Станция",
                    "house": household.get("name", ""),
                    "device_id": device_id,
                    "platform": platform,
                    "iot_id": device.get("id", ""),
                    "type": device_type,
                }
            )
    if not stations:
        raise RuntimeError("В аккаунте Яндекса не найдено ни одной Станции")
    return stations


def get_music_token(x_token: str) -> str:
    form = urllib.parse.urlencode(
        {
            "client_secret": "53bc75238f0c4d08a118e51fe9203300",
            "client_id": "23cabbbdc6cd418abb4b39c32c41195d",
            "grant_type": "x-token",
            "access_token": x_token,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        MUSIC_TOKEN_URL,
        data=form,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=20, context=_https_context()
        ) as response:
            data = json.load(response)
    except Exception as exc:
        raise RuntimeError(f"Не удалось получить токен Яндекс Музыки: {exc}") from exc
    token = data.get("access_token", "")
    if not token:
        raise RuntimeError("Яндекс не выдал токен, необходимый для локального управления")
    return token


def get_glagol_token(x_token: str, device_id: str, platform: str) -> str:
    music_token = get_music_token(x_token)
    query = urllib.parse.urlencode({"device_id": device_id, "platform": platform})
    request = urllib.request.Request(
        f"{GLAGOL_TOKEN_URL}?{query}",
        headers={"Authorization": f"OAuth {music_token}", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=20, context=_https_context()
        ) as response:
            data = json.load(response)
    except Exception as exc:
        raise RuntimeError(f"Не удалось получить локальный токен колонки: {exc}") from exc
    token = data.get("token", "")
    if data.get("status") != "ok" or not token:
        raise RuntimeError("Яндекс не выдал локальный токен для выбранной колонки")
    return token


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    parts = bytearray()
    while len(parts) < length:
        chunk = sock.recv(length - len(parts))
        if not chunk:
            raise ConnectionError("Колонка закрыла соединение")
        parts.extend(chunk)
    return bytes(parts)


class GlagolSocket:
    def __init__(self, host: str, port: int = 1961, timeout: float = 8):
        self.host = host
        raw = socket.create_connection((host, port), timeout=timeout)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.sock = context.wrap_socket(raw, server_hostname=host)
        self.sock.settimeout(timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self.sock.sendall(request)
        headers = bytearray()
        while b"\r\n\r\n" not in headers and len(headers) < 32768:
            headers.extend(self.sock.recv(4096))
        first_line = bytes(headers).split(b"\r\n", 1)[0]
        if b" 101 " not in first_line:
            self.sock.close()
            raise ConnectionError(f"Колонка отклонила WebSocket: {first_line!r}")

    def close(self):
        try:
            self._send_frame(b"", opcode=8)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass

    def _send_frame(self, payload: bytes, opcode: int = 1):
        mask = secrets.token_bytes(4)
        size = len(payload)
        header = bytearray([0x80 | opcode])
        if size < 126:
            header.append(0x80 | size)
        elif size <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", size))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", size))
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_json(self, value: dict):
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_frame(raw)

    def receive_json(self) -> dict:
        while True:
            first, second = _recv_exact(self.sock, 2)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", _recv_exact(self.sock, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", _recv_exact(self.sock, 8))[0]
            mask = _recv_exact(self.sock, 4) if masked else b""
            payload = _recv_exact(self.sock, length)
            if masked:
                payload = bytes(
                    byte ^ mask[index % 4] for index, byte in enumerate(payload)
                )
            if opcode == 8:
                raise ConnectionError("Колонка закрыла WebSocket")
            if opcode == 9:
                self._send_frame(payload, opcode=10)
                continue
            if opcode == 1:
                return json.loads(payload.decode("utf-8"))


def _envelope(token: str, payload: dict, request_id: str | None = None) -> dict:
    return {
        "conversationToken": token,
        "id": request_id or str(uuid.uuid4()),
        "payload": payload,
        "sentTime": int(time.time() * 1000),
    }


def _wait_for(socket_: GlagolSocket, predicate, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        socket_.sock.settimeout(max(0.2, deadline - time.monotonic()))
        data = socket_.receive_json()
        error = data.get("errorText") or data.get("error")
        if error:
            raise RuntimeError(f"Колонка вернула ошибку: {error}")
        if predicate(data):
            return data
    raise TimeoutError("Колонка не ответила вовремя")


def probe_station(host: str, conversation_token: str, timeout: float = 6) -> dict:
    connection = GlagolSocket(host, timeout=timeout)
    try:
        request_id = str(uuid.uuid4())
        connection.send_json(
            _envelope(conversation_token, {"command": "softwareVersion"}, request_id)
        )
        return _wait_for(
            connection,
            lambda item: bool(
                item.get("softwareVersion")
                or item.get("state")
                or item.get("id") == request_id
                or item.get("requestId") == request_id
            ),
            timeout,
        )
    finally:
        connection.close()


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    out.append(value)
    return bytes(out)


def _protobuf_string(field: int, value: str) -> bytes:
    raw = value.encode("utf-8")
    return _encode_varint((field << 3) | 2) + _encode_varint(len(raw)) + raw


def led_screen_payload(gif_url: str) -> dict:
    content = json.dumps(
        {"animation_sequence": [{"frontal_led_image": gif_url}]},
        ensure_ascii=False,
    )
    protobuf = _protobuf_string(1, "draw_led_screen") + _protobuf_string(2, content)
    return {
        "command": "externalCommandBypass",
        "data": base64.b64encode(protobuf).decode("ascii"),
    }


def send_gif(
    host: str,
    x_token: str,
    device_id: str,
    platform: str,
    gif_url: str,
) -> None:
    token = get_glagol_token(x_token, device_id, platform)
    connection = GlagolSocket(host, timeout=10)
    try:
        hello_id = str(uuid.uuid4())
        connection.send_json(
            _envelope(token, {"command": "softwareVersion"}, hello_id)
        )
        _wait_for(
            connection,
            lambda item: bool(
                item.get("softwareVersion")
                or item.get("state")
                or item.get("id") == hello_id
                or item.get("requestId") == hello_id
            ),
            8,
        )
        request_id = str(uuid.uuid4())
        connection.send_json(_envelope(token, led_screen_payload(gif_url), request_id))
        try:
            reply = _wait_for(
                connection,
                lambda item: item.get("id") == request_id
                or item.get("requestId") == request_id,
                5,
            )
            status = reply.get("status")
            if status and status not in ("OK", "SUCCESS"):
                raise RuntimeError(f"Колонка отклонила изображение: {status}")
        except TimeoutError:
            # Some firmware versions apply draw_led_screen without an ACK.
            pass
    finally:
        connection.close()


def _valid_private_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        return bool(
            address.version == 4
            and address.is_private
            and not address.is_loopback
            and not address.is_reserved
            and not address.is_multicast
            and str(address) != "255.255.255.255"
            and not str(address).startswith("255.")
        )
    except ValueError:
        return False


def local_ipv4_addresses() -> list[str]:
    found: set[str] = set()
    try:
        found.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    for remote in (("1.1.1.1", 53), ("8.8.8.8", 53)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(remote)
            found.add(sock.getsockname()[0])
        except OSError:
            pass
        finally:
            sock.close()
    if os.name == "nt":
        try:
            output = subprocess.run(
                ["ipconfig"],
                capture_output=True,
                text=True,
                errors="ignore",
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout
            found.update(re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", output))
        except Exception:
            pass
    return sorted((item for item in found if _valid_private_ipv4(item)), key=lambda x: (not x.startswith("192.168."), x))


def local_ipv4_networks() -> list[ipaddress.IPv4Network]:
    networks: list[ipaddress.IPv4Network] = []
    if os.name == "nt":
        command = (
            "Get-NetIPAddress -AddressFamily IPv4 | "
            "Where-Object {$_.AddressState -eq 'Preferred' -and $_.IPAddress -notlike '127.*'} | "
            "Select-Object IPAddress,PrefixLength | ConvertTo-Json -Compress"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            data = json.loads(result.stdout or "[]")
            if isinstance(data, dict):
                data = [data]
            for item in data:
                address = str(item.get("IPAddress", ""))
                prefix = int(item.get("PrefixLength", 24))
                if not _valid_private_ipv4(address):
                    continue
                # Avoid scanning enormous corporate/VPN ranges. A /22 still
                # covers the common /23 and /24 home-network layouts.
                prefix = max(22, min(30, prefix))
                networks.append(ipaddress.ip_network(f"{address}/{prefix}", strict=False))
        except Exception:
            pass
    if not networks:
        networks = [
            ipaddress.ip_network(f"{address}/24", strict=False)
            for address in local_ipv4_addresses()
        ]
    return list(dict.fromkeys(networks))


def get_lan_ip(station_ip: str) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((station_ip, 1961))
        result = sock.getsockname()[0]
    finally:
        sock.close()
    if not _valid_private_ipv4(result):
        raise RuntimeError(
            "Маршрут к колонке идёт не через локальную сеть. Отключите VPN и повторите"
        )
    return result


def _port_open(host: str, timeout: float = 0.22) -> bool:
    try:
        with socket.create_connection((host, 1961), timeout=timeout):
            return True
    except OSError:
        return False


def find_station_ip(
    x_token: str,
    device_id: str,
    platform: str,
    current_ip: str = "",
    progress=None,
) -> str:
    token = get_glagol_token(x_token, device_id, platform)
    if current_ip:
        try:
            probe_station(current_ip, token, timeout=3)
            return current_ip
        except Exception:
            pass

    networks = local_ipv4_networks()
    if not networks:
        raise RuntimeError("Не найден адрес компьютера в локальной сети")
    candidates: list[str] = []
    for network in networks:
        candidates.extend(str(address) for address in network.hosts())
    candidates = list(dict.fromkeys(candidates))
    if progress:
        progress(f"Проверяю {len(candidates)} адресов локальной сети…")
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        checks = {pool.submit(_port_open, host): host for host in candidates}
        open_hosts = []
        for future in concurrent.futures.as_completed(checks):
            if future.result():
                open_hosts.append(checks[future])
    for host in open_hosts:
        try:
            probe_station(host, token, timeout=4)
            return host
        except Exception:
            continue
    raise RuntimeError(
        "Колонка не найдена. ПК и Станция должны быть в одной локальной сети; "
        "VPN и гостевая изоляция Wi‑Fi должны быть выключены"
    )


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        super().end_headers()


class _ReusableServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class MediaServer:
    def __init__(self, directory: str | Path, port: int = 8765):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        handler = partial(_QuietHandler, directory=str(self.directory))
        try:
            self.server = _ReusableServer(("0.0.0.0", int(port)), handler)
        except OSError:
            self.server = _ReusableServer(("0.0.0.0", 0), handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url_for(self, path: str | Path, station_ip: str) -> str:
        local_ip = get_lan_ip(station_ip)
        name = urllib.parse.quote(Path(path).name)
        return f"http://{local_ip}:{self.port}/{name}?v={int(time.time() * 1000)}"

    def self_test(self, path: str | Path) -> None:
        name = urllib.parse.quote(Path(path).name)
        url = f"http://127.0.0.1:{self.port}/{name}"
        with urllib.request.urlopen(url, timeout=5) as response:
            if response.status != 200:
                raise RuntimeError(f"Встроенный сервер вернул HTTP {response.status}")

    def close(self):
        self.server.shutdown()
        self.server.server_close()
