#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Запуск пульта ДВОЙНЫМ ЩЕЛЧКОМ по файлу — без .cmd и без PowerShell.

Почему так: раньше в наборе лежал ПУСК.cmd, который вызывал PowerShell. Это
две лишние зависимости на ровном месте и лишний повод для ошибок в кавычках.
Здесь то же самое делает сам Python:

  * при двойном щелчке по файлу `pcmcia-kit.pyw` Windows открывает его через
    pythonw.exe — то есть без окна консоли, и запускает графический пульт;
  * при запуске в консоли (`python pcmcia-kit.pyw`) работает обычное меню;
  * в конце работы консольный вариант ждёт нажатия клавиши, чтобы окно не
    закрывалось до того, как человек прочитал сообщение.

Графическая часть — отдельный модуль `gui`; если графики нет (сервер, урезанная
установка), набор честно переходит на текстовое меню, а не падает.
"""
import os
import sys
import traceback

from . import console


def _wait_for_key():
    """Не закрывать окно раньше, чем человек прочитает сообщение."""
    try:
        input("\nНажмите Enter, чтобы закрыть окно...")
    except (EOFError, KeyboardInterrupt):
        pass


def start_gui(argv):
    """Запустить графический пульт. Возвращает код или None, если графики нет."""
    try:
        from . import gui
    except ImportError as error:          # нет tkinter — это не поломка набора
        console.warn(f"графический режим недоступен ({error}); открываю текстовое меню")
        return None
    try:
        return gui.main(argv)
    except Exception as error:            # ошибка внутри окна не должна съесть текст
        name = type(error).__name__
        # Отсутствие графической среды — обычное дело на сервере и в ssh:
        # это не поломка набора, поэтому без трассировки.
        if name == "TclError":
            console.warn("графической среды нет (нет экрана) — открываю текстовое меню")
        else:
            console.bad(f"графический режим завершился с ошибкой ({name}: {error})")
            traceback.print_exc(limit=3)
        return None


def main(argv=None):
    """Точка входа для запуска двойным щелчком."""
    argv = list(sys.argv[1:] if argv is None else argv)
    console.enable_utf8()
    console.enable_color()

    # Текстовый режим просим явно: --text или --no-gui.
    text_mode = "--text" in argv or "--no-gui" in argv
    argv = [a for a in argv if a not in ("--text", "--no-gui")]

    code = None
    if not text_mode:
        code = start_gui(argv)
    if code is None:
        from .cli import main as cli_main
        code = cli_main(argv)

    if os.name == "nt" and sys.stdin and sys.stdin.isatty():
        _wait_for_key()
    return code or 0


if __name__ == "__main__":                # pragma: no cover - ручной запуск
    sys.exit(main())
