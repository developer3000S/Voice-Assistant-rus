"""
Anfisa XL — автоустановщик зависимостей.

Вызывается автоматически при первом запуске и после перенастройки движков.
Ставит только те пакеты, которых действительно нет, после чего корректно завершается.
"""
from __future__ import annotations

import importlib.util
import platform
import subprocess
import sys
from typing import Callable

# ── Списки пакетов ────────────────────────────────────────────────────────
# Каждая запись: (import_name, pip_package_name)

_CORE: list[tuple[str, str]] = [
    ("psutil",             "psutil"),
    ("PIL",                "pillow"),
    ("sounddevice",        "sounddevice"),
    ("numpy",              "numpy"),
    ("requests",           "requests"),
    ("bs4",                "beautifulsoup4"),
    ("ddgs",               "ddgs"),
    ("pyautogui",          "pyautogui"),
    ("pyperclip",          "pyperclip"),
    ("pygetwindow",        "pygetwindow"),
    ("mss",                "mss"),
    ("cv2",                "opencv-python"),
    ("soundfile",          "soundfile"),
    ("miniaudio",          "miniaudio"),
    ("send2trash",         "send2trash"),
    ("pptx",               "python-pptx"),
    ("youtube_transcript_api", "youtube-transcript-api"),
]

# Только Windows (pywinauto, pycaw, win10toast, comtypes)
_WINDOWS: list[tuple[str, str]] = [
    ("comtypes",   "comtypes"),
    ("pycaw",      "pycaw"),
    ("win10toast", "win10toast"),
    ("pywinauto",  "pywinauto"),
]

# Пакеты движков распознавания речи (STT)
_STT: dict[str, list[tuple[str, str]]] = {
    "whisper": [("faster_whisper", "faster-whisper")],
    "vosk":    [("vosk",           "vosk")],
}

# Пакеты движков озвучки (TTS)
_TTS: dict[str, list[tuple[str, str]]] = {
    "edgetts":    [("edge_tts", "edge-tts")],
    # kokoro>=0.9 убрал AlbertModel/AutoModel из transformers — пин версии критичен
    "kokoro":     [("kokoro",   "kokoro>=0.9"), ("soundfile", "soundfile")],
    "elevenlabs": [],   # используется только requests, он уже в core
}


# ── Вспомогательные функции ───────────────────────────────────────────────

def _available(module: str) -> bool:
    """True, если модуль можно импортировать (самого импорта не происходит)."""
    return importlib.util.find_spec(module) is not None


def _playwright_spec() -> str:
    """
    Playwright 1.58 убрал сборки chromium/webkit для macOS 13 и старше, поэтому
    там нужен 1.57.0 — иначе `playwright install chromium` падает с
    "Playwright does not support chromium on mac13".
    """
    if platform.system() == "Darwin":
        # Darwin kernel 22.x = macOS 13, 21.x = macOS 12, и т.д.
        try:
            kernel_major = int(platform.release().split(".")[0])
        except (ValueError, IndexError):
            kernel_major = 0
        if kernel_major and kernel_major < 23:
            return "playwright<1.58"
    return "playwright"


def _pip(package: str, log: Callable | None = None) -> bool:
    if log:
        log(f"SYS: pip install {package} …")
    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "install", package,
            "--quiet", "--disable-pip-version-check",
        ],
        capture_output=True,
    )
    ok = result.returncode == 0
    if not ok and log:
        stderr = result.stderr.decode(errors="replace").strip()
        log(f"ERR: не удалось установить {package} — {stderr[:140]}")
    return ok


# ── Публичный API ─────────────────────────────────────────────────────────

def install_for_config(config: dict, log: Callable | None = None) -> None:
    """
    Установить все отсутствующие пакеты, которые требует *config*.

    Блокирующий — всегда вызывать из фонового потока.
    Прогресс сообщается через необязательный колбэк *log (получает str).
    """
    stt = config.get("stt_engine", "whisper").lower()
    tts = config.get("tts_engine", "edgetts").lower()

    needed: list[tuple[str, str]] = list(_CORE)
    needed += _STT.get(stt, [])
    needed += _TTS.get(tts, [])
    if platform.system() == "Windows":
        needed += _WINDOWS

    # Дедупликация (порядок сохраняется, ключ = имя pip)
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for mod, pkg in needed:
        if pkg not in seen:
            seen.add(pkg)
            unique.append((mod, pkg))

    missing = [(mod, pkg) for mod, pkg in unique if not _available(mod)]

    if not missing:
        if log:
            log("SYS: Все зависимости уже установлены ✓")
        return

    pkg_names = ", ".join(p for _, p in missing)
    if log:
        log(f"SYS: Устанавливаю {len(missing)} пакет(ов): {pkg_names}")

    for _mod, pkg in missing:
        _pip(pkg, log)

    # Playwright: поставить пакет + скачать браузер Chromium
    if not _available("playwright"):
        _pip(_playwright_spec(), log)
        if log:
            log("SYS: Скачиваю браузер Playwright (Chromium, ~150 МБ — один раз)…")
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
        )
        if log:
            log("SYS: Браузер Playwright готов.")

    if log:
        log("SYS: Все зависимости готовы ✓")
