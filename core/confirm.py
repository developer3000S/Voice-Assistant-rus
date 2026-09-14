"""
core/confirm.py — подтверждение, которое модель не может подделать.

ЧЕМ ПЛОХА БЫЛА СТАРАЯ ЗАЩИТА
    computer_settings закрывал shutdown и restart так:

        confirmed = str(params.get("confirmed", "")).lower()
        if confirmed not in ("yes", "true", "1", "confirm"):
            return "Please confirm by calling again with confirmed=yes."

    `confirmed` — это параметр инструмента, а значит пишет его *модель*. Ничто не
    мешает ей прислать confirmed=yes уже в первом вызове, и ничто не проверяет,
    что вообще был человек. Это договорённость, а не шлюз — и прикрывал он два
    действия, так что удаление файлов и выключение WiFi, поверх которого
    ассистент разговаривает, проходили вообще без всякой защиты.

КАК УСТРОЕНО ЗДЕСЬ
    Токен подтверждения выдаёт *интерфейс*, никогда — модель:

      1. Действие вызывает `request(...)` с callable, который делает настоящую работу.
      2. Этот модуль отдаёт в UI баннер с CONFIRM / CANCEL и МГНОВЕННО возвращает
         фразу, которую модели надо сказать вслух.
      3. Если — и только если — пользователь нажимает CONFIRM, UI вызывает
         `resolve()`, который выполняет сохранённый callable вне Qt-потока.

    Ничего не блокируется. Модель продолжает говорить, пока висит баннер, поэтому
    задержка нулевая; более того, это дешевле старой защиты, которая на каждом
    shutdown сжигала два круговых пути инструмента (отказ, затем повторный вызов).

ЧТО ЗДЕСЬ К МЕСТУ, А ЧТО НЕТ
    Только по-настоящему необратимое. Всё, что можно отменить, надо делать сразу
    и складывать в core/undo.py — отмена быстрее вопроса, а ассистент, который
    спрашивает перед каждым действием, никому не нужен.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

# Незавершённое подтверждение сбрасывается через столько секунд. Столько
# выбрано, чтобы пережить обычную паузу «подожди, я посмотрю на экран», но не
# оставить живую кнопку выключения на HUD на весь остаток дня.
TIMEOUT_SECONDS = 90.0


@dataclass
class _Pending:
    key:     str
    title:   str
    detail:  str
    run:     Callable[[], str]
    at:      float


_pending: Optional[_Pending] = None
_lock = threading.Lock()

# Устанавливается один раз при запуске в main.py. Сигнатура: (title, detail) -> None
# для показа и () -> None для скрытия. Оба переносятся в Qt-поток средствами UI.
_show_cb: Optional[Callable[[str, str], None]] = None
_hide_cb: Optional[Callable[[], None]] = None
_log_cb:  Optional[Callable[[str], None]] = None


def bind(show, hide, log=None) -> None:
    """Подключить этот модуль к HUD. Вызывается один раз из main.py при запуске."""
    global _show_cb, _hide_cb, _log_cb
    _show_cb, _hide_cb, _log_cb = show, hide, log


def _log(msg: str) -> None:
    if _log_cb:
        try:
            _log_cb(msg)
        except Exception:
            pass


def request(key: str, title: str, detail: str, run: Callable[[], str]) -> str:
    """Поставить необратимое действие за экранный шлюз.

    Возвращает фразу, которую инструмент должен вернуть модели — она сформулирована
    как указание, чтобы ассистент попросил пользователя вслух на его языке,
    а не зачитывал английскую строку дословно."""
    global _pending

    if _show_cb is None:
        # Интерфейс не подключён (headless либо очень ранний вызов). Отказываем,
        # а не молча выполняем то, что необратимо.
        return (f"Сэр, я не могу сейчас запросить подтверждение «{title}»: интерфейс "
                f"недоступен, поэтому я ничего не делала.")

    with _lock:
        _pending = _Pending(key=key, title=title, detail=detail,
                            run=run, at=time.monotonic())

    try:
        _show_cb(title, detail)
    except Exception as e:
        with _lock:
            _pending = None
        return f"Не удалось запросить подтверждение: {e}. Ничего не выполнено."

    _log(f"SYS: Ожидание подтверждения — {title}")
    return (
        f"[CONFIRMATION_PENDING] Я вывела подтверждение на экран для: {title}. "
        f"Скажи ОДНО короткое предложение на языке пользователя, попросив его "
        f"подтвердить на HUD, прежде чем это будет сделано. Не утверждай, что уже готово."
    )


def resolve(accepted: bool) -> None:
    """Вызывается UI, когда пользователь нажимает CONFIRM или CANCEL.

    Выполняет сохранённый callable в рабочем потоке — вызов идёт из Qt-потока,
    а выключать машину из обработчика кнопки означало бы заморозить интерфейс
    на выходе."""
    global _pending

    with _lock:
        p, _pending = _pending, None

    if _hide_cb:
        try:
            _hide_cb()
        except Exception:
            pass

    if p is None:
        return

    if time.monotonic() - p.at > TIMEOUT_SECONDS:
        _log(f"SYS: Подтверждение устарело — {p.title}")
        return

    if not accepted:
        _log(f"SYS: Отменено — {p.title}")
        return

    def _worker():
        try:
            result = p.run() or "Готово."
            _log(f"SYS: Подтверждено — {p.title}. {result}")
        except Exception as e:
            _log(f"ERR: {p.title} не выполнено — {e}")

    threading.Thread(target=_worker, daemon=True,
                     name=f"confirm-{p.key}").start()


def pending_title() -> str:
    """'' если ничего не ожидает. Позволяет действию не накладывать два баннера."""
    with _lock:
        if _pending is None:
            return ""
        if time.monotonic() - _pending.at > TIMEOUT_SECONDS:
            return ""
        return _pending.title
