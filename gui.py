#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Запуск графической программы PCMCIA Flash Tool (сама попросит пароль sudo)."""

import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))
if os.geteuid() != 0:
    os.execvp("sudo", ["sudo", sys.executable, "./pcmcia_gui.py"])
os.execvp(sys.executable, [sys.executable, "./pcmcia_gui.py"])
