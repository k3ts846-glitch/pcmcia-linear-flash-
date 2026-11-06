#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Исключения пакета pcmcia_flash."""


class DirectError(Exception):
    """Ошибка прямого доступа к PCMCIA-карте через /dev/mem.

    Возникает при отсутствии прав root, недоступности адресов в iomem,
    неудачном mmap или проблемах с окном CardBus-моста.
    """


__all__ = ["DirectError"]
