"""
Локальное распознавание слова-активатора для «Анфисы» («Привет Анфиса»).

Проектные цели:
  • НУЛЕВАЯ стоимость, когда функция выключена: openwakeword импортируется
    ТОЛЬКО внутри start() и вспомогательных функций установки, никогда при
    импорте модуля. Если пользователь не включает слово-активатор, этот код
    приложение не затрагивает.
  • НУЛЕВАЯ задержка на аудиотраектории: колбэк микрофона выполняет только
    дешёвую неблокирующую запись в очередь (feed()); сам инференс модели
    работает в отдельном фоновом потоке модуля, поэтому аудиопоток реального
    времени и стрим Gemini никогда не замедляются.
  • Полностью локально и офлайн: аудио отсюда никогда не уходит с машины;
    единственный сетевой запрос — одноразовая загрузка модели, которую
    запускает сам пользователь из интерфейса.

openwakeword поставляет небольшие ONNX-модели (по несколько МБ каждая) и
уверенно работает на CPU. Предобученная фраза активатора, которая здесь
используется, — «Привет Анфиса».
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

# Предобученная модель openwakeword, слушающая «Привет Анфиса».
WAKE_MODEL = "hey_Anfisa"
# Оценка в диапазоне [0,1]; значение выше неё засчитывается как обнаружение.
# Подстраивается под конкретное окружение.
DEFAULT_THRESHOLD = 0.5
# Кадры с микрофона приходят как int16 с частотой 16 кГц; это просто входная
# частота детектора.
SAMPLE_RATE = 16000


def is_installed() -> bool:
    """Истина, если пакет openwakeword можно импортировать (наличие модели не проверяется)."""
    try:
        import importlib.util
        return importlib.util.find_spec("openwakeword") is not None
    except Exception:
        return False


def is_ready() -> bool:
    """Истина, если openwakeword установлен И файлы моделей есть на диске.

    Это дешёвая ДЕТЕРМИНИРОВАННАЯ проверка существования файлов. Она намеренно
    НЕ создаёт Model ради зондирования готовности — это медленно и, что хуже,
    может конфликтовать с собственным Model детектора, когда тот уже работает;
    из-за чего иногда возвращалось False, и интерфейс мигал статусом
    «модель не загружена». Никогда не бросает исключений.
    """
    if not is_installed():
        return False
    try:
        import openwakeword
        models_dir = Path(openwakeword.__file__).resolve().parent / "resources" / "models"
        if not models_dir.is_dir():
            return False
        has_wake = (any(models_dir.glob(f"{WAKE_MODEL}*.onnx"))
                    or any(models_dir.glob(f"{WAKE_MODEL}*.tflite")))
        has_mel = (any(models_dir.glob("melspectrogram*.onnx"))
                   or any(models_dir.glob("melspectrogram*.tflite")))
        has_emb = (any(models_dir.glob("embedding_model*.onnx"))
                   or any(models_dir.glob("embedding_model*.tflite")))
        return bool(has_wake and has_mel and has_emb)
    except Exception:
        return False


def install_and_download(logger: Callable[[str], None] = print) -> tuple[bool, str]:
    """
    Установка в один клик для кнопки в интерфейсе: при отсутствии ставит
    openwakeword через pip, затем скачивает модель активатора.
    Возвращает (ok, message). Никогда не бросает исключений — любой сбой
    сообщается через возвращаемое сообщение и через logger.
    """
    try:
        if not is_installed():
            logger("Слово-активатор: устанавливаю openwakeword (однократно)…")
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "openwakeword"],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
                return False, f"pip install не удался: {tail[0][:160]}"
        # Скачиваем предобученные melspectrogram/embedding и модель активатора.
        logger("Слово-активатор: скачиваю модели…")
        try:
            import openwakeword.utils as _u
            try:
                _u.download_models([WAKE_MODEL])
            except TypeError:
                _u.download_models()   # старая сигнатура скачивает набор по умолчанию
        except Exception as e:
            return False, f"не удалось скачать модель: {e}"

        if not is_ready():
            return False, "установлено, но модель активатора загрузить не удалось."
        logger("Слово-активатор: готово.")
        return True, "Слово-активатор установлен и готов к работе."
    except Exception as e:
        return False, f"ошибка настройки: {e}"


class WakeWordDetector:
    """
    Запускает модель активатора в выделенном потоке. Поток микрофона передаёт
    сюда сырые int16-кадры через feed(); при обнаружении вызывается on_detect()
    (из этого потока — колбэк должен сам передать управление нужному ему
    циклу событий или интерфейсу).
    """

    def __init__(self, on_detect: Callable[[], None],
                 threshold: float = DEFAULT_THRESHOLD,
                 logger: Callable[[str], None] = print):
        self._on_detect = on_detect
        self._threshold = threshold
        self._logger    = logger
        self._queue: queue.Queue = queue.Queue(maxsize=50)
        self._thread: threading.Thread | None = None
        self._running = False
        self._model = None
        self._ready = False

    def start(self) -> bool:
        """Загружает модель и запускает поток инференса. True при успехе.
        Можно вызывать повторно — ничего не делает, если уже запущено.
        Никогда не бросает исключений."""
        if self._running:
            return True
        try:
            from openwakeword.model import Model
            self._model = Model(wakeword_models=[WAKE_MODEL], inference_framework="onnx")
        except Exception as e:
            self._logger(f"Слово-активатор: не удалось загрузить модель — {e}")
            self._model = None
            return False
        self._running = True
        self._ready = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="WakeWordThread")
        self._thread.start()
        self._logger("Слово-активатор: слушаю «Привет Анфиса».")
        return True

    def stop(self) -> None:
        self._running = False
        # разблокируем поток, если он ожидает на очереди
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        self._model = None
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    def feed(self, frame_int16) -> None:
        """Вызывается из колбэка микрофона (поток реального времени). Должен
        оставаться дешёвым и никогда не блокироваться — кадр копируется и
        отбрасывается, если очередь переполнена."""
        if not self._running:
            return
        try:
            # frame_int16 — numpy-массив int16 (возможно, 2-D моно) — сводим к 1-D
            data = frame_int16[:, 0].copy() if getattr(frame_int16, "ndim", 1) > 1 else frame_int16.copy()
            self._queue.put_nowait(data)
        except queue.Full:
            pass
        except Exception:
            pass

    def _loop(self) -> None:
        import numpy as np
        while self._running:
            try:
                frame = self._queue.get()
                if frame is None or not self._running:
                    break
                scores = self._model.predict(np.asarray(frame, dtype=np.int16))
                score = 0.0
                if isinstance(scores, dict):
                    # ищем нужную модель независимо от точного суффикса ключа
                    for k, v in scores.items():
                        if "Anfisa" in k.lower():
                            score = max(score, float(v))
                    if score == 0.0 and scores:
                        score = max(float(v) for v in scores.values())
                if score >= self._threshold:
                    # сливаем накопленную очередь, чтобы не сработать дважды на одной фразе
                    self._drain()
                    try:
                        self._on_detect()
                    except Exception as e:
                        self._logger(f"Слово-активатор: ошибка on_detect — {e}")
            except Exception as e:
                self._logger(f"Слово-активатор: ошибка инференса — {e}")

    def _drain(self) -> None:
        try:
            while True:
                self._queue.get_nowait()
        except Exception:
            pass
