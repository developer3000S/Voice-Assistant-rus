#flight_finder.py
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from config import is_windows, is_mac, is_linux

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]

_MONTH_MAP: dict[str, int] = {

    "january": 1, "february": 2, "march": 3,     "april": 4,
    "may": 5,     "june": 6,     "july": 7,       "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# Быстрый путь только для английского — Gemini (ниже) приводит даты к формату
# YYYY-MM-DD на ЛЮБОМ языке, поэтому другие языки здесь не прописываются.
_RELATIVE_MAP_KEYS = {
    "today",
    "tomorrow",
}


def _parse_date(raw: str) -> str:

    raw   = raw.strip()
    lower = raw.lower()
    today = datetime.now()

    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass

    relative = {
        "today": today,
        "tomorrow": today + timedelta(days=1),
    }
    for key, val in relative.items():
        if key in lower:
            return val.strftime("%Y-%m-%d")

    try:
        from google import genai as _genai
        _client  = _genai.Client(api_key=_get_api_key())
        response = _client.models.generate_content(
            model="gemini-flash-lite-latest",
            contents=(
                f"Сегодня {today.strftime('%Y-%m-%d')}. "
                f"Преобразуй это дата-выражение в формат YYYY-MM-DD: '{raw}'. "
                f"Верни ONLY строку с датой и ничего больше."
            )
        )
        result = response.text.strip()
        if re.match(r"\d{4}-\d{2}-\d{2}", result):
            return result
    except Exception as e:
        print(f"[FlightFinder] ⚠️ Не удалось разобрать дату через Gemini: {e}")

    for month_name, month_num in _MONTH_MAP.items():
        if month_name in lower:
            day_match = re.search(r"\d{1,2}", raw)
            if day_match:
                day  = int(day_match.group())
                year = today.year if month_num >= today.month else today.year + 1
                return f"{year}-{month_num:02d}-{day:02d}"

    # Крайний случай: сегодня
    print(f"[FlightFinder] ⚠️ Не удалось разобрать дату '{raw}' — беру сегодняшнее число.")
    return today.strftime("%Y-%m-%d")

_CABIN_CODE: dict[str, str] = {
    "economy":  "1",
    "premium":  "2",
    "business": "3",
    "first":    "4",
}


def _build_google_flights_url(
    origin:      str,
    destination: str,
    date:        str,
    return_date: str | None = None,
    passengers:  int        = 1,
    cabin:       str        = "economy",
) -> str:
    cabin_code = _CABIN_CODE.get(cabin.lower(), "1")
    base       = "https://www.google.com/travel/flights"

    # Google Flights принимает эти query-параметры для предзаполнения
    if return_date:
        trip = f"Flights+from+{origin}+to+{destination}+on+{date}+returning+{return_date}"
    else:
        trip = f"Flights+from+{origin}+to+{destination}+on+{date}"

    return (
        f"{base}"
        f"?q={trip}"
        f"&tfs=CBwQAhoeEgoyMDI1LTAzLTE1agcIARIDSVNUcgcIARIDTEhS"   
        f"&curr=USD"
        f"&cabin={cabin_code}"
        f"&adults={passengers}"
    )



def _search_flights_browser(
    origin:      str,
    destination: str,
    date:        str,
    return_date: str | None,
    passengers:  int,
    cabin:       str,
) -> tuple[str, str]:
    import time
    from actions.browser_control import browser_control

    url = _build_google_flights_url(
        origin, destination, date, return_date, passengers, cabin
    )

    print(f"[FlightFinder] 🌐 Открываю: {url}")
    browser_control({"action": "go_to", "url": url})
    time.sleep(5)

    raw = browser_control({"action": "get_text"})
    return (raw or ""), url

def _parse_flights_with_gemini(
    raw_text:    str,
    origin:      str,
    destination: str,
    date:        str,
) -> list[dict]:
    from google import genai as _genai
    from google.genai import types

    _client = _genai.Client(api_key=_get_api_key())
    prompt  = (
        f"Извлеки варианты перелётов из {origin} в {destination} на {date} "
        f"из текста этой страницы Google Flights:\n\n{raw_text[:12000]}\n\n"
        f"Верни JSON-массив не более чем из 5 рейсов:\n"
        f'[{{"airline":"...","departure":"HH:MM","arrival":"HH:MM",'
        f'"duration":"Xh Ym","stops":0,"price":"...","currency":"USD"}}]\n'
        f"Если рейсов не найдено, верни: []"
    )

    try:
        response = _client.models.generate_content(
            model="gemini-flash-latest",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=(
                    "Ты — специалист по извлечению данных о рейсах. "
                    "Извлекай информацию о рейсах из сырого текста веб-страницы. "
                    "Return ONLY valid JSON — no markdown, no explanation."
                )
            ),
        )
        text     = re.sub(r"```(?:json)?", "", response.text).strip().rstrip("`").strip()
        flights  = json.loads(text)
        return flights if isinstance(flights, list) else []
    except Exception as e:
        print(f"[FlightFinder] ⚠️ Не удалось разобрать ответ через Gemini: {e}")
        return []

