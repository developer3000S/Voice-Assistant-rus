import json
import re
from datetime import datetime
from threading import Lock
from pathlib import Path
import sys


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
MEMORY_PATH      = BASE_DIR / "memory" / "long_term.json"
_lock            = Lock()
MAX_VALUE_LENGTH = 380

# ── Почему здесь две очень разные цифры ──────────────────────────────────────
#
# Раньше было одно: MEMORY_MAX_CHARS = 2200, применённый ко всему хранилищу.
# Это был лимит на хранение, и существовал он только потому, что вся память
# целиком вставлялась в системный промпт при каждом подключении, — то есть рост
# памяти удорожал каждый запрос. Когда она заполнялась, _trim_to_limit()
# удаляла самые старые записи и печатала одну строку в консоль, которую никто
# не читает. Память, которая «глубоко помнит проекты, предпочтения и личный
# контекст», на деле оказалась длиной в две страницы и тихо забыла имя сестры
# через несколько недель.
#
# Хранение и бюджет промпта теперь — разные заботы:
#
#   MEMORY_MAX_CHARS  — страховка от выхода из-под контроля, а не ограничение
#                       функционала. Штатный сценарий до него не доходит;
#                       доходит баг, который пишет в цикле.
#   PROMPT_CORE_CHARS — то, что реально едет в системный промпт каждую сессию.
#                       Меньше, чем старый дамп всей памяти, поэтому сессии
#                       стартуют быстрее, чем раньше, а не медленнее.
#
# Всё, что не вошло в ядро, остаётся на диске и достаётся по требованию
# инструментом recall_memory — см. search_memory() и format_memory_for_prompt().
MEMORY_MAX_CHARS  = 200_000
PROMPT_CORE_CHARS = 900
PROMPT_INDEX_CHARS = 420
# Сколько записей одна категория максимум может дать в блок ядра — чтобы у
# человека с сорока сохранёнными предпочтениями в промпт всё равно попала сестра.
PROMPT_MAX_PER_CATEGORY = 6

def _empty_memory() -> dict:
    return {
        "identity":      {},
        "preferences":   {},
        "projects":      {},
        "relationships": {},
        "wishes":        {},
        "notes":         {},
    }

def load_memory() -> dict:
    if not MEMORY_PATH.exists():
        return _empty_memory()
    with _lock:
        try:
            data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = _empty_memory()
                for key in base:
                    if key not in data:
                        data[key] = {}
                return data
            return _empty_memory()
        except Exception as e:
            print(f"[Memory] ⚠️ Ошибка загрузки: {e}")
            return _empty_memory()

def _all_entries(memory: dict) -> list[tuple]:
    entries = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict) and "value" in entry:
                entries.append((cat, key, entry))
    return entries


# Устанавливается из main.py, чтобы обрезка памяти попала в журнал активности.
# Удалить то, что человек тебе сам рассказал, и упомянуть об этом только в
# stdout — так память теряет доверие.
_trim_notifier = None


def set_trim_notifier(fn) -> None:
    """Зарегистрировать вызываемый объект callable(str), который показывает
    обрезки памяти пользователю."""
    global _trim_notifier
    _trim_notifier = fn


def _trim_to_limit(memory: dict) -> dict:
    if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
        return memory
    entries = _all_entries(memory)
    entries.sort(key=lambda t: t[2].get("updated", "0000-00-00"))
    dropped = []
    for cat, key, _ in entries:
        if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
            break
        del memory[cat][key]
        dropped.append(f"{cat}/{key}")
        print(f"[Memory] 🗑️  Удалено при обрезке: {cat}/{key}")
    if dropped and _trim_notifier:
        try:
            _trim_notifier(
                f"SYS: Память переполнена — забыто {len(dropped)} самых старых записей "
                f"({', '.join(dropped[:3])}{'…' if len(dropped) > 3 else ''})"
            )
        except Exception:
            pass
    return memory

def save_memory(memory: dict) -> None:
    if not isinstance(memory, dict):
        return
    memory = _trim_to_limit(memory)
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _truncate_value(val: str) -> str:
    if isinstance(val, str) and len(val) > MAX_VALUE_LENGTH:
        return val[:MAX_VALUE_LENGTH].rstrip() + "…"
    return val


