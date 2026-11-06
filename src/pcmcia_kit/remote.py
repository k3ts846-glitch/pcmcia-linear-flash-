#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Что именно выполнять на ноутбуке для каждой команды пульта.

Отдельный модуль, потому что это единственное место, где записано соответствие
«пункт меню — команда на Debian-машине». Строку легко проверить тестом, и
легко заметить, если пункт меню появился, а команды под ним нет.
"""
import re
import time

from .config import DESTRUCTIVE, NEEDS_IMAGE, REMOTE_DIR


def default_remote_image(kind, stamp=None):
    """Путь к образу на ноутбуке по умолчанию.

    Для чтения — с датой и временем, чтобы два чтения подряд не затирали друг
    друга; для записи — фиксированное имя: его видно в отчёте и легко найти.
    """
    if kind == "read":
        mark = stamp or time.strftime("%Y%m%d-%H%M")
        return f"/root/card-{mark}.bin"
    return "/root/card-write.bin"


def build(action, remote_path="", assume_yes=False, full=False,
          resume=False, journal="", backup=""):
    """Собрать команду для ноутбука. Пустая строка — действие неизвестно.

    В сигнатуре раньше были follow и json_out, но они не участвовали в сборке
    команды: «следить за журналом» делает пульт после записи (cli._follow_
    journal), а --json для info/canwrite/diag вписан в команду постоянно.
    Параметры-обманки убраны, чтобы не создавать ложного впечатления.
    """
    yes = " --yes" if assume_yes else ""
    resume_opt = " --resume" if resume else ""
    journal_opt = f" --journal '{journal}'" if journal else ""
    backup_opt = f" --backup '{backup}'" if backup else ""
    # info/canwrite/diag всегда отдают JSON — --json жестко в команде.
    if action == "info":
        return f"python3 {REMOTE_DIR}/pcmcia_ctl.py info --json"
    if action == "canwrite":
        return f"python3 {REMOTE_DIR}/pcmcia_ctl.py canwrite --json"
    if action == "diag":
        return f"python3 {REMOTE_DIR}/pcmcia_ctl.py diag --json"
    if action == "read":
        path = remote_path or default_remote_image("read")
        return f"python3 {REMOTE_DIR}/pcmcia_ctl.py read '{path}'"
    if action == "write":
        path = remote_path or default_remote_image("write")
        return f"python3 {REMOTE_DIR}/pcmcia_ctl.py write '{path}'{yes}{resume_opt}{journal_opt}{backup_opt}"
    if action == "erase":
        return f"python3 {REMOTE_DIR}/pcmcia_ctl.py erase{yes}"
    if action == "verify":
        path = remote_path or default_remote_image("write")
        return f"python3 {REMOTE_DIR}/pcmcia_ctl.py verify '{path}'"
    if action == "report":
        return f"python3 {REMOTE_DIR}/run_remote.py"
    # Запись мимо ядра, командами микросхемы через окно моста. Это отдельные
    # действия: «write» работает через /dev/mtdN и требует драйвера.
    if action == "fwprobe":
        return f"python3 {REMOTE_DIR}/pcmcia_ctl.py fwprobe{yes}"
    if action == "fwwrite":
        path = remote_path or default_remote_image("write")
        return f"python3 {REMOTE_DIR}/pcmcia_ctl.py fwwrite '{path}'{yes}"
    # Только опрос: команды 90h/98h — чтение, ничего не стирается.
    if action == "chip":
        return f"python3 {REMOTE_DIR}/pcmcia_ctl.py chip"
    if action == "selftest":
        fast = "" if full else " --fast"
        return f"python3 {REMOTE_DIR}/pcmcia_ctl.py selftest{fast}"
    return ""


def needs_image(action):
    """Нужен ли локальный файл образа для этого действия."""
    return action in NEEDS_IMAGE


def is_destructive(action):
    """Меняет ли действие содержимое карты."""
    return action in DESTRUCTIVE


_MD5_IN_TEXT = re.compile(r"md5 ([0-9a-f]{32})")


def md5_from_output(text):
    """Найти в выводе набора сумму md5 снятого образа (для сверки после scp)."""
    match = _MD5_IN_TEXT.search(text)
    return match.group(1) if match else ""


def report_was_not_created(text):
    """Понять по выводу, что образ на ноутбуке не создан и забирать нечего."""
    return ("образ не создан" in text) or ("паспорт не читается" in text)
