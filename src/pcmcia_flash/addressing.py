#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Преобразование адресов между картой, парой чипов и локальным адресом внутри чипа.

У 16-битной карты с парой чипов байт карты i лежит в чипе i % chips по адресу
i // chips. Для команд используется адрес внутри чипа (local), который
раскладывается по чипам через chip_abs.
"""


def chip_of(addr: int, chips: int = 2) -> int:
    """Номер чипа (0-based) для байта карты addr."""
    return addr % chips


def chip_local(addr: int, chips: int = 2) -> int:
    """Адрес внутри чипа для байта карты addr."""
    return addr // chips


def chip_abs(local: int, chip: int, chips: int = 2) -> int:
    """Абсолютный адрес на карте для local внутри чипа chip.

    Для 1 чипа: адрес = local.
    Для N чипов: адрес = local * chips + chip (чередование байтов).
    """
    n = max(int(chips or 1), 1)
    return local if n == 1 else local * n + chip


__all__ = ["chip_of", "chip_local", "chip_abs"]
