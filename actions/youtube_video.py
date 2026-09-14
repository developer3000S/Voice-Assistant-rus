#youtube_video.py
import json
import re
import sys
import time
import subprocess
import shutil
from pathlib import Path
from datetime import datetime
from urllib.parse import quote_plus

try:
    import pyautogui
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    import numpy as np
    _NUMPY = True
except ImportError:
    _NUMPY = False

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    _TRANSCRIPT_OK = True
except ImportError:
    _TRANSCRIPT_OK = False

from config import get_os, is_windows, is_mac, is_linux


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_YT_VIDEO_FILTER = "EgIQAQ%3D%3D"


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _open_url(url: str) -> None:
    try:
        if is_mac():
            subprocess.Popen(["open", url])
        elif is_linux():
            subprocess.Popen(["xdg-open", url])
        else:
            subprocess.Popen(["cmd", "/c", "start", "", url], shell=False)
    except Exception as e:
        print(f"[YouTube] ⚠️ Не удалось открыть ссылку: {e}")

def _scrape_first_video_url(query: str) -> str | None:

    if not _REQUESTS_OK:
        return None

    search_url = (
        f"https://www.youtube.com/results"
        f"?search_query={quote_plus(query)}"
        f"&sp={_YT_VIDEO_FILTER}"
    )

    try:
        r    = requests.get(search_url, headers=HEADERS, timeout=10)
        html = r.text

        video_ids = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html)

        seen = set()
        for vid in video_ids:
            if vid in seen:
                continue
            seen.add(vid)

            if f'/shorts/{vid}' in html:
                continue
            return f"https://www.youtube.com/watch?v={vid}"

    except Exception as e:
        print(f"[YouTube] ⚠️ Не удалось найти первое видео: {e}")

    return None

def _extract_video_id(url: str) -> str | None:
    match = re.search(
        r"(?:v=|\/v\/|youtu\.be\/|\/embed\/|\/shorts\/)([A-Za-z0-9_-]{11})", url
    )
    return match.group(1) if match else None


def _is_valid_youtube_url(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be)", url or ""))


def _ask_for_url(prompt_text: str = "URL видео на YouTube:") -> str | None:
    try:
        import tkinter as tk
        from tkinter import simpledialog

        root = tk._default_root
        if root is None:
            root = tk.Tk()
            root.withdraw()

        url = simpledialog.askstring("ANFISA", prompt_text, parent=root)
        return url.strip() if url else None
    except Exception as e:
        print(f"[YouTube] ⚠️ Не удалось показать диалог ввода URL: {e}")
        return None


def _get_transcript(video_id: str) -> str | None:
    if not _TRANSCRIPT_OK:
        return None
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        transcript      = None

        lang_priority = ["en", "tr", "de", "fr", "es", "it", "pt", "ru", "ja", "ko", "ar", "zh"]

        try:
            transcript = transcript_list.find_manually_created_transcript(lang_priority)
        except Exception:
            pass

        if transcript is None:
            try:
                transcript = transcript_list.find_generated_transcript(lang_priority)
            except Exception:
                for t in transcript_list:
                    transcript = t
                    break

        if transcript is None:
            return None

        fetched = transcript.fetch()
        return " ".join(entry["text"] for entry in fetched)

    except Exception as e:
        print(f"[YouTube] ⚠️ Не удалось получить субтитры: {e}")
        return None


def _summarize_with_gemini(transcript: str, video_url: str) -> str:
    from google import genai as _genai
    from google.genai import types

    _client = _genai.Client(api_key=_get_api_key())
    max_chars = 80000
    truncated = transcript[:max_chars] + ("..." if len(transcript) > max_chars else "")
    response  = _client.models.generate_content(
        model="gemini-flash-latest",
        contents=f"Сделай краткое содержание этой расшифровки видео с YouTube:\n\n{truncated}",
        config=types.GenerateContentConfig(
            system_instruction=(
                "Ты — Анфиса, ассистент на основе ИИ. "
                "Кратко и понятно пересказывай расшифровки видео с YouTube. "
                "Структура: общий обзор в 1 предложение, затем 3-5 ключевых пунктов. "
                "Говори прямо по делу. Обращайся к пользователю «сэр». "
                "Отвечай на языке исходной расшифровки."
            )
        )
    )
    return response.text.strip()