def _recursive_update(target: dict, updates: dict) -> bool:
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, dict) and "value" not in value:
            if key not in target or not isinstance(target[key], dict):
                target[key] = {}
                changed = True
            if _recursive_update(target[key], value):
                changed = True
        else:
            new_val  = _truncate_value(str(value["value"] if isinstance(value, dict) else value))
            entry    = {"value": new_val, "updated": datetime.now().strftime("%Y-%m-%d")}
            existing = target.get(key, {})
            if not isinstance(existing, dict) or existing.get("value") != new_val:
                target[key] = entry
                changed = True
    return changed


def update_memory(memory_update: dict) -> dict:
    if not isinstance(memory_update, dict) or not memory_update:
        return load_memory()
    memory = load_memory()
    if _recursive_update(memory, memory_update):
        save_memory(memory)
        print(f"[Memory] 💾 Сохранено: {list(memory_update.keys())}")
    return memory

def _entry_value(entry) -> str:
    """Принимает и форму {'value': ..., 'updated': ...}, и просто строку,
    потому что ранние версии хранилища записывали обычные строки."""
    if isinstance(entry, dict):
        return str(entry.get("value", "") or "").strip()
    return str(entry or "").strip()


def _pretty(key: str) -> str:
    return key.replace("_", " ").strip()


# Идентификация находится в промпте всегда; эти категории конкурируют за
# остаток бюджета по признаку свежести.
_CATEGORY_LABELS = {
    "preferences":   "Предпочтения",
    "projects":      "Активные проекты / цели",
    "relationships": "Люди в его жизни",
    "wishes":        "Желания / планы",
    "notes":         "Заметки",
}

_IDENTITY_FIELDS = ["name", "age", "birthday", "city", "job",
                    "language", "school", "nationality"]


