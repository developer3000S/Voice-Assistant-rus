"""
Обнаружение плагинов, валидация, детекция коллизий и диспетчеризация.

Обнаружение выполняется один раз (AnfisaLive.__init__ вызывает discover_plugins());
результирующий PluginRegistry кэшируется на время жизни процесса. Состояние
включено/выключено перечитывается из конфига при каждом вызове
get_tool_declarations() / run() / list_for_ui(), поэтому переключение плагина
не требует перезапуска приложения или повторного импорта.
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

from memory.config_manager import get_plugin_enabled, get_plugin_config

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_DEFAULT_PARAMS = {"type": "OBJECT", "properties": {}}


@dataclass
class PluginRecord:
    name: str
    description: str = ""
    parameters: dict = field(default_factory=lambda: dict(_DEFAULT_PARAMS))
    run: Optional[Callable] = None
    file: str = ""
    valid: bool = False
    error: str = ""
    settings: Optional[dict] = None   # необязательная схема PLUGIN_SETTINGS (поля конфига)


class PluginRegistry:
    def __init__(self, plugins: dict[str, PluginRecord], logger: Callable[[str], None]):
        self._plugins = plugins          # name -> PluginRecord, только ВАЛИДНЫЕ записи
        self._all_records: list[PluginRecord] = []   # валидные + невалидные, для списка в UI
        self._logger = logger

    # -- вызывается main.py при построении LiveConnectConfig --
    def get_tool_declarations(self) -> list[dict]:
        decls = []
        for name, rec in self._plugins.items():
            if get_plugin_enabled(name):
                decls.append({
                    "name": rec.name,
                    "description": rec.description,
                    "parameters": rec.parameters,
                })
        return decls

    def has(self, name: str) -> bool:
        return name in self._plugins

    # -- вызывается main.py из ветки else _execute_tool --
    def run(self, name: str, parameters: dict, player=None, session_memory=None) -> str:
        rec = self._plugins.get(name)
        if rec is None or not rec.valid:
            return f"Плагин '{name}' недоступен."
        if not get_plugin_enabled(name):
            return f"Плагин '{name}' в данный момент отключён."
        try:
            return _call_run(rec.run, parameters, player, session_memory) or "Готово."
        except Exception as e:
            self._logger(f"Плагин '{name}' упал во время run(): {e}")
            traceback.print_exc()
            return f"Сэр, плагин '{name}' упал: {e}"

    # -- вызывается вкладкой настроек ui.py, чтобы отрисовать формы конфига --
    def settings_schemas(self) -> list[dict]:
        """По одному элементу на РАЗДЕЛ настроек — только для включённых плагинов,
        объявивших схему PLUGIN_SETTINGS. Разделы дедуплицируются по namespace,
        чтобы связка плагинов с общим пространством имён (например, троица
        принтеров) показала одну форму. Уже сохранённые значения подмешиваются,
        поэтому UI может предзаполнить поля.
        """
        seen: set[str] = set()
        out: list[dict] = []
        for name, rec in self._plugins.items():
            if not rec.settings or not get_plugin_enabled(name):
                continue
            ns = rec.settings.get("namespace") or rec.name
            if ns in seen:
                continue
            seen.add(ns)
            out.append({
                "plugin":    rec.name,
                "namespace": ns,
                "title":     rec.settings.get("title") or rec.name,
                "fields":    rec.settings.get("fields", []),
                "values":    get_plugin_config(ns),
                "action":    rec.settings.get("action"),   # необязательная кнопка проверки/подключения
            })
        return out

    # -- вызывается оверлеем «Менеджер плагинов» в ui.py --
    def list_for_ui(self) -> list[dict]:
        out = []
        for rec in self._all_records:
            out.append({
                "name": rec.name,
                "description": rec.description,
                "file": rec.file,
                "valid": rec.valid,
                "error": rec.error,
                "enabled": get_plugin_enabled(rec.name) if rec.valid else False,
            })
        return out


def _call_run(run_fn, parameters, player, session_memory):
    """Вызвать run(), передав только те kwargs, которые оно действительно объявляет
    (либо все, если есть **kwargs), чтобы работал и минимальный плагин вида
    `def run(parameters):`."""
    sig = inspect.signature(run_fn)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    kwargs = {}
    if has_var_kw or "player" in sig.parameters:
        kwargs["player"] = player
    if has_var_kw or "session_memory" in sig.parameters:
        kwargs["session_memory"] = session_memory
    return run_fn(parameters, **kwargs)


def _validate(module, filename: str) -> PluginRecord:
    """Вернуть PluginRecord; .valid=False + .error установлен при любой проблеме. Никогда не бросает исключение."""
    plugin_meta = getattr(module, "PLUGIN", None)
    if not isinstance(plugin_meta, dict):
        return PluginRecord(name=Path(filename).stem, file=filename,
                             error="В модуле нет dict уровня модуля PLUGIN (не открываемый плагин).")

    name = plugin_meta.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return PluginRecord(name=str(name or Path(filename).stem), file=filename,
                             error="PLUGIN['name'] отсутствует или не валидный идентификатор "
                                   "(буквы/цифры/подчёркивание, должно начинаться с буквы или подчёркивания).")

    description = plugin_meta.get("description")
    if not isinstance(description, str) or not description.strip():
        return PluginRecord(name=name, file=filename,
                             error="PLUGIN['description'] отсутствует или пуст.")

    parameters = plugin_meta.get("parameters", _DEFAULT_PARAMS)
    if not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
        return PluginRecord(name=name, file=filename,
                             error="PLUGIN['parameters'] должен быть dict с \"type\": \"OBJECT\".")

    run_fn = getattr(module, "run", None)
    if not callable(run_fn):
        return PluginRecord(name=name, file=filename,
                             error="Нет вызываемой функции run(parameters, ...).")

    # Необязательная самодокументируемая схема настроек (отрисовывается UI настроек).
    # Некорректная схема игнорируется и не фатальна — плагин всё равно загрузится.
    settings = getattr(module, "PLUGIN_SETTINGS", None)
    if not (isinstance(settings, dict) and isinstance(settings.get("fields"), list)):
        settings = None

    return PluginRecord(name=name, description=description.strip(), parameters=parameters,
                         run=run_fn, file=filename, valid=True, error="", settings=settings)


def discover_plugins(plugins_dir: Path, core_tool_names: set[str],
                      logger: Callable[[str], None] = print) -> PluginRegistry:
    """
    Сканирует plugins_dir по файлам *.py (пропускает файлы, начинающиеся с '_':
    __init__.py, _template.py и любые общие вспомогательные модули, которые автор
    пометил префиксом '_').
    Ошибки импорта, валидации и коллизии имён логируются, проблемный файл
    пропускается — они НИКОГДА не выбрасываются наружу из этой функции и не
    прерывают обход остальных файлов.
    """
    plugins_dir.mkdir(parents=True, exist_ok=True)
    valid: dict[str, PluginRecord] = {}
    all_records: list[PluginRecord] = []

    files = sorted(plugins_dir.glob("*.py"), key=lambda p: p.name)  # детерминируемый порядок
    for path in files:
        if path.name.startswith("_"):
            continue
        try:
            module_name = f"plugins.{path.stem}"
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

            rec = _validate(module, path.name)

            if rec.valid and rec.name in core_tool_names:
                rec = PluginRecord(name=rec.name, file=path.name,
                                    error=f"Имя '{rec.name}' коллизирует с core-инструментом — отклонено.")
            elif rec.valid and rec.name in valid:
                other = valid[rec.name].file
                rec = PluginRecord(name=rec.name, file=path.name,
                                    error=f"Имя '{rec.name}' уже используется плагином '{other}' — отклонено.")

        except Exception as e:
            rec = PluginRecord(name=path.stem, file=path.name,
                                error=f"Не удалось загрузить: {e}")
            traceback.print_exc()

        all_records.append(rec)
        if rec.valid:
            valid[rec.name] = rec
            logger(f"Плагин загружен: {rec.name} ({path.name})")
        else:
            logger(f"Плагин отклонён: {path.name} — {rec.error}")

    registry = PluginRegistry(valid, logger)
    registry._all_records = all_records
    logger(f"Обнаружение плагинов завершено: активно {len(valid)}, "
           f"отклонено {len(all_records) - len(valid)}, всего {len(all_records)}.")
    return registry
