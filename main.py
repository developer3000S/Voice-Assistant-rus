import platform as _platform
import subprocess as _subprocess

# ── Радикально: навязываем CREATE_NO_WINDOW КАЖДОМУ вызову subprocess на Windows ─
# Патчится сам Popen, поэтому флаг в отдельных файлах не нужен нигде.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # сбрасываем любой устаревший/общий STARTUPINFO
            super().__init__(args, **                       kw)

    _subprocess.Popen = _Popen

# ─────────────────────────────────────────────────────────────────────────────

# ── Кодировка консоли ────────────────────────────────────────────────────────
# Строки статуса в этом приложении содержат эмодзи и стрелки («📤 file_controller
# → Moved: a.txt → Documents/»). В консоли с не-UTF-8 — cp1254 на турецкой Windows,
# cp1251 на русской, cp932 на японской — печать такой строки raises
# UnicodeEncodeError, и поскольку print стоит после try/except самого инструмента,
# исключение уходит в приёмный цикл и рвёт сессию. Ассистент умирает на строке лога.
#
# Переконфигурирование ничего не стоит и заставляет приложение запускаться
# одинаково в любой локали. `errors="replace"` — дополнительная страховка: консоль,
# которая действительно не может отрисовать глиф, покажет квадратик вместо того,
# чтобы убивать процесс.
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import asyncio
import re
import threading
import time
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

import sounddevice as sd
import numpy as np
from google import genai
from google.genai import types
from ui import AnfisaUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    save_session_summary, pop_last_session,
    search_memory, set_trim_notifier,
)

# Файловые инструменты (open_app, web_search, browser_control, …) здесь больше не
# импортируются и не объявляются — они описывают себя сами через dict TOOL в своём
# файле actions/*.py и обнаруживаются core.action_loader при запуске.
# В этом файле остаются только инструменты, привязанные к состоянию живой сессии
# (screen_process, close_camera, save_memory, manage_monitor, shutdown_Anfisa,
# system_status).
from actions.screen_processor  import _capture_camera, _capture_screen
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.proactive         import ProactiveEngine
from actions.background_monitor import (
    add_monitor, remove_monitor, list_monitors, check_all as monitor_check_all,
)
from actions.web_search        import _news as _fetch_news_sync
from memory.config_manager     import (
    get_brief_enabled, get_voice, get_wake_word_enabled, save_wake_word_enabled,    get_input_device, get_output_device,
)
from core.plugin_loader        import discover_plugins
from core                      import undo as undo_stack
from core                      import confirm as confirm_gate
from core                      import audio_devices
from core.action_loader        import discover_actions
from core.wake_word            import (
    WakeWordDetector, is_ready as wake_is_ready, install_and_download as wake_install,
)

# Сколько секунд ассистент остаётся бодрым без речи пользователя, прежде чем
# снова уснуть автоматически (режим слова пробуждения).
WAKE_SLEEP_TIMEOUT = 120.0   # секунд (2 минуты)

def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
LIVE_MODEL          = "models/gemini-3.1-flash-live-preview"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000 
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

# RMS, ниже которого 16-битный PCM считается тишиной в комнате; выше _LEVEL_FULL
# волна рисуется на всю высоту. Настроено так, чтобы обычная речь попадала в среднюю
# часть шкалы, а полосы продолжали двигаться и при тихом голосе — не зависит ни от
# языка, ни от устройства.
_LEVEL_FLOOR = 60.0
_LEVEL_FULL  = 2600.0


def _pcm_level(samples) -> float:
    """Переводит блок int16 PCM-сэмплов в уровень громкости 0.0–1.0 для волны на HUD.
    На пустом/некорректном входе возвращает 0.0, поэтому никогда не бросает исключение."""
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(x * x)))
    except Exception:
        return 0.0
    if rms <= _LEVEL_FLOOR:
        return 0.0
    return min(1.0, (rms - _LEVEL_FLOOR) / (_LEVEL_FULL - _LEVEL_FLOOR))


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are Anfisa, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()

