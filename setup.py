"""
Голосовой помощник — одноразовая установка.

Ставит Python-зависимости только для ЭТОЙ операционной системы: у OS-специфичных
пакетов в requirements.txt есть маркеры `sys_platform`, поэтому пользователь macOS
или Linux никогда не потянет библиотеки только для Windows (и наоборот). Затем
скачивает браузеры Playwright, нужные для веб-автоматизации (только сборки для
текущей ОС).

Опциональное локальное слово пробуждения («Привет Анфиса») здесь НЕ ставится —
это загружаемая по одному клику опция, включаемая вручную: ⚙ → WAKE WORD в приложении.
"""
import platform
import subprocess
import sys
from pathlib import Path

OS = platform.system()  # "Windows" | "Darwin" | "Linux"


def _run(label: str, args: list[str]) -> None:
    print(f"\n▶ {label}")
    subprocess.run(args, check=True)


def main() -> None:
    print(f"⚙  Установка голосового помощника — определённая ОС: {OS or 'unknown'}")

    # requirements.txt сама отсеивает OS-специфичные пакеты через маркеры pip.
    _run("Установка Python-зависимостей (OS-специфичные отсеиваются автоматически)…",
         [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])

    # Chromium покрывает Chrome/Edge/Opera/Brave/Vivaldi; Firefox — для Firefox.
    # (Автоматизации Safari дополнительно нужно: python -m playwright install webkit)
    _run("Установка браузеров Playwright (chromium + firefox)…",
         [sys.executable, "-m", "playwright", "install", "chromium", "firefox"])

    # ── Заметки по пост-установке для каждой ОС ───────────────────────────────
    if OS == "Windows":
        try:
            import win32com.client  # noqa: F401
        except ImportError:
            postinstall = Path(sys.executable).parent / "Scripts" / "pywin32_postinstall.py"
            print(
                "\n⚠️  pywin32 не зарегистрировался как надо — создание ярлыка на "
                "рабочий стол будет идти через более медленный запасной путь. "
                "Чтобы исправить, выполните:\n"
                f'    "{sys.executable}" -m pip install --force-reinstall pywin32\n'
                f'    "{sys.executable}" "{postinstall}" -install'
            )
    elif OS == "Linux":
        print(
            "\nℹ️  Заметка для Linux — часть команд управления системой обращается "
            "к нативным утилитам. Поставьте те, что будете использовать, через свой "
            "пакетный менеджер:\n"
            "    • громкость   → pulseaudio-utils   (pactl)\n"
            "    • яркость     → brightnessctl\n"
            "    • напоминания → systemd (systemd-run) или 'at'\n"
            "    • открытие URL → xdg-utils          (xdg-open)"
        )
    elif OS == "Darwin":
        print(
            "\nℹ️  Заметка для macOS — громкость, яркость и напоминания работают "
            "через встроенный 'osascript' / LaunchAgents, так что дополнительные "
            "инструменты не нужны.\n"
            "    Только для автоматизации Safari: python -m playwright install webkit"
        )

    print("\n✅ Установка завершена!")
    print("   1) Запустите его:  python main.py")
    print("   2) Вставьте бесплатный ключ Gemini API, когда появится экран настройки.")
    print("   3) (Необязательно) Включите «Привет Анфиса» в ⚙ → WAKE WORD.")


if __name__ == "__main__":
    main()