def _save_summary(content: str, video_url: str) -> str:
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"youtube_summary_{ts}.txt"
    desktop  = Path.home() / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    filepath = desktop / filename

    header = (
        f"Анфиса — краткое содержание видео с YouTube\n"
        f"{'─' * 50}\n"
        f"Ссылка : {video_url}\n"
        f"Дата   : {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"{'─' * 50}\n\n"
    )
    filepath.write_text(header + content, encoding="utf-8")

    try:
        if is_windows():
            subprocess.Popen(["notepad.exe", str(filepath)])
        elif is_mac():
            subprocess.Popen(["open", "-t", str(filepath)])
        else:
            subprocess.Popen(["xdg-open", str(filepath)])
    except Exception as e:
        print(f"[YouTube] ⚠️ Не удалось открыть текстовый редактор: {e}")

    return str(filepath)


def _scrape_video_info(video_id: str) -> dict:
    if not _REQUESTS_OK:
        return {}
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        r    = requests.get(url, headers=HEADERS, timeout=12)
        html = r.text
        info = {}

        for key, pattern in [
            ("title",    r'"title":\{"runs":\[\{"text":"([^"]+)"'),
            ("channel",  r'"ownerChannelName":"([^"]+)"'),
            ("views",    r'"viewCount":"(\d+)"'),
            ("duration", r'"lengthSeconds":"(\d+)"'),
            ("likes",    r'"label":"([0-9,]+ likes)"'),
        ]:
            match = re.search(pattern, html)
            if match:
                raw = match.group(1)
                if key == "views":
                    info[key] = f"{int(raw):,}"
                elif key == "duration":
                    secs = int(raw)
                    info[key] = f"{secs // 60}:{secs % 60:02d}"
                else:
                    info[key] = raw

        return info
    except Exception as e:
        print(f"[YouTube] ⚠️ Не удалось получить данные о видео: {e}")
        return {}


def _scrape_trending(region: str = "TR", max_results: int = 8) -> list[dict]:
    if not _REQUESTS_OK:
        return []
    url = f"https://www.youtube.com/feed/trending?gl={region.upper()}"
    try:
        r    = requests.get(url, headers=HEADERS, timeout=12)
        html = r.text

        titles   = re.findall(r'"title":\{"runs":\[\{"text":"([^"]+)"\}\]', html)
        channels = re.findall(r'"ownerText":\{"runs":\[\{"text":"([^"]+)"', html)

        results, seen = [], set()
        for i, title in enumerate(titles):
            if title in seen or len(title) < 5:
                continue
            seen.add(title)
            channel = channels[i] if i < len(channels) else "неизвестный канал"
            results.append({"rank": len(results) + 1, "title": title, "channel": channel})
            if len(results) >= max_results:
                break

        return results
    except Exception as e:
        print(f"[YouTube] ⚠️ Не удалось получить тренды: {e}")
        return []

def _handle_play(parameters: dict, player) -> str:
    query = parameters.get("query", "").strip()
    if not query:
        return "Сэр, скажите, что именно вы хотите посмотреть."

    if player:
        player.write_log(f"[YouTube] Поиск: {query}")

    print(f"[YouTube] 🔍 Ищу первое видео не-Shorts по запросу: {query}")

    video_url = _scrape_first_video_url(query)

    if video_url:
        print(f"[YouTube] ▶️ Открываю: {video_url}")
        _open_url(video_url)
        return f"Сэр, включаю: {query}"

    print(f"[YouTube] ⚠️ Не удалось найти видео, открываю страницу поиска с фильтром")
    fallback_url = (
        f"https://www.youtube.com/results"
        f"?search_query={quote_plus(query)}"
        f"&sp={_YT_VIDEO_FILTER}"
    )
    _open_url(fallback_url)
    return f"Сэр, открыт поиск на YouTube по запросу: {query} — выберите видео вручную"


