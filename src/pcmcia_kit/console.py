#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Вывод в консоль: единый вид сообщений и правильная кодировка.

Все сообщения набора печатаются через этот модуль. Причины:

* На Windows консоль по умолчанию читает вывод программ как cp866, и русский
  текст с ноутбука превращается в мешанину. Здесь вывод принудительно
  переводится в UTF-8 с заменой непереводимых символов — молча ничего не
  теряется.
* Цвета включаются только на терминале и только если пользователь их не
  отключил (переменная NO_COLOR). В файл и в конвейер цветовые коды не идут.
* Сообщения об ошибках всегда начинаются со слова «ОШИБКА», чтобы их можно
  было найти поиском в выводе и в журнале.
"""
import os
import sys

# Коды оформления. Пустые строки означают «без цвета» и подставляются, если
# цвета недоступны.
_CYAN = "\033[36m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_BOLD = "\033[1m"
_RESET = "\033[0m"

_color = False


def enable_utf8():
    """Перевести стандартный вывод в UTF-8. Возвращает True при успехе.

    Вызывается один раз при старте. Ошибку не поднимаем: если переключить не
    удалось, работа продолжается — просто текст может быть искажён, и об этом
    будет сказано предупреждением.
    """
    ok = True
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            ok = False
    return ok


def enable_color():
    """Включить цвета, если вывод идёт на терминал и цвета не запрещены."""
    global _color
    if os.environ.get("NO_COLOR"):
        _color = False
        return False
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        _color = False
        return False
    if os.name == "nt":
        # Windows 10 и новее понимают escape-последовательности, но только
        # после явного разрешения для текущей консоли.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)     # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            _color = False
            return False
    _color = True
    return True


def _paint(text, code):
    return code + text + _RESET if _color else text


def say(text=""):
    """Обычная строка."""
    print(text)


def head(title):
    """Заголовок раздела."""
    say()
    say(_paint("=== " + title + " ===", _CYAN))


def step(title):
    """Подзаголовок шага (короче заголовка)."""
    say()
    say(_paint("--- " + title + " ---", _BOLD))


def ok(text):
    """Сообщение об успешном результате."""
    say(_paint("  OK  " + text, _GREEN))


def warn(text):
    """Предупреждение: работа продолжается, но на это стоит посмотреть."""
    say(_paint("  !!! " + text, _YELLOW))


def bad(text):
    """Ошибка. Одно и то же слово в начале — чтобы находить поиском."""
    say(_paint("ОШИБКА: " + text, _RED))


def ask(prompt, default=""):
    """Спросить строку у пользователя. Пустой ответ = default."""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"  {prompt}{suffix}: ")
    except (EOFError, KeyboardInterrupt):
        say()
        return default
    answer = answer.strip().strip('"')
    return answer or default


def ask_yes(prompt):
    """Спросить подтверждение. Возвращает True только при явном «да»."""
    answer = ask(prompt + " (да/нет)", "нет").lower()
    return answer in ("да", "д", "yes", "y", "1")