def _format_spoken(
    flights:     list[dict],
    origin:      str,
    destination: str,
    date:        str,
) -> str:
    if not flights:
        return (
            f"Сэр, не удалось найти рейсы из {origin} в {destination} "
            f"на {date}. Возможно, страница не загрузилась до конца."
        )

    lines = [f"Сэр, лучшие рейсы из {origin} в {destination} на {date}."]

    for i, f in enumerate(flights[:5], 1):
        airline   = f.get("airline",   "неизвестная авиакомпания")
        departure = f.get("departure", "--:--")
        arrival   = f.get("arrival",   "--:--")
        duration  = f.get("duration",  "")
        stops     = f.get("stops",     0)
        price     = f.get("price",     "")
        currency  = f.get("currency",  "")

        stop_str  = "без пересадок" if stops == 0 else f"{stops} пересадка" if stops == 1 else f"{stops} пересадки" if stops < 5 else f"{stops} пересадок"
        price_str = f"{price} {currency}".strip() if price else "цена недоступна"
        dur_str   = f", в пути {duration}" if duration else ""

        lines.append(
            f"Вариант {i}: {airline}, вылет в {departure}, "
            f"прибытие в {arrival}{dur_str}, {stop_str}, {price_str}."
        )

    # Самый дешёвый — для сравнения оставляем только цифры
    priced = [f for f in flights if f.get("price")]
    if priced:
        cheapest = min(
            priced,
            key=lambda x: int(re.sub(r"[^\d]", "", str(x["price"])) or "999999"),
        )
        lines.append(
            f"Самый дешёвый вариант — {cheapest.get('airline')} "
            f"за {cheapest.get('price')} {cheapest.get('currency', '')}."
        )

    return " ".join(lines)


def _format_text_report(
    flights:     list[dict],
    origin:      str,
    destination: str,
    date:        str,
    return_date: str | None,
    page_url:    str,
) -> str:
    lines = [
        "Анфиса — результаты поиска авиабилетов",
        "─" * 50,
        f"Маршрут       : {origin} → {destination}",
        f"Дата          : {date}",
    ]
    if return_date:
        lines.append(f"Обратно       : {return_date}")
    lines += [
        f"Поиск выполнен: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Источник      : {page_url}",
        "─" * 50,
        "",
    ]

    if not flights:
        lines.append("Рейсов не найдено.")
    else:
        for i, f in enumerate(flights, 1):
            stops    = f.get("stops", 0)
            stop_str = "Без пересадок" if stops == 0 else f"{stops} пересадка" if stops == 1 else f"{stops} пересадки" if stops < 5 else f"{stops} пересадок"
            lines += [
                f"Рейс {i}:",
                f"  Авиакомпания: {f.get('airline',   'N/A')}",
                f"  Отправление : {f.get('departure', 'N/A')}",
                f"  Прибытие    : {f.get('arrival',   'N/A')}",
                f"  В пути      : {f.get('duration',  'N/A')}",
                f"  Пересадки   : {stop_str}",
                f"  Цена        : {f.get('price', 'N/A')} {f.get('currency', '')}",
                "",
            ]

    return "\n".join(lines)

