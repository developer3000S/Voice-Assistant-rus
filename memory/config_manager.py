import json
import sys
from pathlib import Path

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR    = get_base_dir()
CONFIG_DIR  = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"

def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def config_exists() -> bool:
    return CONFIG_FILE.exists()

def save_api_keys(gemini_api_key: str) -> None:
    ensure_config_dir()

    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    data["gemini_api_key"] = gemini_api_key.strip()

    CONFIG_FILE.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8"
    )

def load_api_keys() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ Не удалось загрузить api_keys.json: {e}")
        return {}

def get_gemini_key() -> str | None:
    return load_api_keys().get("gemini_api_key")

def is_configured() -> bool:
    key = get_gemini_key()
    return bool(key and len(key) > 15)


def get_assistant_name() -> str:
    """Вернуть настроенное имя ассистента; если не задано — 'Anfisa'."""
    return load_api_keys().get("assistant_name", "Anfisa") or "Anfisa"


def get_user_name() -> str:
    """Вернуть настроенное имя пользователя для обращения."""
    return load_api_keys().get("user_name", "")


def save_assistant_config(assistant_name: str, user_name: str) -> None:
    """Сохранить имя ассистента и имя пользователя в конфиге."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["assistant_name"] = assistant_name.strip() or "Anfisa"
    data["user_name"] = user_name.strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── Голос ассистента ─────────────────────────────────────────────────────────
# Готовые голоса Gemini Live. Названия — имена собственные: они одинаковы на
# всех языках, поэтому этот список можно показывать дословно в любой локали.
AVAILABLE_VOICES = ["Charon", "Puck", "Kore", "Fenrir", "Aoede"]
DEFAULT_VOICE    = "Charon"


def get_voice() -> str:
    """Вернуть настроенный голос Live; если он не задан или сохранённое значение
    нам неизвестно — вернуть голос по умолчанию."""
    v = load_api_keys().get("voice_name", DEFAULT_VOICE) or DEFAULT_VOICE
    return v if v in AVAILABLE_VOICES else DEFAULT_VOICE


def save_voice(voice_name: str) -> None:
    """Сохранить выбранный голос Live. Неизвестные имена сводятся к значению по
    умолчанию, чтобы плохое значение никогда не дошло до API и не сломало сессию."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    v = (voice_name or "").strip()
    data["voice_name"] = v if v in AVAILABLE_VOICES else DEFAULT_VOICE
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_wake_word_enabled() -> bool:
    """Включён ли локальный отбор по слову пробуждения (ассистент спит, пока не скажут «Привет Анфиса»)."""
    return load_api_keys().get("wake_word_enabled", False)


def save_wake_word_enabled(enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["wake_word_enabled"] = bool(enabled)
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_brief_enabled() -> bool:
    return load_api_keys().get("morning_brief_enabled", True)


def save_brief_enabled(enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["morning_brief_enabled"] = enabled
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── Аудиоустройства ──────────────────────────────────────────────────────────
# Хранятся НАЗВАНИЯ устройств, а не индексы sounddevice. Индексы сдвигаются
# каждый раз, когда USB-устройство подключают или отключают, поэтому сохранённый
# индекс незаметно начинает указывать на другой микрофон. Пустая строка означает
# «системные по умолчанию» — это и заводская настройка, и то, на что откатится
# сохранённое устройство, которое не удаётся разрешить; значит, отключённые
# наушники дают встроенные динамики, а не падение.

def _patch_config(**fields) -> None:
    """Чтение-изменение-запись одного или нескольких ключей в api_keys.json.

    Каждый сеттер в этом файле делал это вручную. Сведя их здесь, мы получаем
    одну строку на новую настройку и одно место, где испорченный файл конфига
    обрабатывается, а не девять."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.update(fields)
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_input_device() -> str:
    """Название устройства записи (микрофона) или '' — системное по умолчанию."""
    return (load_api_keys().get("input_device", "") or "").strip()


def save_input_device(name: str) -> None:
    _patch_config(input_device=(name or "").strip())


def get_output_device() -> str:
    """Название устройства воспроизведения (динамиков) или '' — системное по умолчанию."""
    return (load_api_keys().get("output_device", "") or "").strip()


def save_output_device(name: str) -> None:
    _patch_config(output_device=(name or "").strip())


def get_plugin_enabled(plugin_name: str) -> bool:
    """Плагин включён по умолчанию в тот момент, когда он обнаружен (модель opt-out)."""
    return load_api_keys().get("plugins_enabled", {}).get(plugin_name, True)


# ── Настройки каждого плагина («токены» / параметры подключения) ──────────────
# Универсальное хранилище, чтобы плагин мог объявить собственные поля конфига
# (PLUGIN_SETTINGS), и UI настроек отрисовал их и сохранял БЕЗ правок в ядре —
# модель «просто положи файл» сохраняется. Значения лежат в
# plugin_config[<namespace>][<key>].
# По умолчанию пространство имён равно имени плагина, но связка плагинов
# (например, тройка принтера: control/watchdog/autoeject) может делить ОДНО
# пространство имён.
def get_plugin_config(namespace: str) -> dict:
    """Все сохранённые значения пространства имён (пустой dict, если ничего нет)."""
    cfg = load_api_keys().get("plugin_config")
    val = cfg.get(namespace) if isinstance(cfg, dict) else None
    return dict(val) if isinstance(val, dict) else {}


def get_plugin_setting(namespace: str, key: str, default=None):
    """Одно значение из пространства имён либо `default`, если не задано."""
    return get_plugin_config(namespace).get(key, default)


def save_plugin_config(namespace: str, values: dict) -> None:
    """Влить `values` в сохранённый конфиг пространства имён (чтение-изменение-
    запись, как и остальные помощники здесь). Затрагиваются только переданные ключи."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    pc = data.get("plugin_config")
    if not isinstance(pc, dict):
        pc = {}
    cur = pc.get(namespace)
    if not isinstance(cur, dict):
        cur = {}
    cur.update(values)
    pc[namespace] = cur
    data["plugin_config"] = pc
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def save_plugin_enabled(plugin_name: str, enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    plugins_cfg = data.get("plugins_enabled")
    if not isinstance(plugins_cfg, dict):
        plugins_cfg = {}
    plugins_cfg[plugin_name] = enabled
    data["plugins_enabled"] = plugins_cfg
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")