def _handle_summarize(parameters: dict, player, speak) -> str:
    if not _TRANSCRIPT_OK:
        return "Модуль youtube-transcript-api не установлен. Выполните: pip install youtube-transcript-api"

    url = _ask_for_url("Сэр, вставьте ссылку на видео YouTube:")
    if not url:
        return "Сэр, ссылка не указана. Краткое содержание отменено."
    if not _is_valid_youtube_url(url):
        return "Сэр, это не похоже на корректную ссылку YouTube."

    video_id = _extract_video_id(url)
    if not video_id:
        return "Сэр, не удалось извлечь ID видео из этой ссылки."

    if player:
        player.write_log(f"[YouTube] Краткое содержание: {url}")
    if speak:
        speak("Сэр, сейчас загружаю субтитры. Минуту.")

    transcript = _get_transcript(video_id)
    if not transcript:
        return "Сэр, для этого видео не удалось получить субтитры."

    if speak:
        speak("Субтитры получены. Сейчас формирую краткое содержание.")

    try:
        summary = _summarize_with_gemini(transcript, url)
    except Exception as e:
        return f"Сэр, не удалось создать краткое содержание: {e}"

    if speak:
        speak(summary)

    if parameters.get("save", False):
        saved_path = _save_summary(summary, url)
        return f"Сэр, краткое содержание готово и сохранено на Рабочем столе: {saved_path}"

    return summary


def _handle_get_info(parameters: dict, player, speak) -> str:
    url = parameters.get("url", "").strip()
    if not url:
        url = _ask_for_url("Сэр, вставьте ссылку на видео YouTube:")
    if not url or not _is_valid_youtube_url(url):
        return "Сэр, укажите корректную ссылку на видео YouTube."

    video_id = _extract_video_id(url)
    if not video_id:
        return "Сэр, не удалось извлечь ID видео."

    if player:
        player.write_log(f"[YouTube] Данные видео: {url}")

    info = _scrape_video_info(video_id)
    if not info:
        return "Сэр, не удалось получить информацию о видео."

    # Подписи для вывода; ключи info остаются прежними
    labels = {
        "title":    "Название",
        "channel":  "Канал",
        "views":    "Просмотры",
        "duration": "Длительность",
        "likes":    "Лайки",
    }
    lines = [
        f"{labels[key]}: {info[key]}"
        for key in ("title", "channel", "views", "duration", "likes")
        if key in info
    ]
    result = "\n".join(lines)

    if speak:
        speak(f"Сэр, вот данные о видео. {result.replace(chr(10), '. ')}")

    return result


def _handle_trending(parameters: dict, player, speak) -> str:
    region = parameters.get("region", "TR").upper()

    if player:
        player.write_log(f"[YouTube] Тренды: {region}")

    trending = _scrape_trending(region=region, max_results=8)
    if not trending:
        return f"Сэр, не удалось загрузить трендовые видео для региона {region}."

    lines  = [f"Топ трендовых видео в регионе {region}:"]
    lines += [f"{v['rank']}. {v['title']} — {v['channel']}" for v in trending]
    result = "\n".join(lines)

    if speak:
        top3   = trending[:3]
        spoken = "Сэр, вот лучшие трендовые видео. " + ". ".join(
            f"Номер {v['rank']}: {v['title']}, канал {v['channel']}" for v in top3
        )
        speak(spoken)

    return result

_ACTION_MAP = {
    "play":      _handle_play,
    "summarize": _handle_summarize,
    "get_info":  _handle_get_info,
    "trending":  _handle_trending,
}


def youtube_video(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    params = parameters or {}
    action = params.get("action", "play").lower().strip()

    if player:
        player.write_log(f"[YouTube] Действие: {action}")
    print(f"[YouTube] ▶️  Действие: {action}  Параметры: {params}")

    handler = _ACTION_MAP.get(action)
    if handler is None:
        return (
            f"Неизвестное действие YouTube: '{action}'. "
            "Доступны: play, summarize, get_info, trending."
        )

    try:
        if action == "play":
            return handler(params, player) or "Готово."
        return handler(params, player, speak) or "Готово."
    except Exception as e:
        print(f"[YouTube] ❌ Ошибка в {action}: {e}")
        return f"Сэр, действие YouTube {action} не выполнено: {e}"


# ── Описание инструмента (авто-обнаружение через core/action_loader.py) ──────
TOOL = {
    "name": "youtube_video",
    "description": "Управляет YouTube. Применяй для: воспроизведения видео, краткого содержания видео, получения данных о видео или показа трендовых видео.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "play | summarize | get_info | trending (default: play)"
            },
            "query": {
                "type": "STRING",
                "description": "Поисковый запрос для действия play"
            },
            "save": {
                "type": "BOOLEAN",
                "description": "Сохранить краткое содержание в Notepad (только summarize)"
            },
            "region": {
                "type": "STRING",
                "description": "Код страны для трендов, напр. TR, US"
            },
            "url": {
                "type": "STRING",
                "description": "Ссылка на видео для действия get_info"
            }
        },
        "required": []
    },
    "handler": youtube_video,
}
