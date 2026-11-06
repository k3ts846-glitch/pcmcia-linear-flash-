#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Точка входа python -m pcmcia_flash.

Запускает CLI пакета pcmcia_flash: паспорт карты, образ, запись, сверка.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, os.path.dirname(_ROOT))

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
