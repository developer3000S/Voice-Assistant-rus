"""
BackgroundMonitor — наблюдение за темами, которое настраивает сам пользователь.
Проверяет новости DDG по каждой теме не чаще раза в сутки и уведомляет «Анфису»,
когда появляется новый заголовок.
Никаких криптовалют, финансов и слежки без спроса.
"""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path


# ── Запрещённые категории (не отслеживаются, что бы пользователь ни сказал) ────

_BLOCKED = {
    # Названия брендов / активов — пишутся одинаково на всех языках
    "bitcoin", "ethereum", "dogecoin", "solana", "binance",
    "nft", "blockchain", "defi", "altcoin", "memecoin", "coin", "token",
    # Написания корня «crypto» на разных языках
    "crypto", "kripto", "cripto", "krypto", "крипто", "仮想通貨", "暗号資産",
    "cryptocurrency",
}

def _is_blocked(topic: str) -> bool:
    t = topic.lower()
    return any(word in t for word in _BLOCKED)


# ── Вспомогательные функции: slug и хэш ────────────────────────────────────────

def _slug(topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", topic.lower().strip())[:40].strip("_")

def _title_hash(title: str) -> str:
    return hashlib.md5(title.encode("utf-8", errors="ignore")).hexdigest()[:12]


# ── Чтение и запись в память ───────────────────────────────────────────────────

def _load() -> dict:
    from memory.memory_manager import load_memory
    data = load_memory().get("monitors", {})
    return data if isinstance(data, dict) else {}

def _save(monitors: dict) -> None:
    from memory.memory_manager import load_memory, MEMORY_PATH, _lock
    memory = load_memory()
    memory["monitors"] = monitors
    with _lock:
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ── Публичный API ──────────────────────────────────────────────────────────────

def add_monitor(topic: str) -> str:
    topic = topic.strip()
    if not topic:
        return "Сэр, укажите тему для наблюдения."
    if _is_blocked(topic):
        return "Сэр, криптовалюты и финансовые темы я не отслеживаю."
    monitors = _load()
    slug = _slug(topic)
    if slug in monitors:
        return f"Эта тема уже наблюдается: {monitors[slug]['topic']}"
    monitors[slug] = {
        "topic":      topic,
        "added":      datetime.now().strftime("%Y-%m-%d"),
        "last_check": "",
        "last_hash":  "",
    }
    _save(monitors)
    print(f"[Monitor] ➕ Добавлена: {topic}")
    return f"Начинаю наблюдать за темой: {topic}"


def remove_monitor(topic: str) -> str:
    topic = topic.strip().lower()
    monitors = _load()
    # сначала пробуем точное совпадение по slug
    slug = _slug(topic)
    if slug in monitors:
        label = monitors.pop(slug)["topic"]
        _save(monitors)
        return f"Наблюдение прекращено: {label}"
    # если точно не совпало — ищем частичное совпадение
    for key, val in list(monitors.items()):
        if topic in val.get("topic", "").lower():
            label = monitors.pop(key)["topic"]
            _save(monitors)
            return f"Наблюдение прекращено: {label}"
    return f"Среди наблюдаемых тем не найдено: {topic}"


def list_monitors() -> list[str]:
    return [v.get("topic", k) for k, v in _load().items()]


def check_all() -> list[str]:
    """
    Проверяет все темы, у которых наступил срок проверки (по каждой — не чаще
    раза в сутки).
    Возвращает список строк с меткой [MONITOR_ALERT] — пусто, если нового ничего нет.
    """
    from actions.web_search import _ddg_news

    monitors = _load()
    if not monitors:
        return []

    today   = datetime.now().strftime("%Y-%m-%d")
    alerts  = []
    changed = False

    for slug, data in monitors.items():
        if data.get("last_check") == today:
            continue                     # сегодня эта тема уже проверялась

        topic = data.get("topic", slug)
        try:
            results = _ddg_news(topic, max_results=5)
            if not results:
                monitors[slug]["last_check"] = today
                changed = True
                continue

            top   = results[0]
            title = top.get("title", "").strip()
            if not title:
                continue

            h = _title_hash(title)
            monitors[slug]["last_check"] = today
            changed = True

            if h == data.get("last_hash"):
                continue                 # заголовок тот же, что и в прошлый раз — не уведомляем

            monitors[slug]["last_hash"] = h

            snippet = top.get("snippet", "")[:150]
            source  = top.get("source", "")
            parts   = [f"[MONITOR_ALERT] {topic}", f"Заголовок: {title}"]
            if snippet:
                parts.append(snippet)
            if source:
                parts.append(f"Источник: {source}")
            alerts.append("\n".join(parts))
            print(f"[Monitor] 🔔 Новый заголовок по теме '{topic}': {title[:60]}")

        except Exception as e:
            print(f"[Monitor] ⚠️ Не удалось проверить тему '{topic}': {e}")

    if changed:
        _save(monitors)

    return alerts