def format_memory_for_prompt(memory: dict | None) -> str:
    """Собрать блок памяти, который попадает в системный промпт.

    Раньше сюда выгружалось всё. Теперь отправляются три вещи:

      1. IDENTITY (идентификация) — всегда и целиком. Она маленькая, и это
         неправильно, когда ассистенту нужно искать, как тебя зовут.
      2. НЕДАВНЕЕ — самые свежие по дате обновления записи из каждой
         остальной категории, до PROMPT_CORE_CHARS. Свежесть — самый дешёвый
         из полезных сигналов релевантности, доступный без эмбеддингов.
      3. УКАЗАТЕЛЬ — *ключи* всего остального, без значений.

    Пункт 3 — то, благодаря чему вспоминание вообще работает. Модель не может
    решить что-то поискать, если она не знает, что это существует: с одними
    только пунктами 1 и 2 на вопрос «who is Ayse?» следовало «I don't know»,
    тогда как ayse_sister лежал на диске непрочитанным. Указатель стоит
    несколько сотен символов и превращает вспоминание из гадания в точный
    поиск.

    Итог по задержке: этот блок МЕНЬШЕ старого полного дампа, поэтому каждая
    сессия подключается с меньшим числом токенов. Иногда модель тратит один
    лишний круг на recall_memory — это покрыто тем подтверждением, которое она
    произносит заранее, перед любым долгим шагом."""
    if not memory:
        return ""

    core_lines: list[str] = []

    # 1. Идентификация — всегда и целиком
    identity = memory.get("identity", {}) or {}
    for field in _IDENTITY_FIELDS:
        val = _entry_value(identity.get(field))
        if not val:
            continue
        if field == "language":
            # Подаётся как наблюдение, а не как настройка. Голая строка
            # «Language: English», записанная месяцы назад, читается как
            # постоянное указание — и была одной из причин, по которым вопрос
            # на турецком возвращался на английском.
            core_lines.append(
                f"Общался с тобой на: {val} (это наблюдение о прошлом — "
                f"всегда отвечай на языке ТЕКУЩЕГО сообщения)")
        else:
            core_lines.append(f"{field.title()}: {val}")
    for key, entry in identity.items():
        if key in _IDENTITY_FIELDS:
            continue
        val = _entry_value(entry)
        if val:
            core_lines.append(f"{_pretty(key).title()}: {val}")

    # 2. Всё остальное — сначала самое свежее по дате обновления
    rest: list[tuple[str, str, str, str]] = []   # (updated, cat, key, value)
    for cat in _CATEGORY_LABELS:
        for key, entry in (memory.get(cat, {}) or {}).items():
            val = _entry_value(entry)
            if not val:
                continue
            updated = (entry.get("updated", "") if isinstance(entry, dict) else "") or "0000-00-00"
            rest.append((updated, cat, key, val))
    rest.sort(key=lambda t: t[0], reverse=True)

    used    = sum(len(l) + 1 for l in core_lines)
    shown: dict[str, list[str]] = {}
    overflow: dict[str, list[str]] = {}

    # Свежесть задаёт порядок, но ни одна категория не может забрать весь бюджет.
    # Без ограничений у человека с сорока сохранёнными предпочтениями промпт
    # состоит из сорока предпочтений и ни одного имени — а ведь именно те
    # категории, которые важнее всего в разговоре, меняются реже всего, поэтому
    # чистая свежесть систематически их закапывает.
    per_cat_used: dict[str, int] = {}
    for _updated, cat, key, val in rest:
        line = f"  - {_pretty(key).title()}: {val}"
        if (per_cat_used.get(cat, 0) < PROMPT_MAX_PER_CATEGORY
                and used + len(line) + 1 <= PROMPT_CORE_CHARS):
            shown.setdefault(cat, []).append(line)
            per_cat_used[cat] = per_cat_used.get(cat, 0) + 1
            used += len(line) + 1
        else:
            overflow.setdefault(cat, []).append(_pretty(key))

    # Указатель — это оглавление, поэтому он перемешан по категориям, а не идёт
    # дальше в порядке свежести. Отсортированный по свежести, он перечислил бы
    # двадцать четыре предпочтения раньше первого упоминания о человеке из его
    # окружения — и та единственная запись, ради которой указатель и существует
    # (старый факт, о котором модели больше неоткуда узнать), отвалилась бы в
    # конце.
    indexed: list[str] = []
    if overflow:
        cats  = [c for c in _CATEGORY_LABELS if overflow.get(c)]
        cursor = {c: 0 for c in cats}
        while cats:
            for cat in list(cats):
                i = cursor[cat]
                if i >= len(overflow[cat]):
                    cats.remove(cat)
                    continue
                indexed.append(overflow[cat][i])
                cursor[cat] = i + 1

    for cat, label in _CATEGORY_LABELS.items():
        if shown.get(cat):
            core_lines.append("")
            core_lines.append(f"{label}:")
            core_lines.extend(shown[cat])

    if not core_lines and not indexed:
        return ""

    out = [
        "[ЧТО ТЫ ЗНАЕШЬ ОБ ЭТОМ ЧЕЛОВЕКЕ — используй естественно, не перечисляй как список]",
        *core_lines,
    ]

    # 3. Указатель того, что есть на диске, но не попало в этот промпт
    if indexed:
        budget, names = PROMPT_INDEX_CHARS, []
        for n in indexed:
            if budget - len(n) - 2 < 0:
                break
            names.append(n)
            budget -= len(n) + 2
        if names:
            out.append("")
            out.append(
                "[ALSO REMEMBERED — значений здесь нет. Вызови recall_memory "
                "с ключевым словом, чтобы прочитать любое из них, прежде чем говорить, что не знаешь]"
            )
            out.append(", ".join(names)
                       + (f" (+ещё {len(indexed) - len(names)})"
                          if len(indexed) > len(names) else ""))

    return "\n".join(out) + "\n"


# ── Вспоминание ───────────────────────────────────────────────────────────────

def _score(query_words: list[str], cat: str, key: str, value: str) -> int:
    """Дешёвая лексическая релевантность. Без эмбеддингов, без сети, без вызова
    модели — это выполняется заметно меньше миллисекунды, и в этом весь смысл:
    вспоминание должно стоить один круг модели, никогда не два."""
    hay_key = _pretty(key).lower()
    hay_val = value.lower()
    score   = 0
    for w in query_words:
        if not w:
            continue
        if w == hay_key:
            score += 10
        elif w in hay_key:
            score += 6
        if w in hay_val:
            score += 3
        if w in cat:
            score += 1
    return score


