#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pcmcia_kit — работа с линейной флеш-картой PCMCIA с Windows-машины.

Пакет запускается как `python -m pcmcia_kit` (после установки — командой
`pcmcia-kit`). Он ничего не делает при импорте: точка входа — `main()` в
модуле `cli`.

Состав:
    config      настройки и умолчания (адрес ноутбука, порт, ключ, файлы набора)
    console     единый вид сообщений и UTF-8 в консоли Windows
    transport   ssh/scp: запуск, копирование, живой вывод, понятные ошибки
    sync        синхронизация набора: на ноутбук уходят только изменённые файлы
    state       памятка «что за карта» в шапке меню
    remote      что именно выполнять на ноутбуке для каждой команды
    commands/   по одному модулю на команду: menu, info, read, write, erase,
                verify, canwrite, diag, report, chip, selftest, key
"""

from ._version import __version__  # noqa: F401

__all__ = ["__version__"]
