import webbrowser
from urllib.parse import quote_plus


def weather_action(
    parameters: dict,
    player=None,
    session_memory=None,
) -> str:
    city     = parameters.get("city")
    when     = parameters.get("time", "today")  

    if not city or not isinstance(city, str) or not city.strip():
        msg = "Сэр, для прогноза погоды не указан город."
        _log(msg, player)
        return msg

    city = city.strip()
    when = (when or "today").strip()

    search_query  = f"weather in {city} {when}"
    url           = f"https://www.google.com/search?q={quote_plus(search_query)}"

    try:
        opened = webbrowser.open(url)
        if not opened:
            raise RuntimeError("webbrowser.open returned False")
    except Exception as e:
        msg = f"Сэр, не удалось открыть браузер для прогноза погоды: {e}"
        _log(msg, player)
        return msg

    msg = f"Сэр, показываю погоду для {city}, {when}."
    _log(msg, player)

    if session_memory:
        try:
            session_memory.set_last_search(query=search_query, response=msg)
        except Exception:
            pass

    return msg


def _log(message: str, player=None) -> None:
    print(f"[Weather] {message}")
    if player:
        try:
            player.write_log(f"Anfisa: {message}")
        except Exception:
            pass


# ── Описание инструмента (авто-обнаружение через core/action_loader.py) ──────
TOOL = {
    "name": "weather_report",
    "description": "Сообщает пользователю прогноз погоды",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "city": {
                "type": "STRING",
                "description": "Название города"
            }
        },
        "required": [
            "city"
        ]
    },
    "handler": weather_action,
}