def search_memory(query: str, limit: int = 8) -> str:
    """Найти сохранённые факты, подходящие под `query`. Обслуживает инструмент recall_memory.

    Пустой запрос трактуется как «покажи всё, что ты знаешь», с ограничением —
    модель задаёт его, когда пользователь говорит «что ты обо мне помнишь?»."""
    memory = load_memory()
    words  = [w for w in re.split(r"[^\w]+", (query or "").lower()) if len(w) > 1]

    rows: list[tuple[int, str, str, str]] = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue                     # пропускаем 'sessions' — это список
        for key, entry in items.items():
            val = _entry_value(entry)
            if not val:
                continue
            s = _score(words, cat, key, val) if words else 1
            if s > 0:
                rows.append((s, cat, key, val))

    if not rows:
        return (f"Ничего не сохранено о «{query}»." if query
                else "Я пока ничего не сохранил об этом человеке.")

    rows.sort(key=lambda r: (-r[0], r[2]))
    lines = [f"{cat}/{_pretty(key)}: {val}" for _s, cat, key, val in rows[:max(1, limit)]]
    head  = (f"Сохранённые факты, подходящие под «{query}»:" if query
             else "Всё сохранённое на данный момент:")
    more  = (f"\n(+ещё {len(rows) - len(lines)} — уточни поиск более узким ключевым словом)"
             if len(rows) > len(lines) else "")
    return head + "\n" + "\n".join(lines) + more


def all_entries_for_ui() -> list[dict]:
    """Плоский список для панели памяти: что Анфиса знает и когда это узнала.
    Отсортирован от новых к старым, чтобы панель открывалась самым свежим."""
    memory = load_memory()
    rows = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            val = _entry_value(entry)
            if not val:
                continue
            rows.append({
                "category": cat,
                "key":      key,
                "value":    val,
                "updated":  (entry.get("updated", "") if isinstance(entry, dict) else ""),
            })
    rows.sort(key=lambda r: (r["updated"] or "0000-00-00"), reverse=True)
    return rows

def remember(key: str, value: str, category: str = "notes") -> str:
    valid = {"identity", "preferences", "projects", "relationships", "wishes", "notes"}
    if category not in valid:
        category = "notes"
    update_memory({category: {key: {"value": value}}})
    return f"Запомнил: {category}/{key} = {value}"


def forget(key: str, category: str = "notes") -> str:
    memory = load_memory()
    cat    = memory.get(category, {})
    if key in cat:
        del cat[key]
        memory[category] = cat
        save_memory(memory)
        return f"Забыто: {category}/{key}"
    return f"Не найдено: {category}/{key}"


forget_memory = forget


# ── Память сессий ─────────────────────────────────────────────────────────────

_SESSION_MAX = 3   # страховочный предел — фактически после pop остаётся 0-1 записей


def save_session_summary(summary: str, language: str = "") -> None:
    """Дописать краткое резюме сессии в 1-2 предложения в long_term.json['sessions']."""
    summary = (summary or "").strip()
    if not summary:
        return
    memory   = load_memory()
    sessions = memory.get("sessions", [])
    if not isinstance(sessions, list):
        sessions = []
    entry: dict = {
        "date":    datetime.now().strftime("%Y-%m-%d"),
        "summary": summary[:280],
    }
    if language:
        entry["language"] = language
    sessions.append(entry)
    memory["sessions"] = sessions[-_SESSION_MAX:]
    with _lock:
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    print(f"[Memory] 📝 Сессия сохранена ({entry['date']}): {summary[:60]}…")


def pop_last_session() -> dict | None:
    """
    Вернуть И УДАЛИТЬ самую свежую запись сессии.
    Этот вызов потребляет запись, поэтому она никогда не повторится в будущих брифингах.
    """
    with _lock:
        if not MEMORY_PATH.exists():
            return None
        try:
            memory   = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            sessions = memory.get("sessions", [])
            if not isinstance(sessions, list) or not sessions:
                return None
            entry = sessions.pop()          # удаляем последнюю запись
            memory["sessions"] = sessions
            MEMORY_PATH.write_text(
                json.dumps(memory, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return entry
        except Exception as e:
            print(f"[Memory] ⚠️ pop_last_session error: {e}")
            return None