def _save_to_desktop(content: str, origin: str, destination: str) -> str:
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"flights_{origin}_{destination}_{ts}.txt".replace(" ", "_")
    desktop  = Path.home() / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    filepath = desktop / filename

    filepath.write_text(content, encoding="utf-8")
    print(f"[FlightFinder] 💾 Сохранено: {filepath}")

    try:
        if is_windows():
            subprocess.Popen(["notepad.exe", str(filepath)])
        elif is_mac():
            subprocess.Popen(["open", "-t", str(filepath)])
        else:
            subprocess.Popen(["xdg-open", str(filepath)])
    except Exception as e:
        print(f"[FlightFinder] ⚠️ Не удалось открыть текстовый редактор: {e}")

    return str(filepath)


def flight_finder(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}

    origin      = params.get("origin",      "").strip()
    destination = params.get("destination", "").strip()
    date_raw    = params.get("date",        "").strip()
    return_raw  = (params.get("return_date") or "").strip()
    passengers  = max(1, int(params.get("passengers", 1)))
    cabin       = params.get("cabin", "economy").strip().lower()
    save        = bool(params.get("save", False))

    if not origin or not destination:
        return "Сэр, укажите пункт отправления и пункт назначения."
    if not date_raw:
        return "Сэр, укажите дату вылета."

    # Нормализуем класс перелёта
    if cabin not in _CABIN_CODE:
        cabin = "economy"

    date        = _parse_date(date_raw)
    return_date = _parse_date(return_raw) if return_raw else None

    if player:
        player.write_log(f"[FlightFinder] {origin} → {destination} на {date}")

    if speak:
        speak(f"Сэр, ищу рейсы из {origin} в {destination} на {date}.")

    print(
        f"[FlightFinder] ▶️ {origin} → {destination} | {date}"
        f"{' → ' + return_date if return_date else ''}"
        f" | {cabin} | {passengers} pax"
    )

    try:
        raw_text, page_url = _search_flights_browser(
            origin, destination, date, return_date, passengers, cabin
        )

        if not raw_text:
            return "Сэр, не удалось получить данные о рейсах. Возможно, страница не загрузилась."

        if speak:
            speak("Сэр, сейчас анализирую результаты.")

        flights = _parse_flights_with_gemini(raw_text, origin, destination, date)
        spoken  = _format_spoken(flights, origin, destination, date)

        if speak:
            speak(spoken)

        result = spoken

        if save and flights:
            report     = _format_text_report(flights, origin, destination, date, return_date, page_url)
            saved_path = _save_to_desktop(report, origin, destination)
            result    += f" Результаты сохранены на Рабочем столе: {saved_path}"

        return result

    except Exception as e:
        print(f"[FlightFinder] ❌ {e}")
        return f"Сэр, поиск авиабилетов не удался: {e}"


# ── Описание инструмента (авто-обнаружение через core/action_loader.py) ──────
TOOL = {
    "name": "flight_finder",
    "description": "Ищет на Google Flights и озвучивает лучшие варианты.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "origin": {
                "type": "STRING",
                "description": "Город или код аэропорта отправления"
            },
            "destination": {
                "type": "STRING",
                "description": "Город или код аэропорта назначения"
            },
            "date": {
                "type": "STRING",
                "description": "Дата вылета (в любом формате)"
            },
            "return_date": {
                "type": "STRING",
                "description": "Дата обратного вылета для круговых поездок"
            },
            "passengers": {
                "type": "INTEGER",
                "description": "Число пассажиров (по умолчанию: 1)"
            },
            "cabin": {
                "type": "STRING",
                "description": "economy | premium | business | first"
            },
            "save": {
                "type": "BOOLEAN",
                "description": "Сохранить результаты в Notepad"
            }
        },
        "required": [
            "origin",
            "destination",
            "date"
        ]
    },
    "handler": flight_finder,
}
