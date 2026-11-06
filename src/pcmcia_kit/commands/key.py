#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Вход по ключу: настроить один раз и больше не вводить пароль.

Здесь только обвязка «пункт меню -> настройка ключа». Сама настройка живёт в
модуле pcmcia_kit.keyring, потому что она нужна и отдельной командой
(console-скрипт), и из меню.
"""
from .. import keyring


def run(target, host, user, port, key_file):
    """Настроить вход по ключу. Возвращает код возврата."""
    return keyring.setup(host, user, port, key_file)