TOOL_DECLARATIONS = [
    # ── Встроенные инструменты ───────────────────────────────────────────────
    # Они остаются здесь (а не в dict TOOL внутри actions/*.py), потому что их
    # обработка вплетена в состояние живой сессии — захват/инъекция vision,
    # поток камеры, записи в память, монитор-движок и завершение работы. Все
    # остальные инструменты живут в своём файле действий и обнаруживаются
    # автоматически через core.action_loader (см. AnfisaLive.__init__).
    {
        "name": "system_status",
        "description": (
            "Возвращает метрики системы в реальном времени: загрузку CPU, ОЗУ, GPU, "
            "температуру процессора, время работы и число процессов. Используй, когда "
            "пользователь спрашивает о производительности компьютера, температуре, "
            "памяти или расходе ресурсов."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Захватывает изображение экрана или веб-камеры, чтобы ты мог его проанализировать. "
            "Обязательно вызывай, когда пользователь спрашивает, что на экране, "
            "что ты видишь, просит посмотреть в камеру, проанализировать экран и т. п. "
            "Без этого инструмента ты вообще ничего не видишь. "
            "После захвата изображение отправляется напрямую тебе — опиши, что ты видишь, и ответь на вопрос пользователя. "
            "При работе с камерой: живое окно остаётся открытым, пока пользователь не скажет его закрыть или не вызовет close_camera."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' — захватить экран, 'camera' — веб-камера. По умолчанию: 'screen'"},
                "text":  {"type": "STRING", "description": "Вопрос или указание про захваченное изображение"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "close_camera",
        "description": (
            "Закрывает показанное на экране живое окно камеры. "
            "Вызывай, когда пользователь говорит (НА ЛЮБОМ языке): закрой камеру, "
            "выключи камеру, стоп камера, это жутко и т. п."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "manage_monitor",
        "description": (
            "Добавить, удалить или показать список фоновых тем наблюдения. "
            "Анфиса проверяет эти темы раз в день и уведомляет пользователя, когда появляется новое развитие событий. "
            "Используй 'add', когда пользователь говорит «следи за X», «отслеживай X», «мониторь X». "
            "Используй 'remove', когда пользователь говорит «прекрати следить за X». "
            "Используй 'list', когда пользователь спрашивает, за чем идёт наблюдение. "
            "НЕ добавляй крипто, финансовые и торговые темы."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type":        "STRING",
                    "description": "add | remove | list",
                },
                "topic": {
                    "type":        "STRING",
                    "description": "Тема наблюдения или тема, за которой перестаём следить (напр. 'space exploration', 'AI news')",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "shutdown_Anfisa",
        "description": (
            "Полностью завершает работу ассистента. "
            "Вызывай, когда пользователь выражает намерение закончить разговор, "
            "закрыть ассистента, попрощаться или остановить Анфису. "
            "Пользователь может сказать это НА ЛЮБОМ языке."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Сохранить важный личный факт о пользователе в долговременную память. "
            "Вызывай молча каждый раз, когда пользователь сообщает что-то, стоящее запоминания: "
            "имя, возраст, город, работу, предпочтения, увлечения, отношения, проекты или планы на будущее. "
            "НЕ вызывай для: погоды, напоминаний, поисков, одноразовых команд. "
            "НЕ объявляй, что сохраняешь — просто вызывай молча. "
            "Значения всегда на английском, независимо от языка разговора."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — имя, возраст, день рождения, город, работа, язык, национальность | "
                        "preferences — любимая еда/цвет/музыка/фильм/игра/спорт, увлечения | "
                        "projects — текущие проекты, цели, то, что строится | "
                        "relationships — друзья, семья, партнёр, коллеги | "
                        "wishes — планы на будущее, что купить, мечты о поездках | "
                        "notes — привычки, расписание, всё остальное, что стоит запомнить"
                    )
                },
                "key":   {"type": "STRING", "description": "Короткий ключ в snake_case (напр. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Краткое значение на английском (напр. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "Найти факт о пользователе, который ты сохранил, но которого НЕТ "
            "в блоке памяти твоего системного промпта. "
            "В промпте под '[ALSO REMEMBERED]' перечислены ключи, для которых "
            "не хватило места — если пользователь спрашивает про что-то из этого списка, "
            "сначала вызови этот инструмент. "
            "Также вызывай его, прежде чем сказать, что не знаешь чего-то личного, и "
            "когда пользователь спрашивает, что ты о нём помнишь (оставь query пустым, "
            "чтобы получить всё). "
            "Это локальный поиск по файлу: мгновенно и бесплатно."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Ключевое слово для поиска — имя, тема, категория "
                        "(напр. 'ayse', 'coffee', 'projects'). "
                        "Оставь пустым, чтобы показать всё сохранённое."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "undo",
        "description": (
            "Отменить последнее изменение, СДЕЛАННОЕ ТОБОЙ на этом компьютере, — файл, который ты "
            "переместил, переименовал, создал или записал, либо настройку, которую ты изменил, такую как "
            "громкость, яркость, тёмная тема или WiFi. "
            "Вызывай, когда пользователь говорит undo, revert, take it back, put it "
            "back, cancel that или говорит, что ты сделал не то, на ЛЮБОМ "
            "языке. "
            "Используй action='list', когда он спрашивает, что можно отменить. "
            "Это касается только твоих собственных действий — это не Ctrl+Z для того приложения, "
            "которое сейчас на экране (это computer_settings с действием 'undo')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "undo (по умолчанию) — отменить последнее изменение | list — показать, что можно отменить",
                },
            },
            "required": [],
        },
    },
]

class _ReconnectSignal(Exception):
    """Бросается внутри TaskGroup сессии, чтобы форсировать чистое, добровольное
    переподключение (напр. пользователь выбрал новый голос — голос фиксируется в
    момент подключения, поэтому сессию нужно пересобрать).

    Несёт `keep_context`: True для обычной пересборки, когда сохранённый
    handle возобновления воспроизводится и разговор продолжается; False, когда
    новая сессия должна действительно начаться с чистого листа (см. заметку о
    смене голоса в _on_voice_change)."""

    def __init__(self, keep_context: bool = True):
        super().__init__()
        self.keep_context = keep_context


def _is_reconnect_signal(exc: BaseException) -> bool:
    """True, если `exc` — это _ReconnectSignal или (Base)ExceptionGroup, которая
    его оборачивает: TaskGroup собирает дочерние исключения в группу."""
    if isinstance(exc, _ReconnectSignal):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_reconnect_signal(sub) for sub in exc.exceptions)
    return False


def _keep_context_of(exc: BaseException) -> bool:
    """Достаёт `keep_context` из сигнала переподключения, раскручивая группу, в
    которую его положил TaskGroup. По умолчанию True: неожиданная форма не должна
    молча стирать разговор."""
    if isinstance(exc, _ReconnectSignal):
        return getattr(exc, "keep_context", True)
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            if _is_reconnect_signal(sub):
                return _keep_context_of(sub)
    return True


class AnfisaLive:
    def __init__(self, ui: AnfisaUI):
        self.ui             = ui
        self._asst_name     = "JARVI    S"   # обновляется каждую сессию из конфига
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                     = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True, пока идёт стрим с телефона; ставит микрофон ПК на паузу
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) для инъекции после ответа инструмента
        self._vision_cam_active    = False   # True, если камера открывалась для vision → авто-закрытие после ответа
        self._vision_close_pending = False   # True после инъекции vision; следующий turn_complete закроет камеру
        self._vision_last_time     = 0.0     # monotonic-время последнего вызова screen_process (защита от повторов)
        self._vision_busy          = False   # True, пока цикл захвата/инъекции vision в работе
        self._interrupted          = False   # True, пока сливаем аудио после прерывания пользователем
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self.ui.on_voice_change   = self._on_voice_change     # выбор голоса → пересборка сессии
        self.ui.on_audio_device_change = self._on_audio_device_change
        self._reconnect_event: asyncio.Event | None = None
        self._reconnect_keep = True   # False → следующая пересборка отбрасывает handle возобновления

        # ── Возобновление сессии ───────────────────────────────────────────
        # Сервер каждые несколько секунд выдаёт handle возобновления и перевыдаёт
        # его по мере разговора. До этого session_resumption был включён в конфиге,
        # но обновления никогда не читались — handle выбрасывался, и КАЖДОЕ
        # переподключение — потерянный пакет, смена голоса, переключение
        # микрофона — начинали пустую сессию. «Бессрочные сессии» текли именно
        # через эту дыру.
        #
        # Намеренно только в ОЗУ, на диск не пишем никогда. Сохрани мы его, свежий
        # запуск продолжил бы вчерашний разговор — звучит заманчиво, но ломает поток
        # итога сессии: _save_session_summary срабатывает при
        # завершении, а утренний брифинг на следующий день достаёт его оттуда.
        # Разговор, который никогда не заканчивается, не даёт итог, и строка
        # «вчера мы говорили про…» молча исчезает.
        self._resume_handle: str | None = None
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # утренний брифинг срабатывает один раз на процесс
        self._sys_monitor      = SystemMonitor()  # состояние пауз оповещений сохраняется между вызовами
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()  # обновляется на каждую реплику пользователя
        self._session_log: list[str] = []          # реплики разговора для итога в конце сессии

        self._enhanced_live = True  # проактивное аудио; отключается само, если сервер его отклонит

        _base_dir = Path(__file__).resolve().parent
        _inline_names = {t["name"] for t in TOOL_DECLARATIONS}

        # Файловые инструменты: все actions/*.py с dict TOOL — обнаруживаются тем
        # же способом, что и плагины. Зарезервированные имена — встроенные
        # инструменты выше, поэтому действие не может перекрыть ни одно из них.
        self._action_registry = discover_actions(
            actions_dir=_base_dir / "actions",
            reserved_names=_inline_names,
            logger=lambda msg: print(f"[Actions] {msg}"),
        )

        # Плагин не должен коллизировать ни с встроенным инструментом, ни с действием.
        _core_names = _inline_names | self._action_registry.names()
        self._plugin_registry = discover_plugins(
            plugins_dir=_base_dir / "plugins",
            core_tool_names=_core_names,
            logger=lambda msg: (print(f"[Plugins] {msg}"), self.ui.write_log(f"SYS: {msg}")),
        )
        self.ui.get_plugins = self._plugin_registry.list_for_ui
        self.ui.get_plugin_settings = self._plugin_registry.settings_schemas  # ⚙ вкладка настроек
        self.ui.request_say = self.plugin_say   # плагины: канал озвучки посреди задачи

        # ── Слово пробуждения ─────────────────────────────────────────────────
        # _awake закрывает микрофон (см. _listen_audio) и фоновые реплики.
        # Он True всегда, когда слово пробуждения ВЫКЛ — поведение по умолчанию не меняется.
        self._wake_enabled     = get_wake_word_enabled()
        self._awake            = not self._wake_enabled
        self._wake_detector: WakeWordDetector | None = None
        self._wake_sleep_timeout = WAKE_SLEEP_TIMEOUT
        # Интерфейс управления для секции настроек «Слово пробуждения».
        self.ui.wake_is_ready    = wake_is_ready          # () -> bool
        self.ui.wake_get_state   = self._wake_state       # () -> dict
        self.ui.on_wake_toggle   = self._ui_wake_toggle   # (enable: bool) -> str
        self.ui.on_wake_manual   = self._ui_wake_manual   # () -> переключает бодр/спит
        self.ui.on_wake_install  = self._ui_wake_install  # () -> (ok, msg)

    # ── Слово пробуждения: машина состояний ───────────────────────────────────

    def _wake_state(self) -> dict:
        # Загруженный и работающий детектор заведомо готов; иначе полагаемся на
        # дешёвую проверку файлов модели на диске (без создания Model).
        ready = bool(self._wake_detector and self._wake_detector.ready) or wake_is_ready()
        return {"enabled": self._wake_enabled, "awake": self._awake, "ready": ready}

    def _ensure_wake_detector(self) -> bool:
        """Загружает детектор один раз (модель читается при первом старте). Идемпотентно."""
        if self._wake_detector is None:
            self._wake_detector = WakeWordDetector(
                on_detect=self._on_wake_detected,
                logger=lambda m: (print(f"[Wake] {m}"), self.ui.write_log(f"SYS: {m}")),
            )
        if not self._wake_detector.ready:
            return self._wake_detector.start()
        return True

    def _on_wake_detected(self) -> None:
        """Вызывается из потока детектора, когда услышано «Привет Анфиса»."""
        self.wake(reason="слово пробуждения")

    def wake(self, reason: str = "слово пробуждения") -> None:
        if self._awake:
            return
        self._awake = True
        self._last_user_speech = time.monotonic()   # теперь запускаем часы авто-засыпания
        if not self.ui.muted:
            self.ui.set_state("LISTENING")
        self.ui.write_log(f"SYS: Бодрствую — {reason}.")

    def sleep(self, reason: str = "таймаут") -> None:
        if not self._awake:
            return
        self._awake = False
        self.set_speaking(False)
        self.ui.set_state("SLEEPING")
        self.ui.write_log(f"SYS: Засыпаю — {reason}. Скажи «Привет Анфиса», чтобы разбудить меня.")

    async def _run_sleep_watch(self) -> None:
        """Авто-засыпание после заданного окна тишины (только режим слова пробуждения)."""
        while True:
            await asyncio.sleep(5)
            if not self._wake_enabled or not self._awake:
                continue
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue
            if (time.monotonic() - self._last_user_speech) > self._wake_sleep_timeout:
                self.sleep(reason="нет речи две минуты")

    # ── Слово пробуждения: колбэки UI (вызываются из потока Qt) ────────────────

    def _ui_wake_toggle(self, enable: bool) -> str:
        """Включает/выключает слово пробуждения из настроек UI. Возвращает статусный
        токен: 'enabled' | 'disabled' | 'need_download'."""
        if enable:
            if not wake_is_ready():
                return "need_download"
            self._wake_enabled = True
            save_wake_word_enabled(True)
            self._ensure_wake_detector()
            self.sleep(reason="слово пробуждения включено")
            return "enabled"
        else:
            self._wake_enabled = False
            save_wake_word_enabled(False)
            self.wake(reason="слово пробуждения отключено")
            return "disabled"

    def _ui_wake_manual(self) -> None:
        """Кнопка ручного засыпания/пробуждения в UI."""
        if not self._wake_enabled:
            return
        if self._awake:
            self.sleep(reason="ты нажал «усыпить»")
        else:
            self.wake(reason="ты нажал «разбудить»")

    def _ui_wake_install(self) -> tuple[bool, str]:
        """Скачивает openwakeword + модель (выполняется в рабочем потоке UI)."""
        return wake_install(logger=lambda m: self.ui.write_log(f"SYS: {m}"))

    def plugin_say(self, instruction: str) -> None:
        """
        Потокобезопасный канал озвучки для плагинов: позволяет плагину попросить
        Анфису сказать что-то короткое, ПОКА его run() ещё выполняется (плагины
        блокируют свой поток исполнителя, так что через ответ инструмента они
        могут говорить, только закончив). Указание попадает в сессию Live ровно
        как проактивная реплика; Gemini формулирует его естественно на языке
        пользователя. Безмолвный no-op, когда сессия не подключена.
        """
        loop = getattr(self, "_loop", None)
        if not loop or not self.session:
            return

        async def _say():
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": instruction}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[PluginSay] {e}")

        try:
            asyncio.run_coroutine_threadsafe(_say(), loop)
        except Exception as e:
            print(f"[PluginSay] {e}")

    def request_reconnect(self, keep_context: bool = True, reason: str = ""):
        """Потокобезопасно: просит цикл запуска разобрать и пересобрать сессию Live.
        Вызывается из потока Qt. No-op, пока асинхронный цикл и событие
        переподключения не существуют.

        `keep_context=False` отбрасывает handle возобновления, и новая сессия
        начинается пустой — только для изменений, которые сервер не может применить
        к возобновлённой сессии."""
        loop = getattr(self, "_loop", None)
        ev   = self._reconnect_event
        self._reconnect_keep   = keep_context
        self._reconnect_reason = reason
        if loop and ev is not None:
            loop.call_soon_threadsafe(ev.set)

    def _on_voice_change(self):
        """Выбранный голос применён.

        Голос «запекается» в сессию в момент подключения, поэтому нужна пересборка.
        Она собирается БЕЗ handle возобновления намеренно: возобновление восстанавливает
        собственное состояние сессии на сервере, и разумное предположение — что вместе с
        ним восстанавливается и голос, что заставило бы выбор голоса выглядеть пустым
        действием. Потерять контекст здесь приемлемо, потому что смена голоса —
        обдуманный и редкий шаг; потерять его из-за потерянного пакета было нельзя."""
        self.request_reconnect(keep_context=False, reason="новый голос")

    def _on_audio_device_change(self):
        """Сменились микрофон или динамики. Оба потока открываются внутри TaskGroup
        сессии, поэтому пересобрать их можно только вместе с ней — но разговор
        сохраняется, и именно ради этого возобновление появилось раньше, чем эта
        возможность."""
        self.request_reconnect(keep_context=True, reason="аудио-устройство")

    async def _watch_reconnect(self):
        """Задача уровня сессии: когда запрошено добровольное переподключение, бросает
        сигнал, который раскручивает TaskGroup, чтобы цикл запуска пересобрал сессию."""
        assert self._reconnect_event is not None
        await self._reconnect_event.wait()
        self._reconnect_event.clear()
        keep   = self._reconnect_keep
        reason = getattr(self, "_reconnect_reason", "") or "настройки"
        self.ui.write_log(
            f"SYS: Применяю {reason} — переподключение"
            + ("..." if keep else " (начинаю новый разговор)...")
        )
        raise _ReconnectSignal(keep_context=keep)

    def _make_remote_key(self):
        """Вызывается из главного потока Qt, когда пользователь жмёт Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Дашборд недоступен. "
                "Выполни: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    def _on_text_command(self, text: str):
        if not self._loop or not self.session:
            return
        # Уважаем сон от слова пробуждения: набранная команда не должна получать ответ,
        # пока ассистент спит (ворота сна не только для микрофона). Сначала разбуди
        # словом «Привет Анфиса» или кнопкой WAKE NOW.
        if self._wake_enabled and not self._awake:
            self.ui.write_log("SYS: Я сплю — сначала скажи «Привет Анфиса» или нажми WAKE NOW.")
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def interrupt(self) -> None:
        """Останавливает Анфису на середине фразы: сливает очередь аудио и сразу открывает микрофон."""
        self._interrupted = True
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[Anfisa] ✋ Прерывание — {drained} аудио-чанков отброшено")
        self.set_speaking(False)
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Прервано — слушаю...")

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Сэр, инструмент {tool_name} выдал ошибку. {short}")

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # Загружаем кастомизацию из конфига
        try:
            _cfg = json.loads(open(API_CONFIG_PATH, encoding="utf-8").read())
            self._asst_name = (_cfg.get("assistant_name") or "Anfisa").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "Anfisa"
            _user_name = ""

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Сейчас: {time_str}\n"
            f"Используй это, чтобы считать точное время для напоминаний.\n\n"
        )

        # Инъекция личности — перекрывает любое имя, зашитое в prompt.txt
        _addr = (f"ADDRESS: Всегда называй пользователя '{_user_name}'."
                 if _user_name
                 else "ADDRESS: Обращайся к пользователю обычной вежливой формой "
                      "для старшего по статусу на том языке, на котором говоришь сейчас — "
                      "\"сэр\" в английском, её повседневным эквивалентом в любом "
                      "другом. Никаких архаичных или аристократических форм и никогда "
                      "форма из языка, отличного от того, на котором ты говоришь "
                      "этим предложением.")
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Тебя зовут {self._asst_name}. "
            f"Всегда называй себя {self._asst_name}.\n"
            f"{_addr}\n\n"
        )

        parts = [time_ctx, identity_ctx]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)

        cfg = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": (
                TOOL_DECLARATIONS
                + self._action_registry.get_tool_declarations()
                + self._plugin_registry.get_tool_declarations()
            )}],
            # Возвращаем handle, перехваченный из последнего обновления session_resumption.
            # `handle=None` — это ровно старое поведение (запрашивать handle, начинать
            # с чистого листа), поэтому первое подключение запуска не меняется.
            session_resumption=types.SessionResumptionConfig(
                handle=self._resume_handle
            ),
            # Сжатие скользящим окном: сессия не умирает от переполненного контекста —
            # Анфиса может вести один разговор часами
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=get_voice()
                    )
                )
            ),
        )
        if self._enhanced_live:
            # Проактивное аудио: Анфиса молчит, когда речь не обращена к ней
            # (болтовня на фоне, разговор с кем-то другим в комнате).
            # (Affective dialog убран: gemini-3.1-flash-live его не
            #  поддерживает, и на практике тон он надёжно не ловил.
            #  Чтобы вернуть на модели 2.5 с нативным аудио, добавь обратно:
            #  cfg["enable_affective_dialog"] = True )
            cfg["proactivity"] = types.ProactivityConfig(proactive_audio=True)
        return types.LiveConnectConfig(**cfg)

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[Anfisa] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Готово."

        try:
            if name == "recall_memory":
                # Локальный поиск по файлу: ни сети, ни второй модели. Нарочно вне
                # исполнителя — это обход словаря из нескольких сотен коротких строк,
                # и прыжок в поток стоит дороже самой работы.
                result = search_memory(args.get("query", ""), limit=8)

            elif name == "undo":
                if str(args.get("action", "")).lower().strip() == "list":
                    items = undo_stack.history()
                    result = ("Что я могу отменить, начиная с последнего:\n"
                              + "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
                              ) if items else "Я пока ничего не менял, что можно было бы отменить."
                else:
                    result = await loop.run_in_executor(None, undo_stack.undo_last)

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # секунд — перекрывает эхо после окончания речи
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Пауза активна (осталось {_wait:.1f} с) — повторный вызов игнорируется")
                    result = "Vision ещё обрабатывает предыдущий запрос. Я не буду вызывать это снова."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "Что ты видишь?")
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(None, _capture_camera)
                        self.ui.start_camera_stream()
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                        print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                        _stall = "screen"
                    self._pending_vision = (img_b, mime_t, user_text, angle)
                    result = (
                        f"[VISION_ACTIVE] Захват выполнен, режим — {_stall}. "
                        f"Сразу скажи ОДНО короткое естественное предложение на языке пользователя, "
                        f"сообщив, что ты смотришь на захваченное прямо сейчас. "
                        f"НЕ описывай и не угадывай содержимое — само изображение придёт в СЛЕДУЮЩЕМ сообщении."
                    )

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Камера закрыта."

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "manage_monitor":
                action = args.get("action", "").lower().strip()
                topic  = args.get("topic", "").strip()
                if action == "add" and topic:
                    result = await asyncio.to_thread(add_monitor, topic)
                elif action == "remove" and topic:
                    result = await asyncio.to_thread(remove_monitor, topic)
                elif action == "list":
                    topics = await asyncio.to_thread(list_monitors)
                    result = ("Наблюдение: " + ", ".join(topics)) if topics else "Тем наблюдения нет."
                else:
                    result = "Укажи действие (add/remove/list) и тему."

            elif name == "shutdown_Anfisa":
                self.ui.write_log("SYS: Запрошено завершение работы.")
                async def _do_shutdown():
                    await self._save_session_summary()
                    if self.session:
                        try:
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [{"text": "Скажи пользователю короткое естественное прощание."}]},
                                turn_complete=True,
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)
                    import os as _os
                    _os._exit(0)
                asyncio.create_task(_do_shutdown())

            elif self._action_registry.has(name):
                # file_processor: если файл не указан, берём тот, что загружен сейчас
                if name == "file_processor" and not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                _ctx = {"player": self.ui, "speak": self.speak,
                        "response": None, "session_memory": None}
                r = await loop.run_in_executor(None, lambda: self._action_registry.run(name, args, _ctx))
                result = r or "Готово."
                # web_search: дублируем результаты на экранный контент-панель
                if (name == "web_search" and r
                        and not r.startswith("No results")
                        and not r.startswith("Search failed")):
                    _mode  = args.get("mode", "search")
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)

            else:
                if self._plugin_registry.has(name):
                    r = await loop.run_in_executor(
                        None,
                        lambda: self._plugin_registry.run(name, args, player=self.ui, session_memory=None)
                    )
                    result = r or "Готово."
                else:
                    result = f"Неизвестный инструмент: {name}"

        except Exception as e:
            result = f"Инструмент '{name}' завершился с ошибкой: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[Anfisa] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            # Gemini 3.x Live отвергает прежнее поле realtime_input.media_chunks
            # (то, куда попадает `media=...`) и закрывает сокет кодом 1007. Поэтому
            # PCM с микрофона и с телефона шлём через новое поле `audio`. Элементы
            # очереди — это {"data": <bytes>, "mime_type": <str>} из _listen_audio
            # и из телефонного ретранслятора.
            await self.session.send_realtime_input(
                audio=types.Blob(
                    data=msg["data"],
                    mime_type=msg.get("mime_type", "audio/pcm"),
                )
            )

    async def _listen_audio(self):
        print("[Anfisa] 🎤 Микрофон запущен")
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            # ── Ворота слова пробуждения ─────────────────────────────────────
            # Пока я сплю, звук с микрофона НИКОГДА не уходит в Gemini (ничего не
            # транслируется, поэтому Anfisa не может отреагировать на речь, которая
            # к ней не обращена, и наружу с машины ничего не уходит). Вместо этого
            # кадры передаются локальному детектору, который гоняет свою модель в
            # СВОЁМ потоке — здесь цена только толчок в очередь, так что аудиотракт
            # никогда не тормозится. Когда слово пробуждения выключено (по
            # умолчанию) или я не сплю — это одна проверка булева флага.
            if self._wake_enabled and not self._awake:
                det = self._wake_detector
                if det is not None:
                    det.feed(indata)
                return
            with self._speaking_lock:
                Anfisa_speaking = self._is_speaking
            if not Anfisa_speaking and not self.ui.muted and not self._phone_active:
                data = indata.tobytes()
                loop.call_soon_threadsafe(
                    self.out_queue.put_nowait,
                    {"data": data, "mime_type": "audio/pcm"}
                )
                # Живой уровень с микрофона уходит на HUD, чтобы волновая форма
                # реагировала на настоящий голос пользователя во время
                # прослушивания. Чисто косметика — любая ошибка здесь ни в коем
                # случае не должна мешать микрофону.
                try:
                    self.ui.set_audio_level(_pcm_level(indata))
                except Exception:
                    pass

        try:
            def _open_mic(dev):
                return sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    device=dev,
                    callback=callback,
                )

            # Какой микрофон. resolve() возвращает None и для «системного по
            # умолчанию», и для сохранённого устройства, которого больше нет, —
            # поэтому гарнитура, отключённая после прошлого запуска, откатится на
            # встроенный микрофон вместо исключения на старте, которое утащило бы
            # за собой всю сессию.
            _mic_name = get_input_device()
            _mic_dev  = audio_devices.resolve(_mic_name, "input")
            if _mic_dev is not None:
                print(f"[Anfisa] 🎤 Устройство ввода: {_mic_name}")
            try:
                _mic_stream = _open_mic(_mic_dev)
            except Exception as _e:
                # Устройство, которое список показал, но драйвер прямо сейчас
                # открыть не даёт — эксклюзивный режим, уже занята веб-камера, у
                # виртуального микрофона исчез источник. Отказ выбранного
                # железа никогда не должен означать, что ассистент вообще не
                # слышит.
                if _mic_dev is None:
                    raise
                print(f"[Anfisa] ⚠️  Микрофон '{_mic_name}' не удался: {_e} — беру системный по умолчанию")
                self.ui.write_log(
                    f"SYS: Микрофон '{_mic_name}' недоступен — использую системный по умолчанию."
                )
                _mic_stream = _open_mic(None)

            with _mic_stream:
                print("[Anfisa] 🎤 Поток микрофона открыт")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[Anfisa] ❌ Микрофон: {e}")
            raise

    async def _receive_audio(self):
        print("[Anfisa] 👂 Приём запущен")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():

                    # ── Возобновление сессии ─────────────────────────────────
                    # Сервер присылает это периодически. Флаг `resumable` гаснет,
                    # пока ход ещё в полёте, — именно повторы хендла из такого
                    # момента он и предотвращает, — поэтому сохраняем только
                    # возобновляемые хендлы. Три строки кода — и это целиком вся
                    # починка от «после каждого переподключения всё забыто».
                    _sru = getattr(response, "session_resumption_update", None)
                    if _sru is not None:
                        if getattr(_sru, "resumable", False) and getattr(_sru, "new_handle", None):
                            if self._resume_handle is None:
                                print("[Anfisa] 🔗 Возобновление сессии подготовлено")
                            self._resume_handle = _sru.new_handle

                    if response.data:
                        if self._interrupted:
                            pass  # отбрасываем: прерывание
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Режем на чанки ~50 мс, чтобы interrupt() останавливал звук за 50 мс
                            # (24000 Гц × 2 байта/сэмпл × 0.05 с = 2400 байт на срез)
                            _audio_data = response.data
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt and txt != (out_buf[-1] if out_buf else ""):
                                out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # Если этот turn_complete завершает прерванный ответ,
                            # сбрасываем флаг и пропускаем всю дальнейшую обработку хода.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf  = []
                                out_buf = []
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self.ui.write_log(f"You: {full_in}")
                                self._session_log.append(f"User: {full_in}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            if full_out:
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "Anfisa",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            out_buf = []

                            # Вброс изображения: модель завершила ход с ответом
                            # инструмента → теперь отправляем картинку
                            if self._pending_vision and self.session:
                                import base64 as _b64
                                img_b, mime_t, question, angle = self._pending_vision
                                self._pending_vision = None
                                b64 = _b64.b64encode(img_b).decode("ascii")
                                print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")
                                await self.session.send_client_content(
                                    turns={"role": "user", "parts": [
                                        {"inline_data": {"mime_type": mime_t, "data": b64}},
                                        {"text": question},
                                    ]},
                                    turn_complete=True,
                                )
                                # Дальнейшее поведение Anfisa на следующем turn_complete зависит от ракурса
                                if self._vision_cam_active:
                                    # Камера: держим занятой, пока Anfisa договаривает ответ
                                    self._vision_cam_active    = False
                                    self._vision_close_pending = True
                                else:
                                    # Только экран: закрывать камеру не нужно, снимаем флаг занятости сейчас
                                    self._vision_busy = False
                            elif self._vision_close_pending:
                                # Этот turn_complete И ЕСТЬ ответ по зрению — закрываем камеру и снимаем флаг занятости
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[Anfisa] 📞 {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
        except Exception as e:
            print(f"[Anfisa] ❌ Приём: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[Anfisa] 🔊 Воспроизведение запущено")

        _spk_name = get_output_device()
        _spk_dev  = audio_devices.resolve(_spk_name, "output")
        if _spk_dev is not None:
            print(f"[Anfisa] 🔊 Устройство вывода: {_spk_name}")

        def _open_spk(dev):
            st = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=dev,
            )
            st.start()
            return st

        try:
            stream = _open_spk(_spk_dev)
        except Exception as _e:
            # Вывод, который хост-API принимает по имени, но отказывается
            # открыть (эксклюзивный режим, неподходящая частота дискретизации,
            # уснувшее устройство), не должен оставлять пользователя без голоса.
            # Откатываемся на устройство по умолчанию и говорим об этом.
            if _spk_dev is None:
                raise
            print(f"[Anfisa] ⚠️  Устройство вывода '{_spk_name}' не удалось открыть: {_e} — беру системное по умолчанию")
            self.ui.write_log(f"SYS: Устройство вывода '{_spk_name}' недоступно — использую системное по умолчанию.")
            stream = _open_spk(None)

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue

                self.set_speaking(True)

                # Batch all immediately-available chunks into one write to reduce
                # thread-pool round-trips (was one asyncio.to_thread per 50ms slice).
                # Cap at ~200 ms so interrupt() still stops audio within ~200 ms.
                batch = bytearray(chunk)
                while len(batch) < 9600:   # 9600 bytes ≈ 200 ms at 24 kHz / 16-bit mono
                    try:
                        batch.extend(self.audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                # Drive the HUD waveform from Anfisa's own voice while speaking.
                try:
                    self.ui.set_audio_level(_pcm_level(
                        np.frombuffer(bytes(batch), dtype=np.int16)))
                except Exception:
                    pass

                try:
                    await asyncio.to_thread(stream.write, bytes(batch))
                except (RuntimeError, asyncio.CancelledError):
                    break   # executor shutting down — exit cleanly
        except Exception as e:
            print(f"[Anfisa] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    # ── Morning briefing ────────────────────────────────────────────────────────

    async def _send_startup_briefing(self) -> None:
        """
        Two-phase briefing optimized for speed:
          Phase 1 — instant greeting (no tools) → speech starts in <1s
          Phase 2 — news pre-fetched in a background thread while Phase 1 plays,
                    delivered as ready text (no Gemini tool-call round-trip) and
                    shown on the UI content panel. Waits for turn_complete event
                    instead of a fixed sleep so there is no unnecessary gap.
        """
        memory   = load_memory()
        identity = memory.get("identity", {})

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = _val("language")
        name = _val("name")
        time_str = datetime.now().strftime("%H:%M")

        # Start fetching news immediately — runs in parallel while phase 1 plays
        loop = asyncio.get_event_loop()
        news_future = loop.run_in_executor(None, _fetch_news_sync, "top world news today")

        await asyncio.sleep(0.3)
        if not self.session:
            return

        # ── Phase 1: instant greeting ─────────────────────────────────────────
        # The briefing fires before the user has said anything, so the
        # remembered language is the only signal there is. It is a starting
        # point, not a setting: the moment they reply, their language wins.
        lang_clause = (f" Speak this greeting in {lang}, then follow the "
                       f"user's own language from their first reply onward."
                       if lang else "")
        name_clause = f" Address the user as {name}." if name else ""

        # Inject last session context if available — pop removes it so it's never repeated
        last = await asyncio.to_thread(pop_last_session)
        session_clause = ""
        if last:
            try:
                _delta = (datetime.now() - datetime.strptime(last["date"], "%Y-%m-%d")).days
                _when  = "earlier today" if _delta == 0 else ("yesterday" if _delta == 1 else f"{_delta} days ago")
            except Exception:
                _when = "last time"
            session_clause = (
                f" Also briefly and naturally mention that {_when}: {last['summary']}"
            )

        p1 = (
            f"Greet the user warmly, mention it is {time_str}, and say you are fetching today's news now.{session_clause} "
            f"Keep it to 2 short sentences max. Do not call any tools.{lang_clause}{name_clause}"
        )

        # Clear the turn-done event so we can wait for Phase 1 to finish
        if self._turn_done_event:
            self._turn_done_event.clear()

        await self.session.send_client_content(
            turns={"role": "user", "parts": [{"text": p1}]},
            turn_complete=True,
        )
        self.ui.write_log("SYS: Briefing phase 1 (greeting) sent.")

        # ── Phase 2: fire as soon as Phase 1 audio is done ───────────────────
        async def _deliver_news():
            try:
                lang_str = (f" Speak in {lang} unless the user has since "
                            f"spoken another language, in which case use theirs."
                            if lang else "")

                # Wait for news fetch (already running) and Phase 1 turn-complete
                # in parallel — whichever takes longer determines the wait time
                news_done   = asyncio.wrap_future(news_future)
                turn_waited = False
                if self._turn_done_event:
                    try:
                        await asyncio.wait_for(self._turn_done_event.wait(), timeout=6.0)
                        turn_waited = True
                    except asyncio.TimeoutError:
                        pass

                # Extra buffer: turn_complete fires when Gemini finishes *generating*
                # Phase 1, but audio may still be playing.  Waiting a beat here
                # prevents Phase 2 audio from arriving while Phase 1 is mid-sentence
                # (which sounds like a "repeated first response" to the user).
                if turn_waited:
                    await asyncio.sleep(0.8)
                else:
                    await asyncio.sleep(1.0)

                try:
                    news_text = await asyncio.wait_for(news_done, timeout=4.0)
                except Exception:
                    news_text = ""

                if not self.session:
                    return

                if news_text and len(news_text) > 60:
                    # Show on UI content panel immediately
                    self.ui.show_content("NEWS — top world news today", news_text)

                    p2 = (
                        f"[BRIEFING] Here are today's top news headlines:\n{news_text}\n\n"
                        "Pick ONE headline, summarise it in one sentence, then say the full list "
                        f"is displayed on screen. Do not call any tools.{lang_str}"
                    )
                else:
                    p2 = (
                        "News headlines could not be fetched right now. "
                        f"Let the user know briefly.{lang_str}"
                    )

                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": p2}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Briefing phase 2 (news) sent.")
            except Exception as e:
                print(f"[Briefing] Phase 2 error: {e}")
                self.ui.write_log(f"SYS: Briefing phase 2 failed: {e}")

        asyncio.create_task(_deliver_news())

    # ── Session memory ──────────────────────────────────────────────────────────

    async def _save_session_summary(self) -> None:
        """Summarise the current session in 1-2 sentences and save to long_term.json."""
        log = self._session_log
        if len(log) < 3:          # need at least one exchange to be worth saving
            return
        self._session_log = []    # reset immediately so the next session starts clean

        memory = load_memory()
        lang_entry = memory.get("identity", {}).get("language", {})
        lang = (lang_entry.get("value", "") if isinstance(lang_entry, dict) else str(lang_entry)).strip()
        lang = lang or "English"

        convo = "\n".join(log[-40:])   # cap at last 40 turns to stay within token budget
        prompt = (
            f"Summarize this conversation in 1-2 sentences in {lang}. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\n\n" + convo
        )
        try:
            from google import genai as _genai
            client = _genai.Client(api_key=_get_api_key())
            resp   = await asyncio.to_thread(
                client.models.generate_content,
                model="gemini-flash-latest",
                contents=prompt,
            )
            summary = (resp.text or "").strip()
            if summary:
                save_session_summary(summary, lang)
        except Exception as e:
            print(f"[Memory] ⚠️ Session summary failed: {e}")

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if not alert or not self.session or not self._awake:
                continue
            # Don't interrupt an active conversation
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": alert}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Background monitor ──────────────────────────────────────────────────────

    async def _run_background_monitor(self) -> None:
        """Check user-configured topics once per day; speak alerts when new headlines appear."""
        await asyncio.sleep(300)          # wait 5 min after startup before first check
        while True:
            if self.session and self._awake:
                # Don't interrupt if user spoke recently or Anfisa is mid-sentence
                with self._speaking_lock:
                    speaking = self._is_speaking
                recent_speech = (time.monotonic() - self._last_user_speech) < 30
                if not speaking and not recent_speech:
                    try:
                        alerts = await asyncio.to_thread(monitor_check_all)
                        memory = load_memory()
                        lang_e = memory.get("identity", {}).get("language", {})
                        lang   = (lang_e.get("value", "") if isinstance(lang_e, dict) else str(lang_e)).strip() or "English"
                        for alert in alerts:
                            msg = (
                                f"{alert}\n\n"
                                f"Inform the user about this development naturally in {lang}. "
                                "One brief sentence only."
                            )
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [{"text": msg}]},
                                turn_complete=True,
                            )
                            self.ui.write_log(f"SYS: Monitor alert sent.")
                            await asyncio.sleep(6)   # gap between consecutive alerts
                    except Exception as e:
                        print(f"[Monitor] ⚠️ Background check error: {e}")
            await asyncio.sleep(1800)     # check every 30 minutes

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session or not self._awake:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory       = await asyncio.to_thread(load_memory)
                monitors     = await asyncio.to_thread(list_monitors)
                recent_turns = self._session_log[-8:] if self._session_log else []
                prompt = self._proactive.build_prompt(
                    memory       = memory,
                    monitors     = monitors or None,
                    recent_turns = recent_turns or None,
                )
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    # ── Phone audio relay ────────────────────────────────────────────────────────

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No audio for 1 s → phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            self._phone_active = True   # phone is streaming — silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    # A remote command is deliberate control and the phone user
                    # has no desktop WAKE button — so it wakes Anfisa if asleep.
                    if self._wake_enabled and not self._awake:
                        self.wake(reason="remote command")
                    await self.session.send_client_content(
                        turns={"role": "user", "parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()
        self._reconnect_event = asyncio.Event()

        # ── Wire the shared core services to the interface ───────────────────
        # The confirmation gate is useless without a way to ask, and a memory
        # trim is invisible without a way to say so. Both are bound once here
        # rather than passed down through every action signature.
        confirm_gate.bind(
            show = self.ui.show_confirm,
            hide = self.ui.hide_confirm,
            log  = self.ui.write_log,
        )
        set_trim_notifier(self.ui.write_log)

        # Tell the device picker the exact rates the streams open at, from the
        # constants that actually open them — so it can never list a device that
        # cannot be opened at them.
        audio_devices.configure(SEND_SAMPLE_RATE, RECEIVE_SAMPLE_RATE)

        # Enumerate audio devices off-thread. The settings drawer must never pay
        # for host-API enumeration on the Qt thread.
        audio_devices.prefetch()

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            asyncio.create_task(self._dashboard.serve())
            # Runs for the whole lifetime, not just inside an active session
            asyncio.create_task(self._process_dashboard_commands())
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        while True:
            try:
                print("[Anfisa] Connecting...")
                self.ui.set_state("THINKING")
                _resumed_with = self._resume_handle is not None
                config = self._build_config()

                # Fresh client on every reconnect — avoids stale HTTP session state
                # v1alpha carries proactive audio; if it gets rejected we fall
                # back to v1beta.
                client = genai.Client(
                    api_key=_get_api_key(),
                    http_options={"api_version": "v1alpha" if self._enhanced_live else "v1beta"}
                )

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False

                    print("[Anfisa] Connected.")
                    if _resumed_with:
                        # Say it plainly: the difference between "it reconnected"
                        # and "it reconnected and still knows what we were doing"
                        # is the whole point, and it is invisible otherwise.
                        self.ui.write_log("SYS: Reconnected — conversation restored.")

                    # Wake word: if enabled, come up ASLEEP (mic gated, silent)
                    # until the user says "Привет Анфиса" or taps wake in the UI.
                    if self._wake_enabled:
                        self._ensure_wake_detector()
                        self._awake = False
                        self.ui.set_state("SLEEPING")
                        self.ui.write_log("SYS: Anfisa online — sleeping. Say 'Привет Анфиса' to wake me.")
                    else:
                        self._awake = True
                        self.ui.set_state("LISTENING")
                        self.ui.write_log("SYS: Anfisa online.")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    self._reconnect_event.clear()  # ignore requests from before this session
                    tg.create_task(self._watch_reconnect())
                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_background_monitor())
                    tg.create_task(self._run_proactive_mode())
                    tg.create_task(self._run_sleep_watch())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    # Morning briefing — fires once per process launch (if enabled).
                    # Skipped in wake-word mode: it comes up asleep, and a briefing
                    # would mean talking while "asleep".
                    if not self._briefing_sent and get_brief_enabled() and self._awake:
                        self._briefing_sent = True
                        tg.create_task(self._send_startup_briefing())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                # Voluntary reconnect (voice change) — not an error. Rebuild the
                # session immediately with no backoff and no scary logs.
                if _is_reconnect_signal(e):
                    print("[Anfisa] Voluntary reconnect requested.")
                    if not _keep_context_of(e):
                        # A deliberate clean slate (voice change) — drop the
                        # handle so the next connect really does start empty.
                        self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                # A resumption handle the server will not accept — expired, or
                # belonging to a session it has since dropped. Without this, the
                # same dead handle would be replayed on every retry and the
                # assistant would never come back at all: the feature meant to
                # survive a reconnect would be the thing preventing one. Drop it
                # once and let the next attempt start clean.
                if _resumed_with and (
                    "resum" in str(e).lower()
                    or "handle" in str(e).lower()
                    or "INVALID_ARGUMENT" in str(e)
                    or "NOT_FOUND" in str(e)
                ):
                    print("[Anfisa] 🔗 Resumption handle rejected — starting a fresh session")
                    self.ui.write_log("SYS: Could not restore the conversation — starting fresh.")
                    self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                err_str = str(e)
                print(f"[Anfisa] Error ({type(e).__name__}): {e}")
                traceback.print_exc()

                # Proactive audio rejected by the server (preview API drift) —
                # drop it and reconnect with the plain config.
                if self._enhanced_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "proactiv" in err_str.lower()
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                ):
                    self._enhanced_live = False
                    self.ui.write_log(
                        "SYS: Proactive audio unavailable — reconnecting without it."
                    )
                    continue

                # Invalid API key — stop hammering the API, prompt re-configuration
                if "API key not valid" in err_str or "1007" in err_str:
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[Anfisa] New API key saved — reconnecting...")
                    _conn_backoff = 3
                    continue

                # Network / timeout errors — log clearly and back off
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_net_err:
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        f"NET: Connection failed — retrying in {_conn_backoff}s. "
                        "(a VPN may be required)"
                    )
                else:
                    self._conn_backoff = 3
            finally:
                self.session = None
                # Only save if there was a real conversation (≥3 turns)
                if len(self._session_log) >= 3:
                    asyncio.create_task(self._save_session_summary())

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[Anfisa] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

def main():
    ui = AnfisaUI("face.png")

    def runner():
        ui.wait_for_api_key()
        Anfisa = AnfisaLive(ui)
        try:
            asyncio.run(Anfisa.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()