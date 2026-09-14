"""
Обнаружение, валидация и диспетчеризация действий — встроенный близнец plugin_loader.

Каждый файл actions/*.py, который объявляет модулевой dict ``TOOL``,
автоматически обнаруживается здесь — ровно как подключаемый плагин, — поэтому
main.py не должен хардкодить ни декларацию инструмента, ни ветку его диспетчеризации.
Добавить новое встроенное действие — та же операция из одного файла, что и
написать плагин: объявить ``TOOL`` и обработчик.

Форма ``TOOL`` (живой пример — actions/open_app.py):

    TOOL = {
        "name":        "open_app",              # уникальный, ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$
        "description":  "...",                   # что читает Gemini, чтобы направить вызов
        "parameters":  {"type": "OBJECT", ...}, # схема объявления функции для Gemini
        "handler":      open_app,                # вызываемый объект, который запускается
    }

Обработчик вызывается через интроспекцию сигнатуры: он получает ``parameters``
плюс те из ``player`` / ``speak`` / ``response`` / ``session_memory``, которые
фактически объявлены, — поэтому существующие сигнатуры действий работают без изменений.

Обнаружение выполняется один раз при запуске; ошибки импорта, ошибки валидации и
коллизии имён логируются, а проблемный файл пропускается — они НИКОГДА не
выбрасываются наружу из discover_actions() и не прерывают обход остальных файлов.
"""
from __future__ import annotations

import importlib.util
import inspect
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_DEFAULT_PARAMS = {"type": "OBJECT", "properties": {}}
_CTX_KEYS = ("player", "speak", "response", "session_memory")


@dataclass
class ActionRecord:
    name: str
    description: str = ""
    parameters: dict = field(default_factory=lambda: dict(_DEFAULT_PARAMS))
    handler: Optional[Callable] = None
    file: str = ""
    valid: bool = False
    error: str = ""


class ActionRegistry:
    def __init__(self, actions: dict[str, ActionRecord], logger: Callable[[str], None]):
        self._actions = actions          # name -> ActionRecord, только ВАЛИДНЫЕ записи
        self._all_records: list[ActionRecord] = []
        self._logger = logger

    # -- вызывается main.py при построении LiveConnectConfig --
    def get_tool_declarations(self) -> list[dict]:
        return [
            {"name": rec.name, "description": rec.description, "parameters": rec.parameters}
            for rec in self._actions.values()
        ]

    def has(self, name: str) -> bool:
        return name in self._actions

    def names(self) -> set[str]:
        return set(self._actions.keys())

    # -- вызывается main.py из _execute_tool --
    def run(self, name: str, parameters: dict, ctx: dict | None = None) -> str:
        rec = self._actions.get(name)
        if rec is None or not rec.valid:
            return f"Действие '{name}' недоступно."
        try:
            return _call_handler(rec.handler, parameters, ctx or {}) or "Готово."
        except Exception as e:
            self._logger(f"Действие '{name}' упало во время run(): {e}")
            traceback.print_exc()
            return f"Инструмент '{name}' упал: {e}"


def _call_handler(fn: Callable, parameters: dict, ctx: dict) -> str:
    """Вызвать обработчик, передав только те kwargs контекста, которые он
    действительно объявляет (либо все, если у него есть **kwargs), чтобы
    существующая сигнатура каждого действия работала без изменений."""
    sig = inspect.signature(fn)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    kwargs = {}
    for key in _CTX_KEYS:
        if has_var_kw or key in sig.parameters:
            kwargs[key] = ctx.get(key)
    return fn(parameters=parameters, **kwargs)


def _validate(module, filename: str) -> ActionRecord:
    """Вернуть ActionRecord; .valid=False + .error установлен при любой проблеме. Никогда не бросает исключение."""
    tool = getattr(module, "TOOL", None)
    if not isinstance(tool, dict):
        return ActionRecord(name=Path(filename).stem, file=filename,
                            error="В модуле нет dict уровня модуля TOOL (не открываемое действие).")

    name = tool.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return ActionRecord(name=str(name or Path(filename).stem), file=filename,
                            error="TOOL['name'] отсутствует или не валидный идентификатор.")

    description = tool.get("description")
    if not isinstance(description, str) or not description.strip():
        return ActionRecord(name=name, file=filename,
                            error="TOOL['description'] отсутствует или пуст.")

    parameters = tool.get("parameters", _DEFAULT_PARAMS)
    if not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
        return ActionRecord(name=name, file=filename,
                            error="TOOL['parameters'] должен быть dict с \"type\": \"OBJECT\".")

    handler = tool.get("handler")
    if not callable(handler):
        return ActionRecord(name=name, file=filename,
                            error="TOOL['handler'] отсутствует или не callable.")

    return ActionRecord(name=name, description=description.strip(), parameters=parameters,
                        handler=handler, file=filename, valid=True, error="")


def discover_actions(actions_dir: Path, reserved_names: set[str] | None = None,
                     logger: Callable[[str], None] = print) -> ActionRegistry:
    """
    Сканирует actions_dir по файлам *.py (пропускает файлы, начинающиеся с '_').
    Файл рассматривается как действие, только если он объявляет модульный TOOL dict;
    файлы без него (вспомогательные модули, capture-only) тихо игнорируются.
    Ошибки импорта, валидации и коллизии имён логируются, файл пропускается —
    они НИКОГДА не выбрасываются наружу из этой функции.
    """
    reserved = reserved_names or set()
    actions_dir.mkdir(parents=True, exist_ok=True)
    valid: dict[str, ActionRecord] = {}
    all_records: list[ActionRecord] = []

    files = sorted(actions_dir.glob("*.py"), key=lambda p: p.name)  # детерминируемый порядок
    for path in files:
        if path.name.startswith("_"):
            continue
        try:
            module_name = f"actions.{path.stem}"
            # Повторно используем уже импортированный модуль, если он уже есть.
            module = sys.modules.get(module_name)
            if module is None:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    raise ImportError("не удалось построить spec для импорта модуля")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                try:
                    spec.loader.exec_module(module)
                except Exception:
                    sys.modules.pop(module_name, None)
                    raise

            if getattr(module, "TOOL", None) is None:
                continue   # не файл действия — вспомогательный/capture-only модуль

            rec = _validate(module, path.name)

            if rec.valid and rec.name in reserved:
                rec = ActionRecord(name=rec.name, file=path.name,
                                   error=f"Имя '{rec.name}' коллизирует с зарезервированным core-инструментом — отклонено.")
            elif rec.valid and rec.name in valid:
                other = valid[rec.name].file
                rec = ActionRecord(name=rec.name, file=path.name,
                                   error=f"Имя '{rec.name}' уже используется действием '{other}' — отклонено.")

        except Exception as e:
            rec = ActionRecord(name=path.stem, file=path.name,
                               error=f"Не удалось загрузить: {e}")
            traceback.print_exc()

        all_records.append(rec)
        if rec.valid:
            valid[rec.name] = rec
            logger(f"Действие загружено: {rec.name} ({path.name})")
        else:
            # Отказ логируем только если файл реально претендовал на роль действия.
            logger(f"Действие отклонено: {path.name} — {rec.error}")

    registry = ActionRegistry(valid, logger)
    registry._all_records = all_records
    logger(f"Обнаружение действий завершено: активно {len(valid)}.")
    return registry
