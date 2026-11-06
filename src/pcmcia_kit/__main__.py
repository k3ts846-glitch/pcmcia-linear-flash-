#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Запуск пакета как программы: `python -m pcmcia_kit`."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
