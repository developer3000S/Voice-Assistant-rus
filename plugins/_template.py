"""
Шаблон подключаемого плагина для Анфисы.

Скопируйте этот файл, переименуйте его (без подчёркивания в начале), заполните
PLUGIN и run(). Больше менять ничего не нужно — Анфиса обнаружит плагин сама при
запуске.
"""

PLUGIN = {
    "name": "my_plugin",                     # snake_case, уникальный, ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$
    "description": (
        "Одно-два предложения, по которым Gemini решает, когда вызывать этот "
        "инструмент. Прямо укажите фразы-триггеры, а если инструмент можно "
        "спутать с другим — напишите, какой инструмент НЕЛЬЗЯ использовать "
        "вместо него (образец — description у game_updater в main.py)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "example_arg": {"type": "STRING", "description": "Что означает этот аргумент"},
        },
        "required": [],   # для инструмента без аргументов опустите или оставьте пустым
    },
}

def run(parameters: dict, player=None, session_memory=None) -> str:
    """
    parameters: словарь аргументов, которые извлёк Gemini, — см. PLUGIN['parameters'].
    player: экземпляр AnfisaUI — для записи в журнал используйте
            player.write_log(f"Anfisa: ...") так же, как в actions/*.py. Может быть None.
    session_memory: зарезервировано; сегодня обычно None (встроенные инструменты
            тоже в основном передают None).
    Возвращайте короткую строку живым языком — её озвучат пользователю.
    Никогда не бросайте исключений: перехватывайте свои ошибки и возвращайте
    строку с текстом ошибки для озвучки (загрузчик тоже подхватывает исключения
    как вторую страховку, но полагаться на это не стоит).
    """
    example_arg = parameters.get("example_arg", "")
    try:
        result_text = f"Сделано: {example_arg}."
    except Exception as e:
        return f"Сэр, my_plugin не сработал: {e}"
    if player:
        try:
            player.write_log(f"Anfisa: {result_text}")
        except Exception:
            pass
    return result_text
