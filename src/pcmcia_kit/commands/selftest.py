#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка самого набора: тесты на ноутбуке, карта не нужна.

Смысл: убедиться, что набор доехал целым и работает, ДО того как трогать
карту. Быстрый режим — по умолчанию (около 20 секунд), полный — по --full.
"""
from .. import console, steps
from .. import remote as rctl


def run(target, kit_dir, full=False):
    """Прогнать тесты набора на ноутбуке."""
    remote_cmd = rctl.build("selftest", full=full)
    steps.describe(target, "selftest", remote_cmd, "")
    console.say("  карта не затрагивается: тесты работают на модели микросхем")
    if not steps.connect(target):
        return 1
    if not steps.send_kit(target, kit_dir):
        return 1
    steps.skip_image("selftest")
    code, text = steps.execute(target, remote_cmd, "selftest")
    if code == 3:
        steps.needs_sudo_error(target, "selftest")
        return 3
    steps.finish(target)
    if code != 0:
        console.bad("тесты на ноутбуке не прошли — набор повреждён или в системе чего-то нет")
        return code
    console.ok("набор на ноутбуке цел")
    return 0
