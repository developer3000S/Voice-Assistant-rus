import subprocess
import sys
import json
import re
import time
from pathlib import Path


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR           = get_base_dir()
API_CONFIG_PATH    = BASE_DIR / "config" / "api_keys.json"
DESKTOP            = Path.home() / "Desktop"
MAX_BUILD_ATTEMPTS = 3
GEMINI_MODEL       = "gemini-flash-latest"


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _get_gemini(model: str = GEMINI_MODEL):
    from google import genai
    _c = genai.Client(api_key=_get_api_key())

    class _W:
        def generate_content(self, contents):
            return _c.models.generate_content(model=model, contents=contents)

    return _W()


def _clean_code(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _resolve_save_path(output_path: str, language: str) -> Path:
    ext_map = {
        "python": ".py", "py": ".py",
        "javascript": ".js", "js": ".js",
        "typescript": ".ts", "ts": ".ts",
        "html": ".html", "css": ".css",
        "java": ".java", "cpp": ".cpp", "c": ".c",
        "bash": ".sh", "shell": ".sh", "powershell": ".ps1",
        "sql": ".sql", "json": ".json", "rust": ".rs", "go": ".go",
    }
    if output_path:
        p = Path(output_path)
        return p if p.is_absolute() else DESKTOP / p
    ext = ext_map.get((language or "python").lower(), ".py")
    return DESKTOP / f"Anfisa_code{ext}"


def _read_file(file_path: str) -> tuple[str, str]:
    if not file_path:
        return "", "Не указан путь к файлу."
    p = Path(file_path)
    if not p.exists():
        return "", f"Файл не найден: {file_path}"
    try:
        return p.read_text(encoding="utf-8"), ""
    except Exception as e:
        return "", f"Не удалось прочитать файл: {e}"


def _save_file(path: Path, content: str) -> str:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Сохранено: {path}"
    except Exception as e:
        return f"Не удалось сохранить: {e}"


def _preview(code: str, lines: int = 10) -> str:
    all_lines = code.splitlines()
    preview   = "\n".join(all_lines[:lines])
    suffix    = f"\n... (ещё {len(all_lines) - lines} стр.)" if len(all_lines) > lines else ""
    return preview + suffix


def _has_error(output: str) -> bool:
    error_signals = ["error", "exception", "traceback", "syntaxerror",
                     "nameerror", "typeerror", "stderr", "failed", "crash"]
    return any(s in output.lower() for s in error_signals)


def _take_screenshot() -> Path | None:
    try:
        import pyautogui
        screenshot_path = Path.home() / "Desktop" / f"Anfisa_debug_{int(time.time())}.png"
        screenshot = pyautogui.screenshot()
        screenshot.save(str(screenshot_path))
        print(f"[Code] 📸 Снимок экрана: {screenshot_path}")
        return screenshot_path
    except Exception as e:
        print(f"[Code] ⚠️ Не удалось сделать снимок экрана: {e}")
        return None


def _image_to_base64(path: Path) -> str:
    import base64
    return base64.b64encode(path.read_bytes()).decode("utf-8")


_VALID_INTENTS = {"write", "edit", "explain", "run", "build", "screen_debug", "optimize"}


def _detect_intent(description: str, file_path: str, code: str) -> str:
    """
    Языково-независимое определение намерения — БЕЗ фиксированного списка ключевых слов.
    На каком бы языке ни говорил пользователь, описание классифицирует
    Gemini. Если API недоступен, используются языково-независимые
    структурные признаки (есть ли файл на диске, передан ли код).
    """
    desc        = (description or "").strip()
    file_exists = bool(file_path) and Path(file_path).exists()

    if desc:
        try:
            ctx = []
            if file_path:
                ctx.append(f"указан путь к файлу (существует на диске: {file_exists})")
            if code:
                ctx.append("приведён фрагмент кода прямо в запросе")
            prompt = (
                "Определи для запроса к ассистенту по коду РОВНО ОДНО слово-намерение.\n"
                "Запрос может быть написан на ЛЮБОМ языке.\n\n"
                f"Запрос: {desc}\n"
                + (f"Контекст: {'; '.join(ctx)}\n" if ctx else "")
                + "\nНамерения:\n"
                "  write        = создать новый код с нуля\n"
                "  edit         = изменить существующий файл\n"
                "  explain      = рассказать, что делают данный код/файл\n"
                "  run          = запустить существующий файл\n"
                "  build        = написать код, запустить и переделывать, пока не заработает\n"
                "  screen_debug = разобрать ошибку, которую пользователь видит на экране\n"
                "  optimize     = отрефакторить / упростить / ускорить существующий код\n\n"
                "Ответь ONLY словом-намерением, ничего больше."
            )
            ans = _get_gemini().generate_content(prompt).text.strip().lower()
            ans = ans.strip("`'\". \n")
            if ans in _VALID_INTENTS:
                return ans
        except Exception as e:
            print(f"[Code] Не удалось классифицировать намерение ({e}) — структурный запасной путь")

    # Структурный запасной путь — не привязан ни к одному языку
    if file_exists:
        return "edit" if desc else "explain"
    if code:
        return "explain"
    return "write"

def _write(description: str, language: str, output_path: str, player=None) -> tuple[str, Path]:
    lang  = language or "python"
    model = _get_gemini()

    prompt = f"""Ты — опытный разработчик на {lang}.
Напиши чистый, рабочий и хорошо прокомментированный код на {lang} по описанию ниже.

Правила:
- Выведи ONLY код. Без пояснений, без markdown, без обратных кавычек.
- Добавь полезные комментарии в коде.
- Надёжно обработай ошибки и граничные случаи.
- Используй современные лучшие практики.

Описание: {description}

Код:"""

    response = model.generate_content(prompt)
    code     = _clean_code(response.text)
    path     = _resolve_save_path(output_path, lang)
    _save_file(path, code)
    return code, path


def _fix_code(code: str, error_output: str, description: str) -> str:
    model  = _get_gemini()
    prompt = f"""Ты — опытный отладчик.
Приведённый ниже код упал с указанной ошибкой. Исправь его.
Верни ONLY исправленный код — без пояснений, без markdown, без обратных кавычек.

Исходная задача: {description}

Ошибка:
{error_output[:2000]}

Сломанный код:
{code}

Исправленный код:"""

    response = model.generate_content(prompt)
    return _clean_code(response.text)


def _run_file(path: Path, args: list, timeout: int) -> str:
    interpreters = {
        ".py":  [sys.executable],
        ".js":  ["node"],
        ".ts":  ["ts-node"],
        ".sh":  ["bash"],
        ".ps1": ["powershell", "-File"],
        ".rb":  ["ruby"],
        ".php": ["php"],
    }
    interp = interpreters.get(path.suffix.lower())
    if not interp:
        return f"Нет интерпретатора для {path.suffix}."

    try:
        result = subprocess.run(
            interp + [str(path)] + (args or []),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(path.parent)
        )
        output = result.stdout.strip()
        error  = result.stderr.strip()
        parts  = []
        if output: parts.append(f"Вывод:\n{output}")
        if error:  parts.append(f"Stderr:\n{error}")
        return "\n\n".join(parts) if parts else "Выполнено без вывода."

    except subprocess.TimeoutExpired:
        return f"Превышено время ожидания: {timeout} с."
    except FileNotFoundError:
        return f"Интерпретатор не найден: {interp[0]}."
    except Exception as e:
        return f"Ошибка выполнения: {e}"


def _build(description, language, output_path, args, timeout, speak=None, player=None) -> str:
    if not description:
        return "Сэр, опишите, что именно нужно собрать."

    if player:
        player.write_log("[Code] Сборка началась...")

    lang = language or "python"

    try:
        code, path = _write(description, lang, output_path, player)
        print(f"[Code] ✅ Записан: {path}")
    except Exception as e:
        msg = f"Не удалось записать начальный код: {e}"
        if speak: speak(msg)
        return msg

    last_output = ""
    for attempt in range(1, MAX_BUILD_ATTEMPTS + 1):
        print(f"[Code] 🔄 Попытка {attempt}/{MAX_BUILD_ATTEMPTS}")
        if player:
            player.write_log(f"[Code] Попытка {attempt}...")

        last_output = _run_file(path, args, timeout)

        if not _has_error(last_output):
            msg = (
                f"Сборка завершена, сэр. "
                f"Код работает. Попыток: {attempt}. "
                f"Сохранён: {path}."
            )
            if speak: speak(msg)
            return f"{msg}\n\nВывод:\n{last_output}"

        print(f"[Code] ⚠️ Ошибка в попытке {attempt}, исправляю...")
        if player:
            player.write_log(f"[Code] Исправление (попытка {attempt})...")

        try:
            code = _fix_code(code, last_output, description)
            _save_file(path, code)
        except Exception as e:
            msg = f"Не удалось исправить код в попытке {attempt}: {e}"
            if speak: speak(msg)
            return msg

    msg = (
        f"Сэр, после {MAX_BUILD_ATTEMPTS} попыток я не смогла собрать рабочую версию. "
        f"Последняя ошибка: {last_output[:200]}"
    )
    if speak: speak(msg)
    return f"{msg}\n\nПоследний код сохранён: {path}"

def _write_action(description, language, output_path, player) -> str:
    if not description:
        return "Сэр, опишите, какой код нужно написать."
    if player:
        player.write_log("[Code] Написание кода...")
    try:
        code, path = _write(description, language, output_path, player)
        print(f"[Code] ✅ Записан: {path}")
        return f"Код написан. Сохранён: {path}\n\nПредпросмотр:\n{_preview(code)}"
    except Exception as e:
        return f"Не удалось сгенерировать код: {e}"


def _edit_action(file_path, instruction, player) -> str:
    if not file_path:
        return "Сэр, не указан путь к файлу для редактирования."
    if not instruction:
        return "Сэр, опишите, какое изменение нужно внести."

    content, err = _read_file(file_path)
    if err:
        return err

    if player:
        player.write_log("[Code] Редактирование файла...")

    model  = _get_gemini()
    prompt = f"""Ты — опытный редактор кода.
Внеси указанное изменение в приведённый ниже код.
Верни ONLY полный обновлённый код — без пояснений, без markdown, без обратных кавычек.

Изменение: {instruction}

Исходный код:
{content}

Обновлённый код:"""

    try:
        response = model.generate_content(prompt)
        edited   = _clean_code(response.text)
    except Exception as e:
        return f"Не удалось отредактировать код: {e}"

    status = _save_file(Path(file_path), edited)
    print(f"[Code] ✅ Отредактирован: {file_path}")
    return f"Файл отредактирован. {status}\n\nПредпросмотр:\n{_preview(edited)}"


def _explain_action(file_path, code, player) -> str:
    if file_path and not code:
        code, err = _read_file(file_path)
        if err:
            return err
    if not code:
        return "Сэр, не указаны код или путь к файлу для объяснения."

    if player:
        player.write_log("[Code] Анализ кода...")

    model  = _get_gemini()
    prompt = f"""Объясни простыми и понятными словами, что делает этот код.
Опиши: что он делает, как это работает и какие есть важные детали.
Кратко — максимум от 3 до 6 предложений.

Код:
{code[:4000]}

Объяснение:"""

    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        return f"Не удалось объяснить код: {e}"


def _run_action(file_path, args, timeout, player) -> str:
    if not file_path:
        return "Сэр, не указан путь к файлу для запуска."
    p = Path(file_path)
    if not p.exists():
        return f"Файл не найден: {file_path}"
    if player:
        player.write_log(f"[Code] Запуск {p.name}...")
    return _run_file(p, args, timeout)


def _optimize_action(file_path, code, language, output_path, player) -> str:

    if file_path and not code:
        code, err = _read_file(file_path)
        if err:
            return err
    if not code:
        return "Сэр, не указаны код или путь к файлу для оптимизации."

    if player:
        player.write_log("[Code] Оптимизация кода...")

    lang  = language or "python"
    model = _get_gemini()

    prompt = f"""Ты — опытный разработчик на {lang} и ревьюер кода.
Оптимизируй приведённый код по пунктам:
1. Производительность — убери лишние операции, используй эффективные структуры данных
2. Читаемость — понятные имена переменных, корректное форматирование, логичная структура
3. Лучшие практики — современные идиомы {lang}, обработка ошибок, подсказки типов где уместно
4. Удалить мёртвый код, избыточные комментарии и ненужную сложность

Верни ONLY оптимизированный код — без пояснений, без markdown, без обратных кавычек.

Исходный код:
{code[:6000]}

Оптимизированный код:"""

    try:
        response  = model.generate_content(prompt)
        optimized = _clean_code(response.text)
    except Exception as e:
        return f"Не удалось оптимизировать код: {e}"

    # Сохраняем
    if file_path:
        save_path = Path(file_path)
    else:
        save_path = _resolve_save_path(output_path, lang)

    status = _save_file(save_path, optimized)
    print(f"[Code] ✅ Оптимизирован: {save_path}")

    original_lines  = len(code.splitlines())
    optimized_lines = len(optimized.splitlines())
    diff = original_lines - optimized_lines

    return (
        f"Код оптимизирован. {status}\n"
        f"Строк: {original_lines} → {optimized_lines} "
        f"({'−' if diff > 0 else '+'}{abs(diff)} стр.)\n\n"
        f"Предпросмотр:\n{_preview(optimized)}"
    )


def _screen_debug_action(description, file_path, player, speak=None) -> str:

    if player:
        player.write_log("[Code] Снимаю экран для анализа...")

    print("[Code] 📸 Захват экрана для отладки...")


    screenshot_path = _take_screenshot()
    if not screenshot_path:
        return "Сэр, не удалось сделать снимок экрана. Проверьте, что установлен PyAutoGUI."


    file_content = ""
    if file_path:
        file_content, err = _read_file(file_path)
        if err:
            print(f"[Code] ⚠️ Не удалось прочитать файл: {err}")

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=_get_api_key())

        image_bytes  = screenshot_path.read_bytes()
        image_base64 = _image_to_base64(screenshot_path)

        user_question = description or "Какую ошибку или проблему ты видишь на экране? Как её исправить?"

        context = ""
        if file_content:
            context = f"\n\nДополнительно вот содержимое связанного файла:\n```\n{file_content[:4000]}\n```"

        analysis_prompt = f"""Ты — опытный программист и отладчик, который разбирает снимок экрана.

Вопрос пользователя: {user_question}{context}

Нужно:
1. Найти все ошибки, исключения и проблемы, которые видны на экране
2. Простыми словами объяснить, что вызывает проблему
3. Дать конкретное исправление или решение
4. Если на экране виден код, показать исправленную версию

Отвечай конкретно и применимо. Если видишь текст ошибки, приведи его дословно."""

        contents = [
            types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
            analysis_prompt,
        ]

        response = client.models.generate_content(
            model="gemini-flash-latest",
            contents=contents,
        )

        analysis = response.text.strip()
        print(f"[Code] ✅ Анализ экрана завершён")

        try:
            screenshot_path.unlink()
        except Exception:
            pass

        if file_path and file_content:

            code_match = re.search(r"```[a-zA-Z]*\n(.*?)```", analysis, re.DOTALL)
            if code_match:
                fixed_code = code_match.group(1).strip()
                save_path  = Path(file_path)
                _save_file(save_path, fixed_code)
                analysis += f"\n\n✅ Исправленный код сохранён: {file_path}"
                print(f"[Code] ✅ Исправленный код сохранён: {file_path}")

        return analysis

    except Exception as e:

        try:
            screenshot_path.unlink()
        except Exception:
            pass
        return f"Анализ экрана не выполнен: {e}"


