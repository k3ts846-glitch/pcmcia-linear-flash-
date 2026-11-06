#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пакет работы с линейной флеш-картой PCMCIA (сторона ноутбука).

Модули по зонам ответственности:

* errors      — исключения (DirectError);
* mem         — работа с /dev/mem, окнами моста, mmap, sysfs;
* constants   — константы команд и битов статуса (JEDEC / Intel CUI);
* addressing  — преобразование адресов карта ↔ чип;
* chips       — таблица микросхем, автоопределение (JEDEC ID / CFI), Vpp;
* cis         — парсинг CIS (Common Interface Structure — паспорт карты);
* power       — управление питанием сокета (TPS2220B, ExCA, Socket Control);
* nor_flash   — NorFlash и вспомогательные классы (шину, статус,
                программирование, стирание);
* image       — операции с образом: проба, запись, чтение, журнал;
* cli         — точка входа CLI (аргументы, отчёт, main).

pcmcia_direct.py перемещён в windows/legacy/ (технический долг).
Новый код живёт в этом пакете (src/pcmcia_flash/).
"""

from .errors import DirectError
from .constants import *
from .addressing import chip_of, chip_local, chip_abs
from .chips import *
from .cis import *
from .mem import *
from .nor_flash import *
from .image import *
from .cli import main

from ._version import __version__  # noqa: F401

__all__ = [
    "errors", "mem", "constants", "addressing", "chips", "cis",
    "power", "nor_flash", "image", "cli",
    "DirectError",
    "main",
]
