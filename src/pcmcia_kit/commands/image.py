#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Работа с образом: снять, записать, сверить.

Три команды отличаются друг от друга одной строкой — нужен ли образ с этой
машины и забирать ли его обратно. Поэтому они в одном модуле.
"""
import os

from .. import console, steps
from .. import remote as rctl


def check_image(path, writing):
    """Проверить локальный файл образа до подключения к ноутбуку.

    Ошибка записи не того файла — самая дорогая из возможных, поэтому проверки
    здесь строгие: файл должен быть, не быть пустым и (для записи) быть
    достаточно большим, чтобы это был образ, а не текст.
    """
    if not path:
        console.bad("файл образа не указан")
        return ""
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        console.bad(f"файл образа не найден: {path}")
        return ""
    size = os.path.getsize(path)
    if size == 0:
        console.bad(f"файл образа пустой: {path}")
        return ""
    if writing and size < 1024:
        console.bad(f"файл слишком мал для образа карты: {size} байт")
        console.say("  Похоже, это не образ карты. Запись такого файла испортит карту.")
        return ""
    return path


def run(target, kit_dir, action, image="", remote_path="", assume_yes=False,
        work_dir="", resume=False, journal="", backup=""):
    """Выполнить read, write, verify или запись без драйвера."""
    writing = action in ("write", "fwwrite")
    local_image = ""
    if rctl.needs_image(action):
        local_image = check_image(image, writing)
    if not local_image:
        return 2, ""
    if not remote_path:
        remote_path = "/root/" + os.path.basename(local_image)

    remote_cmd = rctl.build(action, remote_path, assume_yes,
                            resume=resume, journal=journal,
                            backup=backup)
    steps.describe(target, action, remote_cmd, remote_path)

    if not steps.connect(target):
        return 1, ""
    if not steps.send_kit(target, kit_dir):
        return 1, ""
    if local_image:
        if not steps.send_image(target, local_image, remote_path):
            return 1, ""
    else:
        steps.skip_image(action)

    code, text = steps.execute(target, remote_cmd, action)
    if code == 3:
        steps.needs_sudo_error(target, action)
        return 3, text
    if action == "read" and rctl.report_was_not_created(text):
        console.bad("образ не создан на ноутбуке — забирать нечего")
        console.say("  Причина в выводе выше. Чаще всего карта отвечает нестабильно")
        console.say("  (два чтения различаются) или паспорт не читается.")
        return 2, text

    collect_code = steps.collect(target, action, remote_path, work_dir or os.getcwd())
    steps.finish(target)
    return collect_code or code, text