def code_helper(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None
) -> str:
    """
    Вызывается из main.py.

    parameters:
        action      : write | edit | explain | run | build | screen_debug | optimize | auto
        description : Что должен делать код / какое изменение внести / какую проблему разобрать
        language    : Язык программирования (по умолчанию: python)
        output_path : Куда сохранить — пользователь указывает полный путь или имя файла
        file_path   : Путь к существующему файлу (edit / explain / run / build / optimize)
        code        : Строка с кодом как есть (explain/optimize без файла)
        args        : Список аргументов командной строки для run/build
        timeout     : Таймаут выполнения в секундах (по умолчанию: 30)
    """
    p           = parameters or {}
    action      = p.get("action", "auto").lower().strip()
    description = p.get("description", "").strip()
    language    = p.get("language", "python").strip()
    output_path = p.get("output_path", "").strip()
    file_path   = p.get("file_path", "").strip()
    code        = p.get("code", "").strip()
    args        = p.get("args", [])
    timeout     = int(p.get("timeout", 30))

    if action == "auto":
        action = _detect_intent(description, file_path, code)
        print(f"[Code] 🤖 Намерение определено автоматически: {action}")

    if action == "write":
        return _write_action(description, language, output_path, player)

    elif action == "edit":
        return _edit_action(
            file_path,
            description or p.get("instruction", ""),
            player
        )

    elif action == "explain":
        return _explain_action(file_path, code, player)

    elif action == "run":
        return _run_action(file_path, args, timeout, player)

    elif action == "build":
        return _build(description, language, output_path, args, timeout, speak, player)

    elif action == "optimize":
        return _optimize_action(file_path, code, language, output_path, player)

    elif action == "screen_debug":
        return _screen_debug_action(description, file_path, player, speak)

    else:
        return (f"Неизвестное действие: '{action}'. Допустимые значения: "
                f"write, edit, explain, run, build, optimize, screen_debug.")


# ── Описание инструмента (авто-обнаружение через core/action_loader.py) ──────
TOOL = {
    "name": "code_helper",
    "description": "Пишет, редактирует, объясняет, запускает и собирает файлы с кодом.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "write | edit | explain | run | build | auto (по умолчанию: auto)"
            },
            "description": {
                "type": "STRING",
                "description": "Что должен делать код или какое изменение нужно внести"
            },
            "language": {
                "type": "STRING",
                "description": "Язык программирования (по умолчанию: python)"
            },
            "output_path": {
                "type": "STRING",
                "description": "Куда сохранить файл"
            },
            "file_path": {
                "type": "STRING",
                "description": "Путь к существующему файлу для edit/explain/run/build"
            },
            "code": {
                "type": "STRING",
                "description": "Код как есть для explain"
            },
            "args": {
                "type": "STRING",
                "description": "Аргументы командной строки для run/build"
            },
            "timeout": {
                "type": "INTEGER",
                "description": "Таймаут выполнения в секундах (по умолчанию: 30)"
            }
        },
        "required": [
            "action"
        ]
    },
    "handler": code_helper,
}
