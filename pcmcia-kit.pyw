#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Запуск пульта двойным щелчком по этому файлу.

Windows связывает расширение .pyw с pythonw.exe, поэтому при двойном щелчке
открывается графический пульт без окна консоли. В консоли тот же файл работает
как обычная программа:

    python pcmcia-kit.pyw              графический пульт
    python pcmcia-kit.pyw --text       текстовое меню
    python pcmcia-kit.pyw info         паспорт карты
    python pcmcia-kit.pyw --help       все ключи

Ни командных файлов, ни PowerShell не нужно. Если Python не установлен, двойной щелчок
ничего не сделает — поставьте Python 3 с https://www.python.org/downloads/
(при установке отметьте "Add python.exe to PATH").
"""
import os
import sys

# Запуск из папки набора: добавляем src в путь, чтобы пакет импортировался без
# установки. После `pip install .` это не требуется, но и не мешает.
_ROOT = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_ROOT, "src")
if os.path.isdir(os.path.join(_SRC, "pcmcia_kit")) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from pcmcia_kit.entry import main      # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
