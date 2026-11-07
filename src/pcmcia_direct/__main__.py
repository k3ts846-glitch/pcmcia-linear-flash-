# -*- coding: utf-8 -*-
"""python3 -m pcmcia_direct — то же, что запуск лаунчера pcmcia_direct.py."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
